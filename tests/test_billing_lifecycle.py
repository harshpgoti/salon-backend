"""Tests for plan changes, grace, restriction and recovery (TASK-048, FR-066/FR-068).

AC-1: Upgrade/downgrade/cancel/grace/recovery implement effective policy.
AC-2: Existing appointments/export preserved; security suspension stays independent.
"""

import hashlib
import hmac
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone, date
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_booking_holds import _bookable, _first_slot
from test_clients import _salon, _client, _teardown


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _secret(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT secret FROM billing_provider_secrets WHERE provider='stripe_test'"),
        ).scalar_one()


def _webhook(client, secret, event):
    raw = json.dumps(event).encode()
    return client.post("/v1/billing/webhooks/stripe", content=raw,
                       headers={"Content-Type": "application/json",
                                "X-Provider-Signature": hmac.new(
                                    secret.encode(), raw, hashlib.sha256).hexdigest()})


def _activate(client, engine, tid, secret, plan_code="growth", customer="cus_test_lc001"):
    started = client.post(f"/v1/tenants/{tid}/billing/checkout",
                          json={"plan_code": plan_code})
    assert started.status_code == 201, started.text
    session_ref = started.json()["checkout_url"].rsplit("/", 1)[-1]
    now = datetime.now(timezone.utc)
    out = _webhook(client, secret,
                   {"id": f"evt-lc-{uuid.uuid4().hex[:8]}", "type": "checkout.session.completed",
                    "occurred_at": now.isoformat(),
                    "data": {"tenant_id": tid, "session_ref": session_ref,
                             "customer_ref": customer, "subscription_ref": "sub_test_lc001",
                             "plan_code": plan_code,
                             "period_end": (now + timedelta(days=30)).isoformat()}})
    assert out.status_code == 200, out.text
    assert out.json()["status"] == "applied"


def _fail_payment(client, secret, customer, when):
    return _webhook(client, secret,
                    {"id": f"evt-lc-{uuid.uuid4().hex[:8]}", "type": "invoice.payment_failed",
                     "occurred_at": when.isoformat(), "data": {"customer_ref": customer}})


def _manual_booking(client, base, service, staff, client_id, day, hour, key):
    starts = (datetime.combine(day, datetime.min.time())
              + timedelta(hours=hour)).replace(tzinfo=timezone.utc)
    return client.post(f"{base}/appointments", json={
        "client_id": client_id, "staff_id": staff, "service_id": service,
        "starts_at": starts.isoformat(), "source": "staff",
        "idempotency_key": key})


