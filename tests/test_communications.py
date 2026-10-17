"""Tests for the durable communication outbox (TASK-027, FR-039/FR-042).

AC-1: Confirm/change/cancel/reminders versioned and dispatched after commit
AC-2: Provider failure leaves booking intact; stale/expired reminders skip
"""

import os
import sys
from datetime import date, timedelta, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.outbox import (
    dispatch_due_communications,
)
from modules.worker.communications import dispatch_tenant_once
from test_clients import _salon, _client, _teardown
from test_booking_holds import _bookable, _first_slot


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


class RecordingAdapter:
    """Synthetic-only sink matching the app's RecordingDeliveryAdapter."""

    def __init__(self):
        self.records = []

    def send(self, *, channel, recipient, template, sender=None, unsubscribe_url=None,
             idempotency_key=None, template_category=None, template_locale=None,
             message_body=None):
        record = {"channel": channel, "recipient": recipient, "template": template,
                  "sender": sender, "template_category": template_category,
                  "template_locale": template_locale, "message_body": message_body}
        if unsubscribe_url is not None:
            record["unsubscribe_url"] = unsubscribe_url
        self.records.append(record)
        return {"status": "recorded", "id": f"recorded-{len(self.records)}"}


class FailingAdapter:
    """Adapter that always raises — simulates a provider outage."""

    def send(self, *, channel, recipient, template, sender=None, unsubscribe_url=None,
             idempotency_key=None, template_category=None, template_locale=None,
             message_body=None):
        raise RuntimeError("provider unavailable")


def _booked_appointment(client, tenant, *, include_trace=False):
    """Create a salon with a client and one confirmed appointment; return ids."""
    base = f"/v1/tenants/{tenant['tenant_id']}"
    loc = tenant["location_id"]
    # Most outbox tests target consent, budget, callback or routing behavior,
    # so make their quiet-hours choice explicit. Product-default quiet hours
    # are covered by the dedicated regression in test_quiet_fallback.py.
    quiet = client.put(f"{base}/messaging/settings", json={
        "quiet_start": None, "quiet_end": None,
        "emergency_stop": False, "max_per_recipient_per_day": None})
    assert quiet.status_code == 200, quiet.text
    day = date.today() + timedelta(days=3)  # far enough that 24h/2h reminders are not due
    client.put(f"{base}/locations/{loc}/hours", json={
        "intervals": [{"weekday": day.weekday(), "opens_at": "09:00", "closes_at": "17:00"}]})
    service = client.post(f"{base}/services", json={
        "name": "Cut", "duration_minutes": 60, "price_minor": 6000}).json()["id"]
    staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
    client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [loc]})
    client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
    client.post(f"{base}/staff/{staff}/shifts", json={
        "location_id": loc, "weekday": day.weekday(), "starts_at": "09:00", "ends_at": "17:00"})
    client_row = client.post(f"{base}/clients", json={"name": "Client One", "contacts": [
        {"kind": "email", "value": "client.one@example.invalid"},
    ]}).json()
    client_id = client_row["id"]
    starts = datetime.combine(day, time(10), ZoneInfo("America/Toronto"))
    response = client.post(f"{base}/appointments", json={
        "client_id": client_id, "staff_id": staff, "service_id": service,
        "starts_at": starts.isoformat(), "source": "staff",
        "idempotency_key": "comm-test-key-0001"})
    assert response.status_code == 201, response.text
    result = (loc, service, staff, client_id, response.json()["appointment_id"], starts)
    return (*result, response.headers["X-Trace-ID"]) if include_trace else result


def _outbox(engine, tenant_id, appointment_id):
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                           {"tenant": tenant_id})
        return connection.execute(text("""SELECT purpose, expected_entity_version, state,
                scheduled_at, expires_at, skip_reason FROM communications
                WHERE tenant_id=:tenant AND source_id=:appointment ORDER BY purpose"""),
            {"tenant": tenant_id, "appointment": appointment_id}).mappings().all()


def _client_email(engine, tenant_id, client_id):
    """Return the client's current primary email under tenant scope."""
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                           {"tenant": tenant_id})
        return connection.execute(text("""SELECT normalized_value FROM client_contacts
            WHERE tenant_id=:tenant AND client_id=:client AND kind='email'
            ORDER BY is_primary DESC, created_at LIMIT 1"""),
            {"tenant": tenant_id, "client": client_id}).scalar_one()


