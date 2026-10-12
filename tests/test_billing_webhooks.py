"""Tests for billing webhook reconciliation and entitlements (TASK-047, FR-067).

AC-1: Valid signed deduplicated out-of-order events converge to provider facts.
AC-2: Replay/missed callbacks/refund/dispute cases do not overgrant.
"""

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
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


def _post(client, tenant_id, secret, event):
    raw = json.dumps(event).encode()
    return client.post("/v1/billing/webhooks/stripe", content=raw,
                       headers={"Content-Type": "application/json",
                                "X-Provider-Signature": hmac.new(
                                    secret.encode(), raw, hashlib.sha256).hexdigest()})


def _sub(client, tenant_id):
    return client.get(f"/v1/tenants/{tenant_id}/billing/subscription").json()


def _entitlements(client, tenant_id):
    return client.get(f"/v1/tenants/{tenant_id}/billing/entitlements").json()


def test_billing_ac1_checkout_webhook_activates(database_url):
    """AC-1: a signed checkout completion converges trial -> active + entitlements."""
    issuer = "https://identity.example.invalid/bw-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        secret = _secret(engine)
        assert _sub(client, tid)["status"] == "trial"
        started = client.post(f"/v1/tenants/{tid}/billing/checkout",
                              json={"plan_code": "growth"})
        session_url = started.json()["checkout_url"]
        session_ref = session_url.rsplit("/", 1)[-1]
        now = datetime.now(timezone.utc)
        event = {"id": "evt-bw-001", "type": "checkout.session.completed",
                 "occurred_at": now.isoformat(),
                 "data": {"tenant_id": tid, "session_ref": session_ref,
                          "customer_ref": "cus_test_bw001", "subscription_ref": "sub_test_bw001",
                          "plan_code": "growth",
                          "period_end": (now + timedelta(days=30)).isoformat()}}
        applied = _post(client, tid, secret, event)
        assert applied.status_code == 200, applied.text
        assert applied.json()["status"] == "applied"
        sub = _sub(client, tid)
        assert sub["status"] == "active" and sub["plan_code"] == "growth"
        assert sub["stripe_customer_ref"] == "cus_test_bw001"
        entitlements = _entitlements(client, tid)
        assert entitlements["plan_code"] == "growth"
        assert entitlements["max_staff"] == 15
        assert entitlements["marketing_allowed"] is True
        # The hosted session is complete; the trial is gone.
        reread = client.get(f"/v1/tenants/{tid}/billing/checkout/"
                            f"{started.json()['session_id']}")
        assert reread.json()["status"] == "complete"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_billing_ac1_duplicates_and_stale_events_converge(database_url):
    """AC-1: replays dedupe; older out-of-order events never regress state."""
    issuer = "https://identity.example.invalid/bw-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        secret = _secret(engine)
        started = client.post(f"/v1/tenants/{tid}/billing/checkout",
                              json={"plan_code": "growth"})
        session_ref = started.json()["checkout_url"].rsplit("/", 1)[-1]
        base = datetime.now(timezone.utc)
        first = {"id": "evt-bw-101", "type": "checkout.session.completed",
                 "occurred_at": base.isoformat(),
                 "data": {"tenant_id": tid, "session_ref": session_ref,
                          "customer_ref": "cus_test_bw101", "subscription_ref": "sub_test_bw101",
                          "plan_code": "growth",
                          "period_end": (base + timedelta(days=30)).isoformat()}}
        assert _post(client, tid, secret, first).json()["status"] == "applied"
        # Exact redelivery converges without touching state.
        replay = _post(client, tid, secret, first)
        assert replay.json()["status"] == "duplicate"
        assert _sub(client, tid)["status"] == "active"
        # A newer payment failure moves to past_due...
        failed = {"id": "evt-bw-102", "type": "invoice.payment_failed",
                  "occurred_at": (base + timedelta(hours=1)).isoformat(),
                  "data": {"customer_ref": "cus_test_bw101"}}
        assert _post(client, tid, secret, failed).json()["status"] == "applied"
        assert _sub(client, tid)["status"] == "past_due"
        # ...but an older paid invoice arriving late must not resurrect active.
        late_paid = {"id": "evt-bw-103", "type": "invoice.paid",
                     "occurred_at": base.isoformat(),
                     "data": {"customer_ref": "cus_test_bw101",
                              "period_end": (base + timedelta(days=30)).isoformat()}}
        assert _post(client, tid, secret, late_paid).json()["status"] == "stale"
        assert _sub(client, tid)["status"] == "past_due"
        # A newer payment recovers honestly.
        recovery = {"id": "evt-bw-104", "type": "invoice.paid",
                    "occurred_at": (base + timedelta(hours=2)).isoformat(),
                    "data": {"customer_ref": "cus_test_bw101",
                             "period_end": (base + timedelta(days=60)).isoformat()}}
        assert _post(client, tid, secret, recovery).json()["status"] == "applied"
        assert _sub(client, tid)["status"] == "active"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_billing_ac2_refund_dispute_and_auth_change_nothing(database_url):
    """AC-2: evidence-only events record without overgranting."""
    issuer = "https://identity.example.invalid/bw-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        secret = _secret(engine)
        started = client.post(f"/v1/tenants/{tid}/billing/checkout",
                              json={"plan_code": "essential"})
        session_ref = started.json()["checkout_url"].rsplit("/", 1)[-1]
        base = datetime.now(timezone.utc)
        activate = {"id": "evt-bw-201", "type": "checkout.session.completed",
                    "occurred_at": base.isoformat(),
                    "data": {"tenant_id": tid, "session_ref": session_ref,
                             "customer_ref": "cus_test_bw201", "subscription_ref": "sub_test_bw201",
                             "plan_code": "essential",
                             "period_end": (base + timedelta(days=30)).isoformat()}}
        assert _post(client, tid, secret, activate).json()["status"] == "applied"
        for event_id, event_type in (("evt-bw-202", "charge.refunded"),
                                     ("evt-bw-203", "dispute.created"),
                                     ("evt-bw-204", "payment.requires_action")):
            out = _post(client, tid, secret,
                        {"id": event_id, "type": event_type,
                         "occurred_at": (base + timedelta(minutes=5)).isoformat(),
                         "data": {"customer_ref": "cus_test_bw201"}})
            assert out.json()["status"] == "applied"
            assert out.json()["state_changed"] is False
        assert _sub(client, tid)["status"] == "active"
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            count = conn.execute(text("SELECT count(*) FROM billing_events WHERE tenant_id=:t"),
                                 {"t": tid}).scalar_one()
        assert count == 4
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_billing_ac1_rejects_forgeries_and_strangers(database_url):
    """AC-1: bad signatures, unknown customers and types are rejected, unstored."""
    issuer = "https://identity.example.invalid/bw-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        secret = _secret(engine)
        event = {"id": "evt-bw-301", "type": "invoice.paid",
                 "occurred_at": datetime.now(timezone.utc).isoformat(),
                 "data": {"customer_ref": "cus_test_bw301"}}
        raw = json.dumps(event).encode()
        forged = client.post("/v1/billing/webhooks/stripe", content=raw,
                             headers={"Content-Type": "application/json",
                                      "X-Provider-Signature": "0" * 64})
        assert forged.status_code == 401
        stranger = _post(client, tid, secret, event)
        assert stranger.status_code == 422
        mistyped = _post(client, tid, secret, {**event, "id": "evt-bw-302",
                                               "type": "invoice.exploded"})
        assert mistyped.status_code == 422
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            count = conn.execute(text("SELECT count(*) FROM billing_events WHERE tenant_id=:t"),
                                 {"t": tid}).scalar_one()
        assert count == 0
        assert _sub(client, tid)["status"] == "trial"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
