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
import time
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


def _company_client(database_url: str, subject: str, *, auth_time=None) -> TestClient:
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_company_identity] = lambda: AuthenticatedIdentity(
        COMPANY, subject, "company", auth_time=time.time() if auth_time is None else auth_time)
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
        conn.execute(text("DELETE FROM company_action_approvals WHERE requested_by_issuer=:i"), {"i": COMPANY})
        conn.execute(text("DELETE FROM company_staff WHERE issuer=:i"), {"i": COMPANY})
        conn.execute(text("DELETE FROM identity_users WHERE issuer=:i"), {"i": COMPANY})
        conn.commit()


def test_finance_ac1_adjustment_four_eyes(database_url):
    """AC-1: refunds/credits need a different approver; execution records refs."""
    issuer = "https://identity.example.invalid/fin-1"
    engine, owner, tenant = _salon(database_url, issuer)
    engine2, _, tenant2 = _salon(database_url, issuer + "-other")
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        admin = _bootstrap(database_url)
        _invite(admin, "fin-a", "finance")
        _invite(admin, "fin-b", "finance")
        _invite(admin, "sup-replay", "support")
        fin_a = _company_client(database_url, "fin-a")
        fin_b = _company_client(database_url, "fin-b")
        support_replay = _company_client(database_url, "sup-replay")
        requested = fin_a.post("/v1/company/finance/adjustments", json={
            "tenant_id": tid, "kind": "refund", "amount_minor": 4900,
            "reason": "Double charge in May."}, headers={"Idempotency-Key": "adjustment-request-001"})
        assert requested.status_code == 201, requested.text
        adjustment_id = requested.json()["id"]
        assert requested.json()["status"] == "requested"
        replay = fin_a.post("/v1/company/finance/adjustments", json={
            "tenant_id": tid, "kind": "refund", "amount_minor": 4900,
            "reason": "Double charge in May."}, headers={"Idempotency-Key": "adjustment-request-001"})
        assert replay.json() == {"id": adjustment_id, "status": "requested", "replayed": True}
        reused_for_other_salon = fin_a.post("/v1/company/finance/adjustments", json={
            "tenant_id": tenant2["tenant_id"], "kind": "refund", "amount_minor": 4900,
            "reason": "Double charge in May."}, headers={"Idempotency-Key": "adjustment-request-001"})
        assert reused_for_other_salon.status_code == 409
        # Self-approval is refused even by the requester.
        assert fin_a.post(
            f"/v1/company/finance/adjustments/{adjustment_id}/decide?approve=true",
            headers={"Idempotency-Key": "adjustment-self-approve-001"},
        ).status_code == 422
        decided = fin_b.post(
            f"/v1/company/finance/adjustments/{adjustment_id}/decide?approve=true",
            headers={"Idempotency-Key": "adjustment-approve-001"})
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "executed"
        assert decided.json()["provider_ref"].startswith("adj_test_")
        again = fin_b.post(
            f"/v1/company/finance/adjustments/{adjustment_id}/decide?approve=true",
            headers={"Idempotency-Key": "adjustment-approve-001"})
        assert again.status_code == 200 and again.json()["replayed"] is True
        assert fin_b.post(
            f"/v1/company/finance/adjustments/{adjustment_id}/decide?approve=true",
            headers={"Idempotency-Key": "adjustment-approve-002"}).status_code == 409
        ledger = fin_a.get("/v1/company/finance/ledger",
                           params={"since_days": 1}).json()["items"]
        assert any(i["kind"] == "adjustment_refund_executed" for i in ledger)
        export_request = fin_a.post("/v1/company/approvals", json={
            "action": "accounting_export", "since_days": 1,
            "reason": "Monthly reconciliation export.", "idempotency_key": "export-request-001"})
        assert export_request.status_code == 201, export_request.text
        export_id = export_request.json()["id"]
        assert fin_a.post(f"/v1/company/approvals/{export_id}/decide", json={
            "approve": True, "idempotency_key": "export-self-approve-001"}).status_code == 422
        export_decision = fin_b.post(f"/v1/company/approvals/{export_id}/decide", json={
            "approve": True, "idempotency_key": "export-approve-001"})
        assert export_decision.status_code == 200, export_decision.text
        assert support_replay.post(f"/v1/company/approvals/{export_id}/decide", json={
            "approve": True, "idempotency_key": "export-approve-001"}).status_code == 403
        export = fin_a.get("/v1/company/finance/export", params={"approval_id": export_id}).json()
        assert export["currency"] == "CAD" and export["generated_at"]
        assert any(i["kind"] == "adjustment_refund_executed"
                   for i in export["items"])
        assert fin_a.get("/v1/company/finance/export", params={"since_days": 1}).status_code == 422
        assert all("result" not in item for item in fin_a.get("/v1/company/approvals").json()["items"])
        stale_finance = _company_client(database_url, "fin-a", auth_time=time.time() - 600)
        assert stale_finance.get("/v1/company/finance/export", params={"approval_id": export_id}).status_code == 401
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id',:t,true)"), {"t": tid})
            conn.execute(text("INSERT INTO billing_events(tenant_id,provider_event_id,event_type) VALUES (:t,'evt-invoice-052','invoice.paid'),(:t,'evt-dispute-052','dispute.created')"), {"t": tid})
        invoice = fin_a.post("/v1/company/approvals", json={
            "action": "invoice_action", "tenant_id": tid, "provider_ref": "evt-invoice-052",
            "operation": "void", "reason": "Duplicate synthetic invoice.",
            "idempotency_key": "invoice-action-052"})
        assert invoice.status_code == 201, invoice.text
        invoice_approved = fin_b.post(f"/v1/company/approvals/{invoice.json()['id']}/decide", json={
            "approve": True, "idempotency_key": "invoice-approve-052"})
        assert invoice_approved.status_code == 200
        assert invoice_approved.json()["result"]["execution"] == "approval_recorded_only"
        dispute = fin_a.post("/v1/company/approvals", json={
            "action": "dispute_action", "tenant_id": tid, "provider_ref": "evt-dispute-052",
            "operation": "contest", "reason": "Evidence packet reviewed.",
            "idempotency_key": "dispute-action-052"})
        assert dispute.status_code == 201, dispute.text
        dispute_approved = fin_b.post(f"/v1/company/approvals/{dispute.json()['id']}/decide", json={
            "approve": True, "idempotency_key": "dispute-approve-052"})
        assert dispute_approved.status_code == 200
        assert dispute_approved.json()["result"]["execution"] == "approval_recorded_only"
    finally:
        _teardown(engine, [tid], issuer)
        _teardown(engine2, [tenant2["tenant_id"]], issuer + "-other")
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


