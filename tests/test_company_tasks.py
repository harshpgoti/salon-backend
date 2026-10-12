"""Tests for customer success tasks and explainable health (TASK-062, FR-075).

AC-1: Business lead/onboarding/renewal tasks expose explainable tenant health.
AC-2: Salons client data never reused for company sales; role scopes enforced.
"""

import json
import os
import sys
from datetime import timedelta
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

COMPANY = "https://company.example.invalid/success"


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


def test_success_ac1_health_explains_and_tasks_flow(database_url):
    """AC-1: scores carry reasons; leads/onboarding/risks open and resolve."""
    issuer = "https://identity.example.invalid/suc-1"
    engine, owner, tenant = _salon(database_url, issuer)
    staff = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        admin = _bootstrap(database_url)
        _invite(admin, "suc-1", "success")
        success = _company_client(database_url, "suc-1")
        created = staff.post(f"/v1/tenants/{tid}/clients", json={
            "name": "Private Person",
            "contacts": [{"kind": "phone", "value": "+14165550909", "is_primary": True}],
        })
        assert created.status_code == 201, created.text
        health = success.get(f"/v1/company/tenants/{tid}/health")
        assert health.status_code == 200, health.text
        body = health.json()
        assert body["subscription_status"] == "trial"
        assert any("no bookings" in reason for reason in body["reasons"])
        blob = json.dumps(body)
        assert "Private Person" not in blob and "+14165550909" not in blob
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE tenant_subscriptions SET status='past_due'
                                 WHERE tenant_id=:t"""), {"t": tid})
        sicker = success.get(f"/v1/company/tenants/{tid}/health").json()
        assert sicker["score"] == body["score"] - 20
        assert any("past_due" in reason for reason in sicker["reasons"])
        lead = success.post("/v1/company/tasks", json={
            "kind": "lead", "title": "Downtown barbershop",
            "detail": "Met at trade show."})
        assert lead.status_code == 201, lead.text
        listed = success.get("/v1/company/tasks").json()["items"]
        assert [t["kind"] for t in listed] == ["lead"]
        done = success.post(f"/v1/company/tasks/{lead.json()['id']}/resolve",
                            params={"resolution": "done"})
        assert done.status_code == 200, done.text
        assert done.json()["status"] == "done"
        assert success.post(f"/v1/company/tasks/{lead.json()['id']}/resolve",
                            params={"resolution": "done"}).status_code == 404
        assert success.get("/v1/company/tenants/00000000-0000-0000-0000-000000000000/health"
                           ).status_code == 404
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)


def test_success_ac1_suggestions_surface_risks(database_url):
    """AC-1: expiring trials, stuck setup and urgent cases suggest work."""
    issuer = "https://identity.example.invalid/suc-2"
    engine, owner, tenant = _salon(database_url, issuer)
    staff = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        admin = _bootstrap(database_url)
        _invite(admin, "suc-2", "success")
        success = _company_client(database_url, "suc-2")
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE tenant_subscriptions
                                 SET trial_ends_at = now() + interval '3 days'
                                 WHERE tenant_id=:t"""), {"t": tid})
            conn.execute(
                text("""INSERT INTO support_cases
                        (tenant_id, title, severity, opened_by_issuer, opened_by_subject)
                        VALUES (:t, 'Outage?', 'urgent', 'x', 'y')"""),
                {"t": tid})
        suggestions = success.get("/v1/company/tasks/suggestions").json()["suggestions"]
        kinds = {(s["tenant_id"], s["kind"]) for s in suggestions}
        assert (tid, "renewal") in kinds
        assert (tid, "risk") in kinds
        assert all("Private" not in json.dumps(s) for s in suggestions)
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)


def test_success_ac2_role_scopes_hold(database_url):
    """AC-2: success cannot touch finance or suspension; finance cannot task."""
    issuer = "https://identity.example.invalid/suc-3"
    engine, owner, tenant = _salon(database_url, issuer)
    staff = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        admin = _bootstrap(database_url)
        _invite(admin, "suc-3", "success")
        _invite(admin, "fin-9", "finance")
        success = _company_client(database_url, "suc-3")
        finance = _company_client(database_url, "fin-9")
        assert success.get("/v1/company/finance/ledger",
                           params={"since_days": 1}).status_code == 403
        assert success.post(f"/v1/company/tenants/{tid}/suspend",
                            json={"reason": "x"}).status_code == 403
        assert finance.post("/v1/company/tasks", json={
            "kind": "lead", "title": "x"}).status_code == 403
        assert success.get("/v1/company/tenants").status_code == 200
        assert finance.get("/v1/company/tenants").status_code == 200
        assert success.post("/v1/company/tasks", json={
            "kind": "risk", "title": "x"}).status_code == 422
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)
