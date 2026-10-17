"""Production authorization regressions for TASK-070 (synthetic PostgreSQL)."""
import os
import uuid
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from test_clients import _salon, _client, _teardown
from test_support_cases import (COMPANY, _bootstrap, _invite, _company_client,
                                _cleanup_company)
from test_billing_webhooks import _secret, _post, _sub, _entitlements


@pytest.fixture
def database_url():
    value = os.environ.get("DATABASE_URL")
    if not value:
        pytest.fail("TASK-070 requires the synthetic PostgreSQL database")
    return value


@pytest.fixture
def support_context(database_url):
    issuer = "https://identity.example.invalid/authz-" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    tid = tenant["tenant_id"]
    salon = _client(database_url, owner)
    try:
        admin = _bootstrap(database_url)
        _invite(admin, "grant-owner", "support")
        _invite(admin, "other-support", "support")
        support = _company_client(database_url, "grant-owner")
        opened = support.post("/v1/company/cases", json={
            "tenant_id": tid, "title": "Synthetic authorization case"})
        assert opened.status_code == 201, opened.text
        case_id = opened.json()["id"]
        requested = support.post(f"/v1/company/cases/{case_id}/grants", json={
            "justification": "Investigate synthetic case"})
        assert requested.status_code == 201, requested.text
        gid = requested.json()["id"]
        assert salon.post(f"/v1/tenants/{tid}/support-access/{gid}/approve").status_code == 200
        yield engine, tid, salon, admin, support, case_id, gid
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)


@pytest.mark.parametrize("invalid", ["billing_scope", "other_requester",
                                    "resolved_case", "closed_case", "no_expiry"])
def test_support_grant_restricts_both_reads(database_url, support_context, invalid):
    engine, tid, salon, admin, support, case_id, gid = support_context
    actor = support
    with engine.begin() as conn:
        if invalid == "billing_scope":
            conn.execute(text("UPDATE support_access_grants SET scope='billing' WHERE id=:g"), {"g": gid})
        elif invalid == "other_requester":
            actor = _company_client(database_url, "other-support")
        elif invalid == "no_expiry":
            conn.execute(text("UPDATE support_access_grants SET expires_at=NULL WHERE id=:g"), {"g": gid})
        else:
            status = "resolved" if invalid == "resolved_case" else "closed"
            conn.execute(text("UPDATE support_cases SET status=:s WHERE id=:c"), {"s": status, "c": case_id})
    diagnostic = actor.get(f"/v1/company/tenants/{tid}/diagnostics", params={"grant_id": gid})
    trace = actor.get(f"/v1/company/traces/{uuid.uuid4()}", params={"tenant_id": tid, "grant_id": gid})
    assert diagnostic.status_code == 403, diagnostic.text
    assert trace.status_code == 403, trace.text
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM support_grant_uses WHERE grant_id=:g"), {"g": gid}).scalar_one() == 0
        assert conn.execute(text("SELECT status FROM support_access_grants WHERE id=:g"), {"g": gid}).scalar_one() == "approved"


@pytest.mark.parametrize("case_status", ["open", "triaged", "assigned"])
def test_requester_can_read_working_case(support_context, case_status):
    engine, tid, salon, admin, support, case_id, gid = support_context
    with engine.begin() as conn:
        conn.execute(text("UPDATE support_cases SET status=:s WHERE id=:c"), {"s": case_status, "c": case_id})
    assert support.get(f"/v1/company/tenants/{tid}/diagnostics", params={"grant_id": gid}).status_code == 200
    assert support.get(f"/v1/company/traces/{uuid.uuid4()}", params={"tenant_id": tid, "grant_id": gid}).status_code == 200
    visible = salon.get(f"/v1/tenants/{tid}/support-access").json()["uses"]
    assert {use["action"] for use in visible} == {"diagnostics_read", "trace_read"}


def test_offboarding_revokes_requested_and_approved_grants(support_context):
    engine, tid, salon, admin, support, case_id, gid = support_context
    pending = support.post(f"/v1/company/cases/{case_id}/grants", json={
        "justification": "Additional synthetic investigation"}).json()["id"]
    staff_id = next(s["id"] for s in admin.get("/v1/company/staff").json()["items"] if s["display_name"] == "Grant-Owner")
    offboarded = admin.post(f"/v1/company/staff/{staff_id}/deactivate")
    assert offboarded.status_code == 200, offboarded.text
    with engine.connect() as conn:
        states = conn.execute(text("SELECT status FROM support_access_grants WHERE id IN (:a, :b)"), {"a": gid, "b": pending}).scalars().all()
    assert states == ["revoked", "revoked"]
    assert support.get(f"/v1/company/tenants/{tid}/diagnostics", params={"grant_id": gid}).status_code == 401
    assert salon.post(f"/v1/tenants/{tid}/support-access/{pending}/approve").status_code == 404


@pytest.mark.parametrize("event_type", ["checkout.session.completed", "invoice.paid",
                                        "customer.subscription.updated"])
