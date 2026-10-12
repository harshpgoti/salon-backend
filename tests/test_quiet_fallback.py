"""Tests for quiet hours, fallback and cost-safe dispatch (TASK-032, FR-046/FR-047).

AC-1: Quiet hours/expiry/preferred channel/current entitlement are enforced.
AC-2: Separately consented fallback only after known failure and budget reservation.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.outbox import (
    dispatch_due_communications,
    record_booking_communication,
)
from modules.communications.senders import register_sender, verify_sender
from test_clients import _salon, _client, _teardown
from test_communications import FailingAdapter, RecordingAdapter, _booked_appointment

# Fixed clock: 2026-10-13 02:00 UTC == 22:00 Toronto (inside 21:00-08:00).
QUIET_NOW = datetime(2026, 10, 13, 2, 0, tzinfo=timezone.utc)
# 14:00 UTC == 10:00 Toronto (outside quiet).
OPEN_NOW = datetime(2026, 10, 13, 14, 0, tzinfo=timezone.utc)


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


def _quiet_on(client, tid, start="21:00", end="08:00"):
    resp = client.put(f"/v1/tenants/{tid}/messaging/settings",
                      json={"quiet_start": start, "quiet_end": end,
                            "emergency_stop": False,
                            "max_per_recipient_per_day": None})
    assert resp.status_code == 200, resp.text


def _client_with_phone(client, base, phone="+14165550101"):
    created = client.post(base, json={
        "name": "Quiet Client",
        "contacts": [{"kind": "phone", "value": phone, "is_primary": True},
                     {"kind": "email", "value": "quiet@example.com", "is_primary": False}],
    })
    assert created.status_code == 201, created.text
    return created.json()


def test_quiet_ac1_defers_then_sends_after_window(database_url):
    """AC-1: due-in-quiet defers to the window end, then sends."""
    issuer = "https://identity.example.invalid/quiet-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        _quiet_on(client, tid)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            record_booking_communication(
                connection=conn, tenant_id=tid, appointment_id=appt_id,
                client_id=client_id, contact_value="q@example.invalid", channel="email",
                purpose="appointment_reminder_24h", entity_version=1)
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tid, adapter=RecordingAdapter(), now=QUIET_NOW)
        assert summary["deferred"] == 1 and summary["dispatched"] == 0
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            row = conn.execute(
                text("SELECT state, scheduled_at FROM communications WHERE tenant_id=:t"),
                {"t": tid}).mappings().one()
        assert row["state"] == "recorded"
        # 08:00 Toronto == 12:00 UTC on the same date.
        assert row["scheduled_at"].isoformat() == "2026-10-13T12:00:00+00:00"
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            later = dispatch_due_communications(
                connection=conn, tenant_id=tid, adapter=RecordingAdapter(),
                now=datetime(2026, 10, 13, 13, 0, tzinfo=timezone.utc))
        assert later["dispatched"] == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_quiet_ac1_deferral_past_usefulness_expires(database_url):
    """AC-1: a deferral past the reminder window expires instead."""
    issuer = "https://identity.example.invalid/quiet-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        _quiet_on(client, tid)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            record_booking_communication(
                connection=conn, tenant_id=tid, appointment_id=appt_id,
                client_id=client_id, contact_value="q@example.invalid", channel="email",
                purpose="appointment_reminder_2h", entity_version=1,
                # Expires 02:30 UTC — still deep inside quiet.
                expires_at=datetime(2026, 10, 13, 2, 30, tzinfo=timezone.utc))
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tid, adapter=RecordingAdapter(), now=QUIET_NOW)
        assert summary["skipped_expired"] == 1
        assert summary["deferred"] == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_quiet_ac1_cap_and_emergency_and_prefs(database_url):
    """AC-1: recipient cap defers a day; emergency defers all; prefs validate."""
    issuer = "https://identity.example.invalid/quiet-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    base = f"/v1/tenants/{tid}"
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        capped = client.put(f"/v1/tenants/{tid}/messaging/settings",
                            json={"quiet_start": None, "quiet_end": None,
                                  "emergency_stop": False,
                                  "max_per_recipient_per_day": 1})
        assert capped.status_code == 200, capped.text
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            for purpose in ("cap_1", "cap_2"):
                record_booking_communication(
                    connection=conn, tenant_id=tid, appointment_id=appt_id,
                    client_id=client_id, contact_value="q@example.invalid",
                    channel="email", purpose=purpose, entity_version=1)
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tid, adapter=RecordingAdapter())
        assert summary["dispatched"] == 1 and summary["deferred"] == 1
        stopped = client.put(f"/v1/tenants/{tid}/messaging/settings",
                             json={"quiet_start": None, "quiet_end": None,
                                   "emergency_stop": True,
                                   "max_per_recipient_per_day": None})
        assert stopped.status_code == 200
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            record_booking_communication(
                connection=conn, tenant_id=tid, appointment_id=appt_id,
                client_id=client_id, contact_value="q@example.invalid",
                channel="email", purpose="cap_3", entity_version=1)
            held = dispatch_due_communications(
                connection=conn, tenant_id=tid, adapter=RecordingAdapter())
        assert held["deferred"] == 1 and held["dispatched"] == 0
        # Preference validation: self-fallback and bad channels rejected.
        prefs = client.put(f"{base}/clients/{client_id}/channel-prefs", json={
            "purpose": "default", "primary_channel": "sms", "fallback_channel": "email"})
        assert prefs.status_code == 200, prefs.text
        assert prefs.json()["fallback_channel"] == "email"
        bad = client.put(f"{base}/clients/{client_id}/channel-prefs", json={
            "purpose": "default", "primary_channel": "sms", "fallback_channel": "sms"})
        assert bad.status_code == 422
        listed = client.get(f"{base}/clients/{client_id}/channel-prefs").json()["items"]
        assert listed[0]["primary_channel"] == "sms"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def _exhaust_email(engine, tid, client, client_id, appt_id, purpose="fb_1"):
    from datetime import timedelta
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        record_booking_communication(
            connection=conn, tenant_id=tid, appointment_id=appt_id,
            client_id=client_id, contact_value="+14165550101", channel="email",
            purpose=purpose, entity_version=1)
        first = dispatch_due_communications(
            connection=conn, tenant_id=tid, adapter=FailingAdapter(), now=OPEN_NOW)
    assert first["retry_scheduled"] == 1
    moment = OPEN_NOW
    totals = {"failed": 0, "fallback_created": 0}
    for _ in range(6):
        moment = moment + timedelta(hours=1)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            from modules.communications.outbox import dispatch_claimed_retries
            round_summary = dispatch_claimed_retries(
                connection=conn, tenant_id=tid, adapter=FailingAdapter(), now=moment)
            totals["failed"] += round_summary["failed"]
            totals["fallback_created"] += round_summary["fallback_created"]
    return totals


def test_quiet_ac2_fallback_only_after_exhaustion_with_eligibility(database_url):
    """AC-2: exhausted email falls back to consented SMS; then it sends."""
    issuer = "https://identity.example.invalid/quiet-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    base = f"/v1/tenants/{tid}/clients"
    try:
        _, _, _, _, appt_id, _ = _booked_appointment(client, tenant)
        row = _client_with_phone(client, base)
        client_id = row["id"]
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            created = register_sender(conn, tenant_id=tid, label="Main",
                                      sender_identity="+14165550199")
            verify_sender(conn, tenant_id=tid, sender_id=created["id"],
                          code=created["verification_code"])
        prefs = client.put(f"/v1/tenants/{tid}/clients/{client_id}/channel-prefs", json={
            "purpose": "default", "primary_channel": "email", "fallback_channel": "sms"})
        assert prefs.status_code == 200, prefs.text
        # Transactional purpose: no consent needed on either channel.
        final = _exhaust_email(engine, tid, client, client_id, appt_id)
        assert final["failed"] == 1
        assert final["fallback_created"] == 1
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            fallback = conn.execute(
                text("""SELECT channel, state FROM communications
                        WHERE tenant_id=:t AND channel='sms'"""),
                {"t": tid}).mappings().one()
        assert fallback["state"] == "recorded"
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            # The fallback was scheduled at exhaustion time; redeliver after it.
            redeliver = dispatch_due_communications(
                connection=conn, tenant_id=tid, adapter=adapter,
                now=OPEN_NOW + timedelta(hours=6))
        assert redeliver["dispatched"] == 1
        assert adapter.records[0]["channel"] == "sms"
        assert adapter.records[0]["sender"] == "+14165550199"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_quiet_ac2_timeout_retries_without_fallback(database_url):
    """AC-2: an ambiguous timeout rides retries; no second channel fires."""
    issuer = "https://identity.example.invalid/quiet-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    base = f"/v1/tenants/{tid}/clients"
    try:
        _, _, _, _, appt_id, _ = _booked_appointment(client, tenant)
        row = _client_with_phone(client, base)
        prefs = client.put(f"/v1/tenants/{tid}/clients/{row['id']}/channel-prefs", json={
            "purpose": "default", "primary_channel": "email", "fallback_channel": "sms"})
        assert prefs.status_code == 200, prefs.text

        class TimeoutAdapter:
            def send(self, *, channel, recipient, template, sender=None):
                raise TimeoutError("provider timed out ambiguously")

        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            record_booking_communication(
                connection=conn, tenant_id=tid, appointment_id=appt_id,
                client_id=row["id"], contact_value="+14165550101", channel="email",
                purpose="fb_timeout", entity_version=1)
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tid, adapter=TimeoutAdapter(), now=OPEN_NOW)
        assert summary["retry_scheduled"] == 1
        assert summary["fallback_created"] == 0
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            channels = conn.execute(
                text("SELECT DISTINCT channel FROM communications WHERE tenant_id=:t"),
                {"t": tid}).scalars().all()
        assert channels == ["email"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_quiet_ac2_suppressed_fallback_and_blocked_manual_retry(database_url):
    """AC-2: ineligible fallback stays unsent; opted-out manual retry refused."""
    issuer = "https://identity.example.invalid/quiet-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    base = f"/v1/tenants/{tid}/clients"
    try:
        _, _, _, _, appt_id, _ = _booked_appointment(client, tenant)
        row = _client_with_phone(client, base)
        prefs = client.put(f"/v1/tenants/{tid}/clients/{row['id']}/channel-prefs", json={
            "purpose": "default", "primary_channel": "email", "fallback_channel": "sms"})
        assert prefs.status_code == 200, prefs.text
        conn = _scoped(engine, tid)
        try:
            from modules.communications.consent import record_suppression
            record_suppression(conn, tenant_id=tid, client_id=row["id"],
                               contact_value="+14165550101", channel="sms",
                               reason="stop")
            conn.commit()
        finally:
            conn.close()
        final = _exhaust_email(engine, tid, client, row["id"], appt_id, purpose="fb_blocked")
        assert final["failed"] == 1
        assert final["fallback_created"] == 0
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            comm_id = conn.execute(
                text("""SELECT id::text AS id FROM communications
                        WHERE tenant_id=:t AND purpose='fb_blocked'"""),
                {"t": tid}).scalar_one()
        # Manual retry is refused once the contact opts out of that channel.
        conn = _scoped(engine, tid)
        try:
            from modules.communications.consent import record_suppression as suppress
            suppress(conn, tenant_id=tid, client_id=row["id"],
                     contact_value="+14165550101", channel="email", reason="stop")
            conn.commit()
        finally:
            conn.close()
        refused = client.post(f"/v1/tenants/{tid}/dead-letters/{comm_id}/retry")
        assert refused.status_code == 422
        assert refused.json()["detail"]["code"] == "retry_rejected"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
