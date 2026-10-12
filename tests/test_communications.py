"""Tests for the durable communication outbox (TASK-027, FR-039/FR-042).

AC-1: Confirm/change/cancel/reminders versioned and dispatched after commit
AC-2: Provider failure leaves booking intact; stale/expired reminders skip
"""

import os
import sys
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.outbox import (
    dispatch_due_communications,
    record_booking_communication,
    schedule_booking_reminders,
)
from test_clients import _salon, _client, _teardown


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

    def send(self, *, channel, recipient, template):
        self.records.append({"channel": channel, "recipient": recipient, "template": template})
        return {"status": "recorded", "id": f"recorded-{len(self.records)}"}


class FailingAdapter:
    """Adapter that always raises — simulates a provider outage."""

    def send(self, *, channel, recipient, template):
        raise RuntimeError("provider unavailable")


def _booked_appointment(client, tenant):
    """Create a salon with a client and one confirmed appointment; return ids."""
    base = f"/v1/tenants/{tenant['tenant_id']}"
    loc = tenant["location_id"]
    day = date.today() + timedelta(days=3)  # far enough that 24h/2h reminders are not due
    client.put(f"{base}/locations/{loc}/hours", json={
        "intervals": [{"weekday": day.weekday(), "opens_at": "09:00", "closes_at": "17:00"}]})
    service = client.post(f"{base}/services", json={
        "name": "Cut", "duration_minutes": 60, "price_minor": 6000}).json()["id"]
    staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
    client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [loc]})
    client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
    client_row = client.post(f"{base}/clients", json={"name": "Client One"}).json()
    client_id = client_row["id"]
    starts = (datetime.combine(day, datetime.min.time())
              + timedelta(hours=10)).replace(tzinfo=timezone.utc)
    appt = client.post(f"{base}/appointments", json={
        "client_id": client_id, "staff_id": staff, "service_id": service,
        "starts_at": starts.isoformat(), "source": "staff",
        "idempotency_key": "comm-test-key-0001"}).json()
    return loc, service, staff, client_id, appt["appointment_id"], starts


def _client_email(engine, tenant_id, client_id):
    with engine.connect() as conn:
        row = conn.execute(
            text("""SELECT normalized_value FROM client_contacts
                    WHERE tenant_id=:t AND client_id=:c AND kind='email' AND is_primary
                    LIMIT 1"""),
            {"t": tenant_id, "c": client_id}).scalar_one_or_none()
    return row or "fallback@example.invalid"


def test_confirmation_recorded_and_dispatched(database_url):
    """AC-1: a booking records a confirmation communication that dispatches cleanly."""
    issuer = "https://identity.example.invalid/comm-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        loc, service, staff, client_id, appt_id, starts = _booked_appointment(client, tenant)
        email = _client_email(engine, tenant["tenant_id"], client_id)
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            row = record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value=email, channel="email",
                purpose="booking_confirmation", entity_version=1)
            assert row is not None
            assert row["state"] == "recorded"
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
        email = _client_email(engine, tenant["tenant_id"], client_id)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            rows = schedule_booking_reminders(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value=email, channel="email",
                entity_version=1, starts_at=starts)
            assert len(rows) == 2  # 24 h and 2 h before start
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert summary["dispatched"] == 0
        dispatch_time = starts - timedelta(hours=1)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter,
                now=dispatch_time)
        assert summary["dispatched"] == 2
        assert len(adapter.records) == 2
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
        email = _client_email(engine, tenant["tenant_id"], client_id)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            schedule_booking_reminders(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value=email, channel="email",
                entity_version=1, starts_at=starts)
        new_starts = starts + timedelta(days=1)
        client.post(f"{base}/appointments/{appt_id}/reschedule", json={
            "starts_at": new_starts.isoformat(), "expected_version": 1,
            "idempotency_key": "comm-resched-0001"})
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter,
                now=new_starts - timedelta(hours=1))
        assert summary["skipped_stale"] == 2
        assert summary["dispatched"] == 0
        assert len(adapter.records) == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_expired_reminder_skipped(database_url):
    """AC-2: a reminder whose window has passed is skipped with a reason."""
    issuer = "https://identity.example.invalid/comm-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        loc, service, staff, client_id, appt_id, starts = _booked_appointment(client, tenant)
        email = _client_email(engine, tenant["tenant_id"], client_id)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            schedule_booking_reminders(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value=email, channel="email",
                entity_version=1, starts_at=starts)
        adapter = RecordingAdapter()
        after_start = starts + timedelta(minutes=30)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter,
                now=after_start)
        assert summary["skipped_expired"] == 2
        assert summary["dispatched"] == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_provider_failure_does_not_affect_appointment(database_url):
    """AC-2: a failing adapter marks the communication failed; the appointment is intact."""
    issuer = "https://identity.example.invalid/comm-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        loc, service, staff, client_id, appt_id, starts = _booked_appointment(client, tenant)
        email = _client_email(engine, tenant["tenant_id"], client_id)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value=email, channel="email",
                purpose="booking_confirmation", entity_version=1)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"],
                adapter=FailingAdapter())
        assert summary["failed"] == 1
        assert summary["dispatched"] == 0
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            status = conn.execute(
                text("SELECT status FROM appointments WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": appt_id}).scalar_one()
        assert status == "confirmed"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_dedupe_prevents_double_record(database_url):
    """AC-1: a replayed booking command does not create a second communication."""
    issuer = "https://identity.example.invalid/comm-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        loc, service, staff, client_id, appt_id, starts = _booked_appointment(client, tenant)
        email = _client_email(engine, tenant["tenant_id"], client_id)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            first = record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value=email, channel="email",
                purpose="booking_confirmation", entity_version=1)
            second = record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value=email, channel="email",
                purpose="booking_confirmation", entity_version=1)
        assert first is not None
        assert second is None  # deduped
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)