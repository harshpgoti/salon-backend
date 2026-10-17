"""Tests for the company tenant registry and employee lifecycle (TASK-050, FR-070/FR-076).

AC-1: Separate MFA company roles see safe tenant setup/billing health.
AC-2: Offboarding sessions/grants revoked; finance cannot view client notes.
"""

import hashlib
import json
import os
import sys
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


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _company_client(database_url: str, issuer: str, subject: str) -> TestClient:
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_company_identity] = lambda: AuthenticatedIdentity(
        issuer, subject, "company")
    return TestClient(app)


COMPANY = "https://company.example.invalid/registry"


def _invite(client, issuer, subject, name, role, mfa=True):
    return client.post("/v1/company/staff", json={
        "issuer": issuer, "subject": subject, "display_name": name,
        "role": role, "mfa_enrolled": mfa})


def test_company_ac1_bootstrap_and_role_separation(database_url):
    """AC-1: first employee must be an admin; later invites need one."""
    admin = _company_client(database_url, COMPANY, "admin-1")
    try:
        early = _invite(admin, COMPANY, "support-1", "Sam Support", "support")
        assert early.status_code == 422
        assert early.json()["detail"]["code"] == "bootstrap_requires_admin"
        created = _invite(admin, COMPANY, "admin-1", "Ava Admin", "admin")
        assert created.status_code == 201, created.text
        second = _invite(admin, COMPANY, "support-1", "Sam Support", "support")
        assert second.status_code == 201, second.text
        listed = admin.get("/v1/company/staff")
        assert listed.status_code == 200
        assert {s["role"] for s in listed.json()["items"]} == {"admin", "support"}
        support = _company_client(database_url, COMPANY, "support-1")
        assert support.post("/v1/company/staff", json={
            "issuer": COMPANY, "subject": "x-1", "display_name": "X",
            "role": "support", "mfa_enrolled": True}).status_code == 403
        assert support.get("/v1/company/staff").status_code == 403
        assert support.get("/v1/company/tenants").status_code == 200
    finally:
        with create_engine(database_url).connect() as conn:
            conn.execute(text("DELETE FROM company_staff WHERE issuer=:i"), {"i": COMPANY})
            conn.execute(text("DELETE FROM identity_users WHERE issuer=:i"), {"i": COMPANY})
            conn.commit()


def test_company_ac1_registry_shows_health_without_client_data(database_url):
    """AC-1: registry aggregates setup/billing health; client PII never appears."""
    issuer = "https://identity.example.invalid/reg-1"
    engine, owner, tenant = _salon(database_url, issuer)
    staff = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        created = staff.post(f"/v1/tenants/{tid}/clients", json={
            "name": "Private Person",
            "contacts": [{"kind": "phone", "value": "+14165550909", "is_primary": True}],
        })
        assert created.status_code == 201, created.text
        admin = _company_client(database_url, COMPANY, "admin-1")
        assert _invite(admin, COMPANY, "admin-1", "Ava Admin", "admin").status_code == 201
        listed = admin.get("/v1/company/tenants")
        assert listed.status_code == 200, listed.text
        mine = next(t for t in listed.json()["items"] if t["tenant_id"] == tid)
        assert mine["business_name"] == "Synthetic CRM Salon"
        assert mine["subscription_status"] == "trial"
        assert mine["plan_code"] == "essential"
        assert mine["locations"] == 1
        blob = json.dumps(listed.json())
        assert "Private Person" not in blob and "+14165550909" not in blob
        detail = admin.get(f"/v1/company/tenants/{tid}")
        assert detail.status_code == 200
        assert detail.json()["channel_setup"]["sms_senders_total"] == 0
        assert admin.get("/v1/company/tenants/00000000-0000-0000-0000-000000000000").status_code == 404
    finally:
        _teardown(engine, [tid], issuer)
        with create_engine(database_url).connect() as conn:
            conn.execute(text("DELETE FROM company_staff WHERE issuer=:i"), {"i": COMPANY})
            conn.execute(text("DELETE FROM identity_users WHERE issuer=:i"), {"i": COMPANY})
            conn.commit()