def test_lifecycle_ac1_upgrade_now_downgrade_later(database_url):
    """AC-1: upgrades apply immediately; downgrades wait for period end."""
    issuer = "https://identity.example.invalid/lc-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        secret = _secret(engine)
        _activate(client, engine, tid, secret, plan_code="growth")
        up = client.post(f"/v1/tenants/{tid}/billing/subscription/plan",
                         json={"plan_code": "multi_location"})
        assert up.status_code == 200, up.text
        assert up.json()["effective"] == "immediate"
        entitlements = client.get(f"/v1/tenants/{tid}/billing/entitlements").json()
        assert entitlements["plan_code"] == "multi_location"
        assert entitlements["max_locations"] == 5
        down = client.post(f"/v1/tenants/{tid}/billing/subscription/plan",
                           json={"plan_code": "essential"})
        assert down.status_code == 200, down.text
        assert down.json()["effective"] == "period_end"
        assert down.json()["applies_at"]
        # Still on multi-location until the period ends.
        assert client.get(f"/v1/tenants/{tid}/billing/subscription").json()["plan_code"] \
            == "multi_location"
        from modules.company import run_billing_tick
        with engine.begin() as conn:
            tick = run_billing_tick(connection=conn, tenant_id=tid)
        assert tick == {"scheduled_applied": 0, "grace_expired": 0}
        with engine.begin() as conn:
            tick = run_billing_tick(
                connection=conn, tenant_id=tid,
                now=datetime.now(timezone.utc) + timedelta(days=31))
        assert tick["scheduled_applied"] == 1
        assert client.get(f"/v1/tenants/{tid}/billing/subscription").json()["plan_code"] \
            == "essential"
        assert client.get(f"/v1/tenants/{tid}/billing/entitlements").json()["max_staff"] == 5
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_lifecycle_ac1_cancel_and_grace_recovery(database_url):
    """AC-1: cancel-at-period-end flags; failed payments grace then recover."""
    issuer = "https://identity.example.invalid/lc-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    base = f"/v1/tenants/{tid}"
    try:
        secret = _secret(engine)
        _activate(client, engine, tid, secret)
        flagged = client.post(f"/v1/tenants/{tid}/billing/subscription/cancel",
                              json={"at_period_end": True})
        assert flagged.status_code == 200, flagged.text
        assert flagged.json()["cancel_at_period_end"] is True
        assert flagged.json()["status"] == "active"
        now = datetime.now(timezone.utc)
        failed = _fail_payment(client, secret, "cus_test_lc001", now)
        assert failed.json()["status"] == "applied"
        sub = client.get(f"{base}/billing/subscription").json()
        assert sub["status"] == "past_due"
        assert sub["grace_until"]
        # Grace preserves operations but pauses marketing.
        assert client.get(f"{base}/billing/entitlements").json()["marketing_allowed"] is False
        day = date.today() + timedelta(days=1)
        _, _, service, staff, client_id = _bookable(client, database_url, tenant)
        ok = _manual_booking(client, base, service, staff, client_id, day, 10, "lc-book-0001")
        assert ok.status_code == 201, ok.text
        # Recovery through a paid invoice restores everything.
        paid = _webhook(client, secret,
                        {"id": f"evt-lc-{uuid.uuid4().hex[:8]}", "type": "invoice.paid",
                         "occurred_at": (now + timedelta(hours=1)).isoformat(),
                         "data": {"customer_ref": "cus_test_lc001",
                                  "period_end": (now + timedelta(days=30)).isoformat()}})
        assert paid.json()["status"] == "applied"
        sub = client.get(f"{base}/billing/subscription").json()
        assert sub["status"] == "active" and sub["grace_until"] is None
        assert client.get(f"{base}/billing/entitlements").json()["marketing_allowed"] is True
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_lifecycle_ac2_restricted_blocks_new_but_keeps_existing(database_url):
    """AC-2: restricted stops new bookings; reads, exports and resolution go on."""
    issuer = "https://identity.example.invalid/lc-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    base = f"/v1/tenants/{tid}"
    try:
        secret = _secret(engine)
        _activate(client, engine, tid, secret, customer="cus_test_lc003")
        base_b, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        before = _manual_booking(client, base, service, staff, client_id, day, 10, "lc-book-0002")
        assert before.status_code == 201, before.text
        before_id = before.json()["appointment_id"]
        now = datetime.now(timezone.utc)
        assert _fail_payment(client, secret, "cus_test_lc003", now).json()["status"] == "applied"
        from modules.company import run_billing_tick
        with engine.begin() as conn:
            tick = run_billing_tick(connection=conn, tenant_id=tid, now=now + timedelta(days=8))
        assert tick["grace_expired"] == 1
        assert client.get(f"{base}/billing/subscription").json()["status"] == "restricted"
        # New bookings stop — staff and public alike.
        blocked = _manual_booking(client, base, service, staff, client_id, day, 12, "lc-book-0003")
        assert blocked.status_code == 402
        assert blocked.json()["detail"]["code"] == "billing_restricted"
        start = _first_slot(client, slug, service, day)
        hold = client.post(f"/v1/public/salons/{slug}/holds",
                           json={"service_id": service, "staff_id": staff,
                                 "starts_at": start, "idempotency_key": "lc-hold-0001"})
        assert hold.status_code == 201, hold.text
        public = client.post(f"/v1/public/salons/{slug}/holds/{hold.json()['hold_id']}/confirm",
                             json={"owner_token": hold.json()["owner_token"],
                                   "client_id": client_id, "idempotency_key": "lc-confirm-0001"})
        assert public.status_code == 402
        # Existing work continues: reads, reschedules, plan changes wait.
        assert client.get(f"{base}/appointments",
                          params={"date_from": day.isoformat(),
                                  "date_to": day.isoformat()}).status_code == 200
        resched = client.post(f"{base}/appointments/{before_id}/reschedule", json={
            "starts_at": (datetime.combine(day, datetime.min.time())
                          + timedelta(hours=11)).replace(tzinfo=timezone.utc).isoformat(),
            "expected_version": 1, "idempotency_key": "lc-resched-0001"})
        assert resched.status_code == 200, resched.text
        change = client.post(f"{base}/billing/subscription/plan",
                             json={"plan_code": "essential"})
        assert change.status_code == 409
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_lifecycle_ac2_suspension_is_independent(database_url):
    """AC-2: security suspension blocks bookings but never touches billing state."""
    issuer = "https://identity.example.invalid/lc-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    base = f"/v1/tenants/{tid}"
    try:
        secret = _secret(engine)
        _activate(client, engine, tid, secret, customer="cus_test_lc004")
        _, _, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("UPDATE tenant_subscriptions SET status='suspended' WHERE tenant_id=:t"),
                         {"t": tid})
        blocked = _manual_booking(client, base, service, staff, client_id, day, 10, "lc-book-0004")
        assert blocked.status_code == 403
        assert blocked.json()["detail"]["code"] == "account_suspended"
        # Reads and billing surfaces stay up; the subscription itself is intact.
        assert client.get(f"{base}/billing/subscription").json()["status"] == "suspended"
        assert client.get(f"{base}/billing/subscription").json()["plan_code"] == "growth"
        assert client.get(f"{base}/appointments",
                          params={"date_from": day.isoformat(),
                                  "date_to": day.isoformat()}).status_code == 200
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
