"""Tests for finance adjustments and operations controls (TASK-052, FR-073/FR-074).

AC-1: Invoices/refunds/disputes/export and feature/suspension actions have approvals.
AC-2: Reason/MFA/safe retry permissions prevent PII access or consent bypass.
"""

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timedelta, timezone
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

COMPANY = "https://company.example.invalid/finance"


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


def test_finance_ac1_adjustment_four_eyes(database_url):
    """AC-1: refunds/credits need a different approver; execution records refs."""
    issuer = "https://identity.example.invalid/fin-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        admin = _bootstrap(database_url)
        _invite(admin, "fin-a", "finance")
        _invite(admin, "fin-b", "finance")
        fin_a = _company_client(database_url, "fin-a")
        fin_b = _company_client(database_url, "fin-b")
        requested = fin_a.post("/v1/company/finance/adjustments", json={
            "tenant_id": tid, "kind": "refund", "amount_minor": 4900,
            "reason": "Double charge in May."})
        assert requested.status_code == 201, requested.text
        adjustment_id = requested.json()["id"]
        assert requested.json()["status"] == "requested"
        # Self-approval is refused even by the requester.
        assert fin_a.post(
            f"/v1/company/finance/adjustments/{adjustment_id}/decide?approve=true",
        ).status_code == 422
        decided = fin_b.post(
            f"/v1/company/finance/adjustments/{adjustment_id}/decide?approve=true")
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "executed"
        assert decided.json()["provider_ref"].startswith("adj_test_")
        again = fin_b.post(
            f"/v1/company/finance/adjustments/{adjustment_id}/decide?approve=true")
        assert again.status_code == 409
        ledger = fin_a.get("/v1/company/finance/ledger",
                           params={"since_days": 1}).json()["items"]
        assert any(i["kind"] == "adjustment_refund_executed" for i in ledger)
        export = fin_a.get("/v1/company/finance/export",
                           params={"since_days": 1}).json()
        assert export["currency"] == "CAD" and export["generated_at"]
        assert any(i["kind"] == "adjustment_refund_executed"
                   for i in export["items"])
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)


def test_finance_ac1_tax_config_and_reconciliation(database_url):
    """AC-1: tax starts unset until configured; mismatches surface honestly."""
    issuer = "https://identity.example.invalid/fin-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        admin = _bootstrap(database_url)
        _invite(admin, "fin-2", "finance")
        _invite(admin, "sup-2", "support")
        finance = _company_client(database_url, "fin-2")
        support = _company_client(database_url, "sup-2")
        with engine.begin() as conn:
            conn.execute(text("""UPDATE company_tax_config
                                 SET gst_hst_rate=NULL, pst_rate=NULL, qst_rate=NULL,
                                     notes='' WHERE id=1"""))
        fresh = finance.get("/v1/company/finance/tax-config").json()
        assert fresh["gst_hst_rate"] is None
        assert finance.put("/v1/company/finance/tax-config", json={
            "gst_hst_rate": 0.05, "notes": "Counsel confirmed."}).status_code == 200
        assert float(finance.get("/v1/company/finance/tax-config").json()["gst_hst_rate"]) == 0.05
        assert support.put("/v1/company/finance/tax-config", json={}).status_code == 403
        clean = finance.get("/v1/company/finance/reconciliation").json()["anomalies"]
        assert not [a for a in clean if a["tenant_id"] == tid]
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(
                text("""INSERT INTO tenant_entitlements
                        (tenant_id, plan_code, status, max_locations, max_staff,
                         included_sms_segments, included_emails, marketing_allowed)
                        VALUES (:t, 'essential', 'active', 1, 5, 200, 2000, true)"""),
                {"t": tid})
        flagged = finance.get("/v1/company/finance/reconciliation").json()["anomalies"]
        mine = next(a for a in flagged if a["tenant_id"] == tid)
        assert mine["subscription_status"] == "trial"
        assert mine["entitlement_status"] == "active"
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)


def test_finance_ac2_no_client_detail_for_finance(database_url):
    """AC-2: finance has no path to client detail or service notes."""
    issuer = "https://identity.example.invalid/fin-3"
    engine, owner, tenant = _salon(database_url, issuer)
    staff = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        created = staff.post(f"/v1/tenants/{tid}/clients", json={"name": "Private"})
        assert created.status_code == 201, created.text
        admin = _bootstrap(database_url)
        _invite(admin, "fin-3", "finance")
        finance = _company_client(database_url, "fin-3")
        # No staff session, no client reads (401/404); unimplemented methods
        # stay closed (405) — never data in any case.
        assert finance.get(
            f"/v1/tenants/{tid}/clients/{created.json()['id']}").status_code in (401, 403, 404)
        assert finance.get(f"/v1/tenants/{tid}/clients").status_code in (401, 403, 404, 405)
        from modules.company import COMPANY_PERMISSIONS
        assert "company.finance.read" in COMPANY_PERMISSIONS["finance"]
        assert not (COMPANY_PERMISSIONS["finance"] & {"clients.operational.read",
                                                      "clients.sensitive.read",
                                                      "company.support.manage"})
        assert finance.get("/v1/company/tenants").status_code == 200
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)