def test_booking_http_commands_record_versioned_email_after_commit(database_url):
    """AC-1: created, moved, and cancelled bookings persist versioned mail intents."""
    issuer = "https://identity.example.invalid/comm-http"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        _, _, _, _, appointment_id, starts = _booked_appointment(client, tenant)
        initial = _outbox(engine, tenant["tenant_id"], appointment_id)
        assert {row["purpose"] for row in initial} == {
            "booking_confirmation", "appointment_reminder_24h", "appointment_reminder_2h"}
        assert {row["expected_entity_version"] for row in initial} == {1}
        assert all(row["state"] == "recorded" for row in initial)
        reminders = {row["purpose"]: row for row in initial if row["purpose"].startswith("appointment_reminder_")}
        assert reminders["appointment_reminder_24h"]["scheduled_at"] == starts - timedelta(hours=24)
        assert reminders["appointment_reminder_2h"]["scheduled_at"] == starts - timedelta(hours=2)

        moved = starts + timedelta(days=7)
        rescheduled = client.post(f"{base}/appointments/{appointment_id}/reschedule", json={
            "starts_at": moved.isoformat(), "expected_version": 1,
            "idempotency_key": "comm-http-reschedule-1"})
        assert rescheduled.status_code == 200, rescheduled.text
        cancelled = client.post(f"{base}/appointments/{appointment_id}/cancel", json={
            "expected_version": 2, "reason": "Synthetic cancellation",
            "idempotency_key": "comm-http-cancel-0001"})
        assert cancelled.status_code == 200, cancelled.text

        rows = _outbox(engine, tenant["tenant_id"], appointment_id)
        events = {(row["purpose"], row["expected_entity_version"]) for row in rows}
        assert {("booking_rescheduled", 2), ("booking_cancelled", 3)} <= events
        assert {row["expected_entity_version"] for row in rows
                if row["purpose"].startswith("appointment_reminder_")} == {1, 2}
        assert len([row for row in rows if row["purpose"] == "booking_confirmation"]) == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_confirmation_recorded_and_dispatched(database_url):
    """AC-1: a booking records a confirmation communication that dispatches cleanly."""
    issuer = "https://identity.example.invalid/comm-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        loc, service, staff, client_id, appt_id, starts = _booked_appointment(client, tenant)
        adapter = RecordingAdapter()
        initial = _outbox(engine, tenant["tenant_id"], appt_id)
        confirmation = next(row for row in initial if row["purpose"] == "booking_confirmation")
        assert confirmation["state"] == "recorded"
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
            assert summary["dispatched"] == 1
        assert summary["skipped_stale"] == 0
        assert len(adapter.records) == 1
        assert adapter.records[0]["template"] == "booking_confirmation"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_reminders_scheduled_and_versioned(database_url):
    """AC-1: reminders are scheduled with the appointment version and dispatched on time."""
    issuer = "https://identity.example.invalid/comm-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        loc, service, staff, client_id, appt_id, starts = _booked_appointment(client, tenant)
        scheduled = [row for row in _outbox(engine, tenant["tenant_id"], appt_id)
                     if row["purpose"].startswith("appointment_reminder_")]
        assert len(scheduled) == 2  # 24 h and 2 h before start
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert summary["dispatched"] == 1  # confirmation is ready immediately
        assert [record["template"] for record in adapter.records] == ["booking_confirmation"]
        dispatch_time = starts - timedelta(hours=1)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter,
                now=dispatch_time)
        assert summary["dispatched"] == 2
        assert summary["dispatched"] == 2
        assert len(adapter.records) == 3
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_stale_reminder_skipped_after_reschedule(database_url):
    """AC-2: rescheduling bumps the appointment version; old reminders skip as stale."""
    issuer = "https://identity.example.invalid/comm-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, service, staff, client_id, appt_id, starts = _booked_appointment(client, tenant)
        new_starts = starts + timedelta(days=7)
        client.post(f"{base}/appointments/{appt_id}/reschedule", json={
            "starts_at": new_starts.isoformat(), "expected_version": 1,
            "idempotency_key": "comm-resched-0001"})
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter,
                now=starts - timedelta(hours=1))
        assert summary["skipped_stale"] == 3  # old confirmation and two old reminders
        assert summary["dispatched"] == 1  # current reschedule notice; new reminders are future
        assert [record["template"] for record in adapter.records] == ["booking_rescheduled"]
        stale_rows = [row for row in _outbox(engine, tenant["tenant_id"], appt_id)
                      if row["state"] == "skipped_stale"]
        assert len(stale_rows) == 3
        assert {row["purpose"] for row in stale_rows} == {
            "booking_confirmation", "appointment_reminder_24h", "appointment_reminder_2h"}
        assert all(row["skip_reason"] and "appointment version changed" in row["skip_reason"]
                   for row in stale_rows)
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_expired_reminder_skipped(database_url):
    """AC-2: a reminder whose window has passed is skipped with a reason."""
    issuer = "https://identity.example.invalid/comm-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        loc, service, staff, client_id, appt_id, starts = _booked_appointment(client, tenant)
        adapter = RecordingAdapter()
        after_start = starts + timedelta(minutes=30)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            dispatch_due_communications(connection=conn, tenant_id=tenant["tenant_id"],
                                        adapter=adapter, now=starts - timedelta(hours=25))
        adapter.records.clear()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter,
                now=after_start)
        assert summary["skipped_expired"] == 2
        assert summary["dispatched"] == 0
        expired_rows = [row for row in _outbox(engine, tenant["tenant_id"], appt_id)
                        if row["state"] == "skipped_expired"]
        assert len(expired_rows) == 2
        assert {row["purpose"] for row in expired_rows} == {
            "appointment_reminder_24h", "appointment_reminder_2h"}
        assert all(row["skip_reason"] == "reminder window passed before dispatch"
                   for row in expired_rows)
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_provider_failure_does_not_affect_appointment(database_url):
    """AC-2: a failing adapter schedules a backoff retry; the appointment is intact."""
    issuer = "https://identity.example.invalid/comm-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        loc, service, staff, client_id, appt_id, starts = _booked_appointment(client, tenant)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"],
                adapter=FailingAdapter())
        # TASK-030: the failure schedules attempt 1 with backoff instead of
        # failing terminally; the booking itself is untouched either way.
        assert summary["retry_scheduled"] == 1
        assert summary["dispatched"] == 0
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            status = conn.execute(
                text("SELECT status FROM appointments WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": appt_id}).scalar_one()
            comm_state = conn.execute(
                text("SELECT state FROM communications WHERE tenant_id=:t AND source_id=:id "
                     "AND purpose='booking_confirmation'"),
                {"t": tenant["tenant_id"], "id": appt_id}).scalar_one()
            attempt = conn.execute(
                text("SELECT attempt, state FROM dispatch_attempts WHERE tenant_id=:t"),
                {"t": tenant["tenant_id"]}).mappings().one()
        assert status == "confirmed"
        assert comm_state == "retry_scheduled"
        assert (attempt["attempt"], attempt["state"]) == (1, "queued")
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_dedupe_prevents_double_record(database_url):
    """AC-1: a replayed booking command does not create a second communication."""
    issuer = "https://identity.example.invalid/comm-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        loc, service, staff, client_id, appt_id, starts = _booked_appointment(client, tenant)
        replay = client.post(f"/v1/tenants/{tenant['tenant_id']}/appointments", json={
            "client_id": client_id, "staff_id": staff, "service_id": service,
            "starts_at": starts.isoformat(), "source": "staff",
            "idempotency_key": "comm-test-key-0001"})
        assert replay.status_code in (200, 201), replay.text
        assert replay.json()["appointment_id"] == appt_id
        rows = _outbox(engine, tenant["tenant_id"], appt_id)
        assert len([row for row in rows if row["purpose"] == "booking_confirmation"]) == 1
        assert len([row for row in rows if row["purpose"].startswith("appointment_reminder_")]) == 2
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_public_hold_confirmation_records_email_and_reminders(database_url):
    """AC-1: a confirmed public booking commits its own email intents atomically."""
    issuer = "https://identity.example.invalid/comm-public"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, slug, service, staff, _ = _bookable(client, database_url, tenant)
        starts = _first_slot(client, slug, service, date.today() + timedelta(days=1))
        hold = client.post(f"/v1/public/salons/{slug}/holds", json={
            "service_id": service, "staff_id": staff, "starts_at": starts,
            "idempotency_key": "comm-public-hold-" + os.urandom(8).hex()})
        assert hold.status_code == 201, hold.text
        confirmation = client.post(
            f"/v1/public/salons/{slug}/holds/{hold.json()['hold_id']}/confirm", json={
                "owner_token": hold.json()["owner_token"], "client_name": "Synthetic Booker",
                "email": "public.booking@example.invalid", "country": "CA",
                "policy_accepted": True, "policy_version": 1,
                "idempotency_key": "comm-public-confirm-0001"})
        assert confirmation.status_code == 200, confirmation.text
        rows = _outbox(engine, tenant["tenant_id"], confirmation.json()["appointment_id"])
        assert {row["purpose"] for row in rows} == {
            "booking_confirmation", "appointment_reminder_24h", "appointment_reminder_2h"}
        assert {row["expected_entity_version"] for row in rows} == {1}
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_worker_discovers_due_tenant_and_dispatches_in_tenant_scope(database_url):
    """AC-1/AC-2: worker discovery sees tenant IDs only, and content reads are scoped."""
    issuer = "https://identity.example.invalid/comm-worker"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, _, appointment_id, starts = _booked_appointment(client, tenant)
        now = starts - timedelta(hours=23)
        with engine.begin() as connection:
            candidates = connection.execute(
                text("SELECT tenant_id::text FROM due_communication_tenants(:now, :limit)"),
                {"now": now, "limit": 50}).scalars().all()
        assert tenant["tenant_id"] in candidates

        adapter = RecordingAdapter()
        summary = dispatch_tenant_once(engine, tenant["tenant_id"], adapter=adapter, now=now)
        assert summary["dispatched"] == 2  # confirmation plus the 24-hour reminder
        assert len(adapter.records) == 2
        remaining = _outbox(engine, tenant["tenant_id"], appointment_id)
        two_hour = next(row for row in remaining if row["purpose"] == "appointment_reminder_2h")
        assert two_hour["state"] == "recorded"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
