"""Tests for signed callbacks, retries and dead letters (TASK-030, FR-043/FR-047).

AC-1: Signed callback account mapping and duplicate/out-of-order state handled.
AC-2: Unknown acceptance reconciled; crash/retry never blindly sends twice.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.callbacks import (
    CallbackModuleError,
    claim_due_retries,
    ingest_callback,
    queue_retry,
    reclaim_stale_sending,
    sign_callback_body,
    verify_callback_signature,
)
from modules.communications.outbox import (
    dispatch_claimed_retries,
    dispatch_due_communications,
    record_booking_communication,
)
from test_clients import _salon, _client, _teardown
from test_communications import FailingAdapter, RecordingAdapter, _booked_appointment, _client_email

SECRET = "test-webhook-secret-0123456789abcdef"


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _scoped(engine, tenant_id):
    conn = engine.connect()
    conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
    return conn


def _seed_secret(conn, tenant_id, channel="email"):
    conn.execute(
        text("""INSERT INTO provider_webhook_secrets (tenant_id, channel, secret)
                VALUES (:t, :c, :s) ON CONFLICT DO NOTHING"""),
        {"t": tenant_id, "c": channel, "s": SECRET})


def _recorded_email(engine, client, tenant):
    _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
    email = _client_email(engine, tenant["tenant_id"], client_id)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                     {"t": tenant["tenant_id"]})
        row = record_booking_communication(
            connection=conn, tenant_id=tenant["tenant_id"],
            appointment_id=appt_id, client_id=client_id,
            contact_value=email, channel="email",
            purpose="booking_confirmation", entity_version=1)
        if row is None:
            row = conn.execute(text("""SELECT id::text AS id
                FROM communications WHERE tenant_id=:t AND source_id=:a
                  AND purpose='booking_confirmation'"""),
                {"t": tenant["tenant_id"], "a": appt_id}).mappings().one()
        dispatch_due_communications(
            connection=conn, tenant_id=tenant["tenant_id"],
            adapter=RecordingAdapter())
        comm_id = conn.execute(text("""SELECT id::text FROM communications
            WHERE tenant_id=:t AND source_id=:a AND purpose='booking_confirmation'"""),
            {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
    return comm_id


def test_callback_ac1_signed_delivery_advances_state(database_url):
    """AC-1: a signed delivered event moves dispatched -> delivered."""
    issuer = "https://identity.example.invalid/cb-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            comm_id = conn.execute(
                text("SELECT id::text FROM communications WHERE tenant_id=:t AND source_id=:a AND channel='email' LIMIT 1"),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
            conn.execute(text("UPDATE communications SET state='dispatched' WHERE tenant_id=:t AND id=:id"),
                         {"t": tenant["tenant_id"], "id": comm_id})
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            _seed_secret(conn, tenant["tenant_id"])
            body = b'{"event":"delivered"}'
            out = ingest_callback(
                conn, tenant_id=tenant["tenant_id"], channel="email",
                raw_body=body, signature=sign_callback_body(SECRET, body),
                event="delivered", provider_event_id="evt-001",
                communication_id=comm_id)
            assert out["status"] == "applied" and out["state"] == "delivered"
            state = conn.execute(
                text("SELECT state FROM communications WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": comm_id}).scalar_one()
            assert state == "delivered"
            conn.commit()
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_callback_ac1_bad_signature_and_duplicates(database_url):
    """AC-1: bad signatures are rejected; duplicate events converge silently."""
    issuer = "https://identity.example.invalid/cb-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        comm_id = _recorded_email(engine, client, tenant)
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            _seed_secret(conn, tenant["tenant_id"])
            body = b'{"event":"delivered"}'
            with pytest.raises(CallbackModuleError):
                ingest_callback(
                    conn, tenant_id=tenant["tenant_id"], channel="email",
                    raw_body=body, signature="0" * 64,
                    event="delivered", provider_event_id="evt-001",
                    communication_id=comm_id)
            assert verify_callback_signature(SECRET, body, "not-hex") is False
            first = ingest_callback(
                conn, tenant_id=tenant["tenant_id"], channel="email",
                raw_body=body, signature=sign_callback_body(SECRET, body),
                event="delivered", provider_event_id="evt-001",
                communication_id=comm_id)
            assert first["status"] == "applied"
            version = conn.execute(
                text("SELECT version FROM communications WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": comm_id}).scalar_one()
            second = ingest_callback(
                conn, tenant_id=tenant["tenant_id"], channel="email",
                raw_body=body, signature=sign_callback_body(SECRET, body),
                event="delivered", provider_event_id="evt-001",
                communication_id=comm_id)
            assert second["status"] == "duplicate"
            again = conn.execute(
                text("SELECT version, state FROM communications WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": comm_id}).mappings().one()
            assert again["version"] == version and again["state"] == "delivered"
            conn.commit()
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_callback_ac1_channel_mapping_rejects_cross_channel_event(database_url):
    """A valid account signature cannot map an email event as an SMS event."""
    issuer = "https://identity.example.invalid/cb-channel-map"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, _, appt_id, _ = _booked_appointment(client, tenant)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            comm_id = conn.execute(text("""SELECT id::text FROM communications
                WHERE tenant_id=:t AND source_id=:a AND channel='email' LIMIT 1"""),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
            conn.execute(text("UPDATE communications SET state='dispatched' WHERE tenant_id=:t AND id=:id"),
                         {"t": tenant["tenant_id"], "id": comm_id})
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            _seed_secret(conn, tenant["tenant_id"], channel="sms")
            body = b"{}"
            with pytest.raises(CallbackModuleError, match="channel"):
                ingest_callback(
                    conn, tenant_id=tenant["tenant_id"], channel="sms",
                    raw_body=body, signature=sign_callback_body(SECRET, body),
                    event="delivered", provider_event_id="evt-cross-channel",
                    communication_id=comm_id)
            state = conn.execute(
                text("SELECT state FROM communications WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": comm_id}).scalar_one()
            assert state == "dispatched"
            conn.rollback()
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_callback_ac1_out_of_order_and_late_failure(database_url):
    """AC-1: delivered-before-accepted converges delivered; late failure loses."""
    issuer = "https://identity.example.invalid/cb-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        comm_id = _recorded_email(engine, client, tenant)
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            _seed_secret(conn, tenant["tenant_id"])
            body = b"{}"
            sig = sign_callback_body(SECRET, body)
            assert ingest_callback(
                conn, tenant_id=tenant["tenant_id"], channel="email",
                raw_body=body, signature=sig, event="delivered",
                provider_event_id="evt-100",
                communication_id=comm_id)["state"] == "delivered"
            late = ingest_callback(
                conn, tenant_id=tenant["tenant_id"], channel="email",
                raw_body=body, signature=sig, event="accepted",
                provider_event_id="evt-099",
                communication_id=comm_id)
            assert late["state"] == "delivered"
            doomed = ingest_callback(
                conn, tenant_id=tenant["tenant_id"], channel="email",
                raw_body=body, signature=sig, event="failed",
                provider_event_id="evt-101",
                communication_id=comm_id)
            assert doomed["state"] == "delivered"
            conn.commit()
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_callback_ac2_unknown_reference_reconciles_to_dead_letter(database_url):
    """AC-2: an event for nothing known becomes a dead letter, never delivery."""
    issuer = "https://identity.example.invalid/cb-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _booked_appointment(client, tenant)
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            _seed_secret(conn, tenant["tenant_id"])
            body = b"{}"
            sig = sign_callback_body(SECRET, body)
            out = ingest_callback(
                conn, tenant_id=tenant["tenant_id"], channel="email",
                raw_body=body, signature=sig, event="accepted",
                provider_event_id="evt-ghost-1", provider_ref="ghost-ref-1")
            assert out["status"] == "reconciled"
            # A redelivery of the same unknown event converges on the one row.
            again = ingest_callback(
                conn, tenant_id=tenant["tenant_id"], channel="email",
                raw_body=body, signature=sig, event="accepted",
                provider_event_id="evt-ghost-1", provider_ref="ghost-ref-1")
            assert again["status"] == "reconciled"
            rows = conn.execute(
                text("""SELECT reason, attempts FROM dead_letters
                        WHERE tenant_id=:t AND resolved_at IS NULL"""),
                {"t": tenant["tenant_id"]}).mappings().all()
            assert len(rows) == 1 and rows[0]["reason"] == "unknown_reference"
            conn.commit()
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_callback_ac2_retry_backoff_then_worker_send(database_url):
    """AC-2: failure backs off; the worker's claimed send delivers once."""
    issuer = "https://identity.example.invalid/cb-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, _, appt_id, _ = _booked_appointment(client, tenant)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            # Booking creation already wrote its confirmation in the booking
            # transaction; locate that outbox row rather than creating a duplicate.
            comm_id = conn.execute(text("""SELECT id::text FROM communications
                WHERE tenant_id=:t AND source_id=:a AND channel='email'
                  AND purpose='booking_confirmation' ORDER BY created_at LIMIT 1"""),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
            conn.execute(text("""UPDATE communications SET state='skipped_stale'
                WHERE tenant_id=:t AND source_id=:a AND id<>:id AND state='recorded'"""),
                {"t": tenant["tenant_id"], "a": appt_id, "id": comm_id})
            before = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"],
                adapter=FailingAdapter())
            assert before["retry_scheduled"] == 1
        # Backoff has not elapsed: the worker claims nothing.
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            assert claim_due_retries(connection=conn,
                                     tenant_id=tenant["tenant_id"]) == []
        # After the backoff the worker claims exactly one attempt and sends it.
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_claimed_retries(
                connection=conn, tenant_id=tenant["tenant_id"],
                adapter=RecordingAdapter(),
                now=datetime.now(timezone.utc) + timedelta(minutes=10))
        assert summary["dispatched"] == 1
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            states = conn.execute(
                text("SELECT state FROM dispatch_attempts WHERE tenant_id=:t ORDER BY attempt"),
                {"t": tenant["tenant_id"]}).scalars().all()
            comm_state = conn.execute(
                text("SELECT state FROM communications WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": comm_id}).scalar_one()
        assert states == ["succeeded"]
        assert comm_state == "dispatched"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_callback_ac2_exhaustion_dead_letters_and_manual_retry(database_url):
    """AC-2: five failures exhaust; the dead letter requeues one fresh attempt."""
    issuer = "https://identity.example.invalid/cb-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, _, appt_id, _ = _booked_appointment(client, tenant)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            # Booking creation already wrote its confirmation in the booking
            # transaction; locate the existing outbox row rather than adding another.
            comm_id = conn.execute(text("""SELECT id::text FROM communications
                WHERE tenant_id=:t AND source_id=:a AND channel='email'
                  AND purpose='booking_confirmation' ORDER BY created_at LIMIT 1"""),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
            conn.execute(text("""UPDATE communications SET state='skipped_stale'
                WHERE tenant_id=:t AND source_id=:a AND id<>:id AND state='recorded'"""),
                {"t": tenant["tenant_id"], "a": appt_id, "id": comm_id})
            dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"],
                adapter=FailingAdapter())
        moment = datetime.now(timezone.utc)
        for _ in range(6):
            moment = moment + timedelta(hours=1)
            with engine.begin() as conn:
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                             {"t": tenant["tenant_id"]})
                dispatch_claimed_retries(
                    connection=conn, tenant_id=tenant["tenant_id"],
                    adapter=FailingAdapter(), now=moment)
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            comm = conn.execute(
                text("SELECT id::text AS id, state FROM communications WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": comm_id}).mappings().one()
            letters = conn.execute(
                text("""SELECT reason, attempts, resolved_at FROM dead_letters
                        WHERE tenant_id=:t"""),
                {"t": tenant["tenant_id"]}).mappings().all()
        assert comm["state"] == "failed"
        assert len(letters) == 1 and letters[0]["reason"] == "exhausted_retries"
        assert letters[0]["attempts"] == 5 and letters[0]["resolved_at"] is None
        # Manual requeue from the route: audited there, one fresh attempt here.
        retried = client.post(
            f"/v1/tenants/{tenant['tenant_id']}/dead-letters/{comm['id']}/retry")
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "retry_scheduled"
        listed = client.get(f"/v1/tenants/{tenant['tenant_id']}/dead-letters")
        assert listed.status_code == 200 and listed.json()["items"] == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_callback_ac2_crash_reclaim_never_double_sends(database_url):
    """AC-2: a second worker cannot steal a live claim; stale claims return."""
    issuer = "https://identity.example.invalid/cb-7"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, _, appt_id, _ = _booked_appointment(client, tenant)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            comm_id = conn.execute(text("""SELECT id::text FROM communications
                WHERE tenant_id=:t AND source_id=:a AND channel='email'
                  AND purpose='booking_confirmation' ORDER BY created_at LIMIT 1"""),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
            conn.execute(text("""UPDATE communications SET state='skipped_stale'
                WHERE tenant_id=:t AND source_id=:a AND id<>:id AND state='recorded'"""),
                {"t": tenant["tenant_id"], "a": appt_id, "id": comm_id})
            dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"],
                adapter=FailingAdapter())
            queued = queue_retry(connection=conn, tenant_id=tenant["tenant_id"],
                                 communication_id=comm_id, error="boom")
            assert queued["attempt"] == 2
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        with engine.begin() as first:
            first.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                          {"t": tenant["tenant_id"]})
            held = claim_due_retries(connection=first, tenant_id=tenant["tenant_id"], now=future)
            assert len(held) == 1
            # A live claim is invisible to the second worker and unreclaimable.
            assert claim_due_retries(connection=first, tenant_id=tenant["tenant_id"],
                                     now=future) == []
            assert reclaim_stale_sending(connection=first,
                                         tenant_id=tenant["tenant_id"]) == 0
            # Age the claim past the staleness timeout: the reaper returns it.
            first.execute(
                text("""UPDATE dispatch_attempts SET updated_at = now() - interval '10 minutes'
                        WHERE tenant_id=:t"""),
                {"t": tenant["tenant_id"]})
            assert reclaim_stale_sending(connection=first,
                                         tenant_id=tenant["tenant_id"]) == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_callback_ac2_ambiguous_acceptance_reuses_provider_idempotency_key(database_url):
    """A lost response followed by retry resolves the first provider receipt."""
    issuer = "https://identity.example.invalid/cb-ambiguous"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)

    provider_receipts = {}  # external provider idempotency store survives worker restart
    adapter_keys = []
    acceptances = {"count": 0}

    class AcceptThenTimeoutAdapter:
        def send(self, *, channel, recipient, template, sender=None,
                 unsubscribe_url=None, idempotency_key=None):
            adapter_keys.append(idempotency_key)
            if idempotency_key not in provider_receipts:
                acceptances["count"] += 1
                receipt = {"status": "accepted", "id": "provider-receipt-1"}
                provider_receipts[idempotency_key] = receipt
                raise TimeoutError("provider accepted, response was lost")
            return provider_receipts[idempotency_key]

    try:
        _, _, _, _, appt_id, _ = _booked_appointment(client, tenant)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            comm_id = conn.execute(text("""SELECT id::text FROM communications
                WHERE tenant_id=:t AND source_id=:a AND channel='email'
                  AND purpose='booking_confirmation' ORDER BY created_at LIMIT 1"""),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
            conn.execute(text("""UPDATE communications SET state='skipped_stale'
                WHERE tenant_id=:t AND source_id=:a AND id<>:id AND state='recorded'"""),
                {"t": tenant["tenant_id"], "a": appt_id, "id": comm_id})

        adapter = AcceptThenTimeoutAdapter()
        # A fresh adapter instance represents a worker process restart; only
        # the provider's idempotency store survives it.
        adapter_after_restart = AcceptThenTimeoutAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            first = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert first["retry_scheduled"] == 1
        assert len(adapter_keys) == 1, (first, adapter_keys)

        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            second = dispatch_claimed_retries(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter_after_restart,
                now=datetime.now(timezone.utc) + timedelta(minutes=10))
        assert second["dispatched"] == 1
        assert acceptances["count"] == 1
        assert (len(adapter_keys) == 2 and adapter_keys[0] is not None
                and adapter_keys[0] == adapter_keys[1])
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_callback_ac2_signed_acceptance_cancels_ambiguous_retry(database_url):
    """A signed accepted callback reconciles timeout and prevents its queued resend."""
    issuer = "https://identity.example.invalid/cb-accepted-reconcile"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)

    class AcceptThenTimeoutAdapter:
        def __init__(self):
            self.calls = 0

        def send(self, *, channel, recipient, template, sender=None,
                 unsubscribe_url=None, idempotency_key=None):
            self.calls += 1
            raise TimeoutError("provider accepted, response was lost")

    try:
        _, _, _, _, appt_id, _ = _booked_appointment(client, tenant)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            comm_id = conn.execute(text("""SELECT id::text FROM communications
                WHERE tenant_id=:t AND source_id=:a AND channel='email'
                  AND purpose='booking_confirmation' ORDER BY created_at LIMIT 1"""),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
            conn.execute(text("""UPDATE communications SET state='skipped_stale'
                WHERE tenant_id=:t AND source_id=:a AND id<>:id AND state='recorded'"""),
                {"t": tenant["tenant_id"], "a": appt_id, "id": comm_id})

        adapter = AcceptThenTimeoutAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            first = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert first["retry_scheduled"] == 1 and adapter.calls == 1

        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            _seed_secret(conn, tenant["tenant_id"], channel="email")
            body = b"{}"
            reconciled = ingest_callback(
                conn, tenant_id=tenant["tenant_id"], channel="email",
                raw_body=body, signature=sign_callback_body(SECRET, body),
                event="accepted", provider_event_id="evt-accepted-after-timeout",
                communication_id=comm_id)
            assert reconciled["state"] == "dispatched"

        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            second = dispatch_claimed_retries(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter,
                now=datetime.now(timezone.utc) + timedelta(minutes=10))
        assert second["dispatched"] == 0 and adapter.calls == 1
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            attempt_states = conn.execute(
                text("SELECT state FROM dispatch_attempts WHERE tenant_id=:t"),
                {"t": tenant["tenant_id"]}).scalars().all()
        assert attempt_states == ["superseded"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_callback_ac1_http_route_verifies_and_lists(database_url):
    """AC-1: the provider route checks signatures; staff list dead letters."""
    issuer = "https://identity.example.invalid/cb-8"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        comm_id = _recorded_email(engine, client, tenant)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            _seed_secret(conn, tenant["tenant_id"])
        payload = {"event": "delivered", "provider_event_id": "evt-http-1",
                   "communication_id": comm_id}
        raw = json.dumps(payload).encode()
        ok = client.post(f"/v1/tenants/{tenant['tenant_id']}/provider-callbacks/email",
                         content=raw,
                         headers={"Content-Type": "application/json",
                                  "X-Provider-Signature": sign_callback_body(SECRET, raw)})
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "applied"
        forged = client.post(f"/v1/tenants/{tenant['tenant_id']}/provider-callbacks/email",
                             content=raw,
                             headers={"Content-Type": "application/json",
                                      "X-Provider-Signature": "0" * 64})
        assert forged.status_code == 401
        # Unknown references reconcile over HTTP too, then list for staff.
        ghost = {"event": "accepted", "provider_event_id": "evt-ghost-9",
                 "provider_ref": "ghost-9"}
        ghost_raw = json.dumps(ghost).encode()
        missing = client.post(f"/v1/tenants/{tenant['tenant_id']}/provider-callbacks/email",
                              content=ghost_raw,
                              headers={"Content-Type": "application/json",
                                       "X-Provider-Signature": sign_callback_body(SECRET, ghost_raw)})
        assert missing.json()["status"] == "reconciled"
        listed = client.get(f"/v1/tenants/{tenant['tenant_id']}/dead-letters")
        assert listed.status_code == 200
        assert [d["reason"] for d in listed.json()["items"]] == ["unknown_reference"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