def test_company_ac2_offboarding_revokes_sessions(database_url):
    """AC-2: offboarding kills sessions and that employee's support grants."""
    admin = _company_client(database_url, COMPANY, "admin-1")
    issuer = "https://identity.example.invalid/offboard"
    salon_engine, owner, tenant = _salon(database_url, issuer)
    tid = tenant["tenant_id"]
    engine = create_engine(database_url)
    try:
        assert _invite(admin, COMPANY, "admin-1", "Ava Admin", "admin").status_code == 201
        assert _invite(admin, COMPANY, "support-9", "Sam Support", "support").status_code == 201
        support_id = admin.get("/v1/company/staff").json()["items"][1]["id"]
        with engine.begin() as conn:
            case_id = conn.execute(
                text("""INSERT INTO support_cases
                           (tenant_id, title, opened_by_issuer, opened_by_subject)
                        VALUES (:tenant, 'Synthetic offboarding case', :issuer, 'owner')
                        RETURNING id"""),
                {"tenant": tid, "issuer": issuer}).scalar_one()
            owned_grants = []
            for status in ("requested", "approved"):
                owned_grants.append(conn.execute(
                    text("""INSERT INTO support_access_grants
                               (case_id, tenant_id, status, requested_by_issuer,
                                requested_by_subject, justification)
                            VALUES (:case, :tenant, :status, :issuer, 'support-9', 'synthetic test')
                            RETURNING id"""),
                    {"case": case_id, "tenant": tid, "status": status,
                     "issuer": COMPANY}).scalar_one())
            unrelated_grant = conn.execute(
                text("""INSERT INTO support_access_grants
                           (case_id, tenant_id, status, requested_by_issuer,
                            requested_by_subject, justification)
                        VALUES (:case, :tenant, 'approved', :issuer, 'support-10', 'synthetic test')
                        RETURNING id"""),
                {"case": case_id, "tenant": tid, "issuer": COMPANY}).scalar_one()
            conn.execute(
                text("""INSERT INTO auth_sessions (session_hash, surface, issuer, subject, expires_at)
                        VALUES (:h, 'company', :i, :s, now() + interval '1 hour')"""),
                {"h": hashlib.sha256(b"support-session-9").hexdigest(),
                 "i": COMPANY, "s": "support-9"})
        dropped = admin.post(f"/v1/company/staff/{support_id}/deactivate")
        assert dropped.status_code == 200, dropped.text
        assert dropped.json() == {"id": support_id, "is_active": False,
                                  "sessions_revoked": 1, "grants_revoked": 2}
        with engine.connect() as conn:
            left = conn.execute(
                text("""SELECT count(*) FROM auth_sessions
                        WHERE issuer=:i AND subject=:s AND surface='company'"""),
                {"i": COMPANY, "s": "support-9"}).scalar_one()
            audit = conn.execute(
                text("""SELECT action FROM company_audit_events
                        WHERE resource_id=:r"""),
                {"r": support_id}).scalars().all()
            grant_statuses = conn.execute(
                text("SELECT id, status FROM support_access_grants WHERE id = ANY(:ids)"),
                {"ids": owned_grants + [unrelated_grant]}).all()
        assert left == 0
        assert {str(row.id): row.status for row in grant_statuses} == {
            str(owned_grants[0]): "revoked", str(owned_grants[1]): "revoked",
            str(unrelated_grant): "approved"}
        assert "company.staff_deactivated" in audit
        support = _company_client(database_url, COMPANY, "support-9")
        assert support.get("/v1/company/tenants").status_code == 401
        assert admin.post(f"/v1/company/staff/{support_id}/deactivate").status_code == 404
    finally:
        engine.dispose()
        _teardown(salon_engine, [tid], issuer)
        with create_engine(database_url).connect() as conn:
            conn.execute(text("DELETE FROM company_staff WHERE issuer=:i"), {"i": COMPANY})
            conn.execute(text("DELETE FROM identity_users WHERE issuer=:i"), {"i": COMPANY})
            conn.commit()


def test_company_ac2_finance_and_mfa_boundaries(database_url):
    """AC-2: finance never sees client detail; MFA-less sessions stop at the door."""
    issuer = "https://identity.example.invalid/reg-2"
    engine, owner, tenant = _salon(database_url, issuer)
    staff = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        created = staff.post(f"/v1/tenants/{tid}/clients", json={"name": "Private Person"})
        assert created.status_code == 201, created.text
        client_id = created.json()["id"]
        admin = _company_client(database_url, COMPANY, "admin-1")
        assert _invite(admin, COMPANY, "admin-1", "Ava Admin", "admin").status_code == 201
        assert _invite(admin, COMPANY, "fin-1", "Fay Finance", "finance").status_code == 201
        finance = _company_client(database_url, COMPANY, "fin-1")
        # A company identity holds no staff session: client detail does not
        # resolve for it at all (401 here — never the data).
        peek = finance.get(f"/v1/tenants/{tid}/clients/{client_id}")
        assert peek.status_code in (401, 403, 404)
        assert finance.get("/v1/company/tenants").status_code == 200
        assert _invite(admin, COMPANY, "nomfa-1", "No Mfa", "success",
                       mfa=False).status_code == 201
        nomfa = _company_client(database_url, COMPANY, "nomfa-1")
        denied = nomfa.get("/v1/company/tenants")
        assert denied.status_code == 401
        assert denied.json()["detail"]["code"] == "company_mfa_required"
    finally:
        _teardown(engine, [tid], issuer)
        with create_engine(database_url).connect() as conn:
            conn.execute(text("DELETE FROM company_staff WHERE issuer=:i"), {"i": COMPANY})
            conn.execute(text("DELETE FROM identity_users WHERE issuer=:i"), {"i": COMPANY})
            conn.commit()