def test_ops_ac1_incidents_queues_and_flags(database_url):
    """AC-1: incidents lifecycle, queue visibility and enforced flags."""
    issuer = "https://identity.example.invalid/ops-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        admin = _bootstrap(database_url)
        _invite(admin, "sup-4", "support")
        _invite(admin, "fin-4", "finance")
        support = _company_client(database_url, "sup-4")
        finance = _company_client(database_url, "fin-4")
        opened = support.post("/v1/company/incidents", json={
            "provider": "sms-test", "severity": "high",
            "title": "SMS latency spike"})
        assert opened.status_code == 201, opened.text
        incident_id = opened.json()["id"]
        assert finance.post("/v1/company/incidents", json={
            "provider": "x", "severity": "low", "title": "y"}).status_code == 403
        assert support.post(f"/v1/company/incidents/{incident_id}/status",
                            json={"status": "mitigated"}).status_code == 200
        assert support.post(f"/v1/company/incidents/{incident_id}/status",
                            json={"status": "resolved"}).status_code == 200
        assert support.post(f"/v1/company/incidents/{incident_id}/status",
                            json={"status": "mitigated"}).status_code == 404
        queues = support.get("/v1/company/ops/queues").json()
        assert set(queues) >= {"retry_queued", "dead_letters_open",
                               "provider_failed_24h", "open_incidents"}
        flagged = support.put("/v1/company/flags", json={
            "tenant_id": tid, "flag": "rebooking_prompts_enabled", "enabled": False})
        assert flagged.status_code == 200, flagged.text
        from modules.finance import flag_enabled
        with engine.connect() as conn:
            import uuid as _uuid
            assert flag_enabled(conn, tenant_id=tid,
                                flag="rebooking_prompts_enabled") is False
            assert flag_enabled(conn, tenant_id=str(_uuid.uuid4()),
                                flag="rebooking_prompts_enabled") is True
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)


def test_ops_ac1_suspend_reactivate_roundtrip(database_url):
    """AC-1: suspension blocks bookings independently; reactivation restores."""
    issuer = "https://identity.example.invalid/ops-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    base = f"/v1/tenants/{tid}"
    try:
        admin = _bootstrap(database_url)
        _invite(admin, "sup-5", "support")
        support = _company_client(database_url, "sup-5")
        suspended = support.post(f"/v1/company/tenants/{tid}/suspend",
                                 json={"reason": "Credential stuffing wave."})
        assert suspended.status_code == 200, suspended.text
        assert suspended.json() == {"tenant_id": tid, "status": "suspended",
                                    "was": "trial"}
        assert support.post(f"/v1/company/tenants/{tid}/suspend",
                            json={"reason": "Again."}).status_code == 409
        restored = support.post(f"/v1/company/tenants/{tid}/reactivate")
        assert restored.status_code == 200, restored.text
        assert restored.json() == {"tenant_id": tid, "status": "trial"}
        assert support.post(f"/v1/company/tenants/{tid}/reactivate").status_code == 404
        with engine.connect() as conn:
            actions = conn.execute(
                text("""SELECT action FROM company_audit_events
                        WHERE resource_id=:t ORDER BY created_at"""),
                {"t": tid}).scalars().all()
        assert actions == ["company.tenant_suspended", "company.tenant_reactivated"]
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)


def test_ops_ac2_rate_limit_and_public_status(database_url):
    """AC-2: provider bursts throttle; status notices are public."""
    issuer = "https://identity.example.invalid/ops-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        admin = _bootstrap(database_url)
        _invite(admin, "sup-6", "support")
        support = _company_client(database_url, "sup-6")
        notice = support.post("/v1/company/notices", json={
            "title": "SMS delays", "body": "Provider reports latency.",
            "severity": "degraded"})
        assert notice.status_code == 201, notice.text
        public = client.get("/v1/public/status").json()["notices"]
        assert any(n["title"] == "SMS delays" for n in public)
        codes = set()
        for _ in range(65):
            burst = client.post(f"/v1/tenants/{tid}/provider-callbacks/email",
                                content=b"{}",
                                headers={"Content-Type": "application/json",
                                         "X-Provider-Signature": "0" * 64})
            codes.add(burst.status_code)
        assert 429 in codes
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)
        with create_engine(database_url).connect() as conn:
            conn.execute(text("DELETE FROM status_notices"))
            conn.execute(text("""UPDATE company_tax_config SET gst_hst_rate=NULL,
                                 pst_rate=NULL, qst_rate=NULL, notes='' WHERE id=1"""))
            conn.commit()