def test_finance_approval_requires_recent_mfa_reason_and_distinct_actor(database_url):
    issuer = "https://identity.example.invalid/fin-mfa"
    engine, owner, tenant = _salon(database_url, issuer)
    try:
        admin = _bootstrap(database_url)
        _invite(admin, "fin-mfa", "finance")
        fresh = _company_client(database_url, "fin-mfa")
        stale = _company_client(database_url, "fin-mfa", auth_time=time.time() - 600)
        assert stale.post("/v1/company/approvals", json={
            "action": "accounting_export", "since_days": 30, "reason": "Monthly close",
            "idempotency_key": "stale-export-001"}).status_code == 401
        missing_reason = fresh.post("/v1/company/approvals", json={
            "action": "accounting_export", "since_days": 30, "reason": " ",
            "idempotency_key": "blank-export-001"})
        assert missing_reason.status_code == 422
        requested = fresh.post("/v1/company/approvals", json={
            "action": "accounting_export", "since_days": 30, "reason": "Monthly close",
            "idempotency_key": "mfa-export-001"})
        assert requested.status_code == 201
        collision = fresh.post("/v1/company/approvals", json={
            "action": "accounting_export", "since_days": 60, "reason": "Monthly close",
            "idempotency_key": "mfa-export-001"})
        assert collision.status_code == 409
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
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
        assert flagged.status_code == 409
        request = support.post("/v1/company/approvals", json={
            "action": "feature_flag", "tenant_id": tid, "flag": "rebooking_prompts_enabled",
            "enabled": False, "reason": "Pause synthetic prompts during investigation.",
            "idempotency_key": "flag-request-001"})
        assert request.status_code == 201, request.text
        assert support.post(f"/v1/company/approvals/{request.json()['id']}/decide", json={
            "approve": True, "idempotency_key": "flag-self-001"}).status_code == 422
        assert admin.post(f"/v1/company/approvals/{request.json()['id']}/decide", json={
            "approve": True, "idempotency_key": "flag-approve-001"}).status_code == 200
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
        assert support.post(f"/v1/company/tenants/{tid}/suspend",
                            json={"reason": "Credential stuffing wave."}).status_code == 409
        suspend_request = support.post("/v1/company/approvals", json={
            "action": "tenant_suspend", "tenant_id": tid, "reason": "Credential stuffing wave.",
            "idempotency_key": "suspend-request-001"})
        assert suspend_request.status_code == 201, suspend_request.text
        suspended = admin.post(f"/v1/company/approvals/{suspend_request.json()['id']}/decide", json={
            "approve": True, "idempotency_key": "suspend-approve-001"})
        assert suspended.status_code == 200, suspended.text
        assert suspended.json()["result"] == {"tenant_id": tid, "status": "suspended", "was": "trial"}
        repeat = admin.post(f"/v1/company/approvals/{suspend_request.json()['id']}/decide", json={
            "approve": True, "idempotency_key": "suspend-approve-001"})
        assert repeat.status_code == 200 and repeat.json()["replayed"] is True
        restore_request = support.post("/v1/company/approvals", json={
            "action": "tenant_reactivate", "tenant_id": tid, "reason": "Investigation resolved.",
            "idempotency_key": "reactivate-request-001"})
        assert restore_request.status_code == 201
        restored = admin.post(f"/v1/company/approvals/{restore_request.json()['id']}/decide", json={
            "approve": True, "idempotency_key": "reactivate-approve-001"})
        assert restored.status_code == 200, restored.text
        assert restored.json()["result"] == {"tenant_id": tid, "status": "trial"}
        with engine.connect() as conn:
            actions = conn.execute(text("SELECT action FROM company_audit_events WHERE resource_id=:t ORDER BY created_at"), {"t": tid}).scalars().all()
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