def test_payment_event_preserves_security_suspension(database_url, event_type):
    issuer = "https://identity.example.invalid/security-billing-" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    tid = tenant["tenant_id"]
    client = _client(database_url, owner)
    try:
        secret = _secret(engine)
        started = client.post(f"/v1/tenants/{tid}/billing/checkout", json={"plan_code": "growth"})
        assert started.status_code == 201, started.text
        session_ref = started.json()["checkout_url"].rsplit("/", 1)[-1]
        customer = "cus_security_" + uuid.uuid4().hex
        base = datetime.now(timezone.utc)
        data = {"tenant_id": tid, "session_ref": session_ref, "customer_ref": customer,
                "subscription_ref": "sub_" + uuid.uuid4().hex, "plan_code": "growth",
                "provider_status": "active", "period_end": (base + timedelta(days=30)).isoformat()}
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("UPDATE tenant_subscriptions SET status='suspended' WHERE tenant_id=:t"), {"t": tid})
            conn.execute(text("""UPDATE tenant_subscriptions
                SET stripe_subscription_ref=:sub, stripe_customer_ref=:customer WHERE tenant_id=:t"""),
                {"sub": data["subscription_ref"], "customer": customer, "t": tid})
            conn.execute(text("INSERT INTO billing_customer_map (customer_ref, tenant_id) VALUES (:c, :t)"), {"c": customer, "t": tid})
            conn.execute(text("""INSERT INTO billing_provider_facts
                (tenant_id, subscription_ref, customer_ref, provider_status, plan_code,
                 period_end, provider_updated_at)
                VALUES (:tenant, :sub, :customer, 'active', 'growth', :period, :at)"""), {
                "tenant": tid, "sub": data["subscription_ref"], "customer": customer,
                "period": base + timedelta(days=30), "at": base})
        event = {"id": "evt_security_" + uuid.uuid4().hex, "type": event_type,
                 "occurred_at": base.isoformat(), "data": data}
        applied = _post(client, tid, secret, event)
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied"
        assert _sub(client, tid)["status"] == "suspended"
        entitlements = _entitlements(client, tid)
        assert entitlements["marketing_allowed"] is False
        assert entitlements["status"] == "suspended"
    finally:
        _teardown(engine, [tid], issuer)


def test_grant_expiring_during_lock_wait_is_denied(support_context):
    engine, tid, salon, admin, support, case_id, gid = support_context
    with engine.begin() as conn:
        conn.execute(text("UPDATE support_access_grants SET expires_at=clock_timestamp() + interval '3 seconds' WHERE id=:g"), {"g": gid})
    with ThreadPoolExecutor(max_workers=1) as executor:
        with engine.begin() as blocker:
            blocker.execute(text("SELECT id FROM support_access_grants WHERE id=:g FOR UPDATE"), {"g": gid})
            pending = executor.submit(support.get, f"/v1/company/tenants/{tid}/diagnostics", params={"grant_id": gid})
            deadline = time.monotonic() + 2
            blocked = False
            while time.monotonic() < deadline:
                with engine.connect() as observer:
                    blocked = observer.execute(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND wait_event_type='Lock' AND query LIKE '%FOR SHARE OF g, c%')")).scalar_one()
                if blocked:
                    break
                time.sleep(0.02)
            assert blocked, "Read must actually wait behind the grant lock"
            time.sleep(3.1)
        response = pending.result(timeout=10)
    assert response.status_code == 403, response.text
    assert salon.get(f"/v1/tenants/{tid}/support-access").json()["uses"] == []


def test_grant_request_cannot_escape_offboarding_sweep(support_context, monkeypatch):
    from modules import support as support_module
    engine, tid, salon, admin, support, case_id, gid = support_context
    checked, resume = Event(), Event()
    original = support_module.company_scope
    def pause_after_employee_check(connection, identity, **kwargs):
        role = original(connection, identity, **kwargs)
        checked.set()
        assert resume.wait(10)
        return role
    monkeypatch.setattr(support_module, "company_scope", pause_after_employee_check)
    staff_id = next(s["id"] for s in admin.get("/v1/company/staff").json()["items"] if s["display_name"] == "Grant-Owner")
    with ThreadPoolExecutor(max_workers=2) as executor:
        request = executor.submit(support.post, f"/v1/company/cases/{case_id}/grants", json={"justification": "Concurrent request"})
        assert checked.wait(5)
        offboard = executor.submit(admin.post, f"/v1/company/staff/{staff_id}/deactivate")
        deadline = time.monotonic() + 3
        try:
            while not offboard.done() and time.monotonic() < deadline:
                with engine.connect() as observer:
                    if observer.execute(text("SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE pid <> pg_backend_pid() AND wait_event_type='Lock' AND query LIKE '%UPDATE company_staff SET is_active=false%')")).scalar_one():
                        break
                time.sleep(0.02)
        finally:
            resume.set()
        issued = request.result(timeout=10)
        assert issued.status_code == 201, issued.text
        assert offboard.result(timeout=10).status_code == 200
    with engine.connect() as conn:
        state = conn.execute(text("SELECT status FROM support_access_grants WHERE id=:g"), {"g": issued.json()["id"]}).scalar_one()
    assert state == "revoked"
