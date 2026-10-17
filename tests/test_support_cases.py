"""Tests for support cases and tenant-approved access (TASK-051, FR-071/FR-072).

AC-1: Case correlates safe booking/job/trace; default masked diagnostic view.
AC-2: Grant scope/expiry/revoke enforced and every use tenant-visible audited.
"""

import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.identity.main import (
    AuthenticatedIdentity,
    create_app,
    require_company_identity,
)
from test_clients import _salon, _client, _teardown

COMPANY = "https://company.example.invalid/support"


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _company_client(database_url: str, subject: str) -> TestClient:
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_company_identity] = lambda: AuthenticatedIdentity(
        COMPANY, subject, "company")
    return TestClient(app)


def _invite(admin, subject, role):
    resp = admin.post("/v1/company/staff", json={
        "issuer": COMPANY, "subject": subject, "display_name": subject.title(),
        "role": role, "mfa_enrolled": True})
    assert resp.status_code == 201, resp.text


def _bootstrap(database_url):
    admin = _company_client(database_url, "admin-1")
    first = admin.post("/v1/company/staff", json={
        "issuer": COMPANY, "subject": "admin-1", "display_name": "Ava Admin",
        "role": "admin", "mfa_enrolled": True})
    assert first.status_code == 201, first.text
    return admin


def _cleanup_company(database_url):
    with create_engine(database_url).connect() as conn:
        conn.execute(text("DELETE FROM company_staff WHERE issuer=:i"), {"i": COMPANY})
        conn.execute(text("DELETE FROM identity_users WHERE issuer=:i"), {"i": COMPANY})
        conn.commit()


def test_support_ac1_case_masks_and_grant_flows(database_url):
    """AC-1: safe correlates, redacted notes, masked diagnostics, logged use."""
    issuer = "https://identity.example.invalid/sup-1"
    engine, owner, tenant = _salon(database_url, issuer)
    staff = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        request_id = uuid.uuid4()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            job_id = conn.execute(
                text("""INSERT INTO tenant_jobs (tenant_id, kind, state)
                        VALUES (:tenant, 'support_fixture', 'failed') RETURNING id"""),
                {"tenant": tid}).scalar_one()
        admin = _bootstrap(database_url)
        _invite(admin, "support-1", "support")
        support = _company_client(database_url, "support-1")
        opened = support.post("/v1/company/cases", json={
            "tenant_id": tid, "title": "Booking stuck", "severity": "high",
            "request_id": str(request_id), "job_id": str(job_id),
            "trace_id": "trace-abc-123",
            "notes": "Client ana@example.com called from +14165550000, ref 987654."})
        assert opened.status_code == 201, opened.text
        case_id = opened.json()["id"]
        with engine.connect() as conn:
            stored = conn.execute(
                text("SELECT internal_notes FROM support_cases WHERE id=:id"),
                {"id": case_id}).scalar_one()
        assert "ana@example.com" not in stored and "+14165550000" not in stored
        assert "[redacted-email]" in stored and "[redacted-phone]" in stored
        listed = support.get("/v1/company/cases").json()["items"]
        assert len(listed) == 1
        assert listed[0]["tenant_name"] == "Synthetic CRM Salon"
        assert "internal_notes" not in listed[0]
        assert listed[0]["request_id"] == str(request_id)
        assert listed[0]["job_id"] == str(job_id)
        assert listed[0]["trace_id"] == "trace-abc-123"
        assert listed[0]["grants"] == []
        requested = support.post(f"/v1/company/cases/{case_id}/grants", json={
            "scope": "diagnostic", "justification": "Reproduce the stuck booking."})
        assert requested.status_code == 201, requested.text
        grant_id = requested.json()["id"]
        listed = support.get("/v1/company/cases").json()["items"]
        grant = listed[0]["grants"][0]
        assert {grant["id"], grant["scope"], grant["status"], grant["uses"]} == {
            grant_id, "diagnostic", "requested", 0}
        mine = staff.get(f"/v1/tenants/{tid}/support-access").json()
        assert [g["status"] for g in mine["grants"]] == ["requested"]
        assert mine["uses"] == []
        approved = staff.post(f"/v1/tenants/{tid}/support-access/{grant_id}/approve")
        assert approved.status_code == 200, approved.text
        diag = support.get(f"/v1/company/tenants/{tid}/diagnostics",
                           params={"grant_id": grant_id})
        assert diag.status_code == 200, diag.text
        snapshot = json.dumps(diag.json()["snapshot"])
        assert "Synthetic CRM Salon" in snapshot
        assert "ana@example.com" not in snapshot and "+14165550000" not in snapshot
        mine = staff.get(f"/v1/tenants/{tid}/support-access").json()
        assert len(mine["uses"]) == 1
        assert mine["uses"][0]["action"] == "diagnostics_read"
        listed = support.get("/v1/company/cases").json()["items"]
        assert listed[0]["grants"][0]["uses"] == 1
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)


def test_support_ac2_expiry_revoke_and_reject(database_url):
    """AC-2: expiry flips state, revoke and reject close the door."""
    issuer = "https://identity.example.invalid/sup-2"
    engine, owner, tenant = _salon(database_url, issuer)
    staff = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        admin = _bootstrap(database_url)
        _invite(admin, "support-2", "support")
        support = _company_client(database_url, "support-2")
        case_id = support.post("/v1/company/cases", json={
            "tenant_id": tid, "title": "Slow reminders"}).json()["id"]
        grant_id = support.post(f"/v1/company/cases/{case_id}/grants", json={
            "justification": "Check the outbox."}).json()["id"]
        assert staff.post(f"/v1/tenants/{tid}/support-access/{grant_id}/approve").status_code == 200
        with engine.begin() as conn:
            conn.execute(
                text("""UPDATE support_access_grants SET expires_at = now() - interval '1 minute'
                        WHERE id=:id"""),
                {"id": grant_id})
        refused = support.get(f"/v1/company/tenants/{tid}/diagnostics",
                              params={"grant_id": grant_id})
        assert refused.status_code == 403
        with engine.connect() as conn:
            state = conn.execute(
                text("SELECT status FROM support_access_grants WHERE id=:id"),
                {"id": grant_id}).scalar_one()
        assert state == "expired"
        grant2 = support.post(f"/v1/company/cases/{case_id}/grants", json={
            "justification": "Second look."}).json()["id"]
        assert staff.post(f"/v1/tenants/{tid}/support-access/{grant2}/reject").status_code == 200
        assert staff.post(f"/v1/tenants/{tid}/support-access/{grant2}/approve").status_code == 404
        grant3 = support.post(f"/v1/company/cases/{case_id}/grants", json={
            "justification": "Third look."}).json()["id"]
        assert staff.post(f"/v1/tenants/{tid}/support-access/{grant3}/approve").status_code == 200
        assert support.post(f"/v1/company/grants/{grant3}/revoke").status_code == 200
        assert support.get(f"/v1/company/tenants/{tid}/diagnostics",
                           params={"grant_id": grant3}).status_code == 403
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)


def test_support_ac2_roles_and_closed_cases(database_url):
    """AC-2: finance cannot case-work; closed cases and resolved cases lock."""
    issuer = "https://identity.example.invalid/sup-3"
    engine, owner, tenant = _salon(database_url, issuer)
    staff = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        admin = _bootstrap(database_url)
        _invite(admin, "support-3", "support")
        _invite(admin, "fin-3", "finance")
        finance = _company_client(database_url, "fin-3")
        assert finance.post("/v1/company/cases", json={
            "tenant_id": tid, "title": "X"}).status_code == 403
        support = _company_client(database_url, "support-3")
        assert support.post("/v1/company/cases", json={
            "tenant_id": "00000000-0000-0000-0000-000000000000",
            "title": "Ghost"}).status_code == 404
        case_id = support.post("/v1/company/cases", json={
            "tenant_id": tid, "title": "Done deal"}).json()["id"]
        assert support.post(f"/v1/company/cases/{case_id}/status",
                            json={"status": "resolved"}).status_code == 200
        assert support.post(f"/v1/company/cases/{case_id}/grants", json={
            "justification": "Too late."}).status_code == 422
        assert support.post(f"/v1/company/cases/{case_id}/status",
                            json={"status": "closed"}).status_code == 200
        assert support.post(f"/v1/company/cases/{case_id}/status",
                            json={"status": "open"}).status_code == 422
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)


def test_support_case_rejects_cross_tenant_job_reference(database_url):
    """A company case may correlate only to a job in its selected salon."""
    first_issuer = "https://identity.example.invalid/sup-job-a"
    second_issuer = "https://identity.example.invalid/sup-job-b"
    first_engine, owner, first_tenant = _salon(database_url, first_issuer)
    second_engine, _, second_tenant = _salon(database_url, second_issuer)
    first_id, second_id = first_tenant["tenant_id"], second_tenant["tenant_id"]
    try:
        with first_engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": first_id})
            job_id = conn.execute(
                text("""INSERT INTO tenant_jobs (tenant_id, kind, state)
                        VALUES (:tenant, 'support_fixture', 'failed') RETURNING id"""),
                {"tenant": first_id}).scalar_one()
        admin = _bootstrap(database_url)
        _invite(admin, "support-job", "support")
        support = _company_client(database_url, "support-job")
        opened = support.post("/v1/company/cases", json={
            "tenant_id": second_id, "title": "Cross-salon reference",
            "job_id": str(job_id)})
        assert opened.status_code == 422, opened.text
        assert opened.json()["detail"]["code"] == "support_reference_unknown"
    finally:
        _teardown(first_engine, [first_id], first_issuer)
        _teardown(second_engine, [second_id], second_issuer)
        _cleanup_company(database_url)
