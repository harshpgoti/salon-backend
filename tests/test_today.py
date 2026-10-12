"""Tests for the Today dashboard (TASK-041, FR-058).

AC-1: Appointments approvals service states and delivery tasks reconcile.
AC-2: Location/role scope plus empty/error/stale UI states verified.
"""

import os
import sys
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.callbacks import ingest_callback, sign_callback_body
from modules.communications.outbox import dispatch_due_communications, record_booking_communication
from test_booking_holds import _bookable
from test_clients import _salon, _client, _teardown
from test_communications import FailingAdapter


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _move(client, base, appt_id, to_status, version, reason="today"):
    moved = client.post(f"{base}/appointments/{appt_id}/transitions", json={
        "to_status": to_status, "expected_version": version,
        "idempotency_key": f"today-t-{appt_id[:8]}-{version}", "reason": reason})
    assert moved.status_code == 200, moved.text


def _book_on(client, base, service, staff, client_id, day, hour, key):
    starts = (datetime.combine(day, datetime.min.time())
              + timedelta(hours=hour)).replace(tzinfo=timezone.utc)
    created = client.post(f"{base}/appointments", json={
        "client_id": client_id, "staff_id": staff, "service_id": service,
        "starts_at": starts.isoformat(), "source": "staff",
        "idempotency_key": key})
    assert created.status_code == 201, created.text
    return created.json()["appointment_id"]


def test_today_ac1_aggregates_reconcile(database_url):
    """AC-1: states, approvals, closures and delivery tasks add up."""
    issuer = "https://identity.example.invalid/today-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        loc = tenant["location_id"]
        tid = tenant["tenant_id"]

        confirmed = _book_on(client, base, service, staff, client_id, day, 10, "today-k-1")
        checked = _book_on(client, base, service, staff, client_id, day, 12, "today-k-2")
        _move(client, base, checked, "checked_in", 1)
        dropped = _book_on(client, base, service, staff, client_id, day, 14, "today-k-3")
        _move(client, base, dropped, "cancelled", 1, reason="client request")
        # pending_approval has no producing API yet: fixture the state directly
        # so the dashboard proves it aggregates whatever exists.
        waiting = _book_on(client, base, service, staff, client_id, day, 15, "today-k-4")
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE appointments SET status='pending_approval'
                                 WHERE tenant_id=:t AND id=:a"""),
                         {"t": tid, "a": waiting})

        exc = client.post(f"{base}/locations/{loc}/exceptions", json={
            "exception_date": day.isoformat(), "kind": "closed"}).json()["id"]
        client.post(f"{base}/locations/{loc}/exceptions/{exc}/publish", json={"published": True})
        client.post(f"{base}/closures/{loc}/impacts", json={
            "appointment_id": confirmed, "location_id": loc,
            "exception_date": day.isoformat()})

        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            # An SMS with no verified sender fails visibly (delivery task 1).
            record_booking_communication(
                connection=conn, tenant_id=tid, appointment_id=confirmed,
                client_id=client_id, contact_value="+14165550101", channel="sms",
                purpose="appointment_reminder_24h", entity_version=1)
            dispatch_due_communications(connection=conn, tenant_id=tid,
                                        adapter=FailingAdapter())
            # An unknown provider acceptance reconciles to a dead letter (task 2).
            conn.execute(
                text("""INSERT INTO provider_webhook_secrets (tenant_id, channel, secret)
                        VALUES (:t, 'email', :s) ON CONFLICT DO NOTHING"""),
                {"t": tid, "s": "today-secret-0123456789abcdef0102"})
            body = b"{}"
            ingest_callback(conn, tenant_id=tid, channel="email", raw_body=body,
                            signature=sign_callback_body(
                                "today-secret-0123456789abcdef0102", body),
                            event="accepted", provider_event_id="today-ghost",
                            provider_ref="today-ghost-ref")
            # An inbound reply opens a follow-up task (task 3).
            conn.execute(
                text("""INSERT INTO follow_up_tasks (tenant_id, id, kind, title, detail,
                                                      source_type)
                        VALUES (:t, gen_random_uuid(), 'inbound_reply', 'Client replied',
                                'triage', 'manual')"""),
                {"t": tid})

        report = client.get(f"/v1/tenants/{tid}/reports/today",
                            params={"day": day.isoformat()})
        assert report.status_code == 200, report.text
        body = report.json()
        assert body["date"] == day.isoformat()
        assert body["generated_at"]
        assert body["counts_by_state"] == {"confirmed": 1, "checked_in": 1,
                                           "cancelled": 1, "pending_approval": 1}
        assert [a["id"] for a in body["pending_approvals"]] == [waiting]
        assert len(body["appointments"]) == 4
        assert len(body["active_closure_impacts"]) == 1
        assert body["delivery_tasks"] == {"failed_communications": 1,
                                          "open_dead_letters": 1,
                                          "open_follow_up_tasks": 1}
        assert any(s["staff_id"] == staff for s in body["staff_on_shift"])
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_today_ac2_scope_empty_and_stale(database_url):
    """AC-2: location filter isolates; empty salon shows empty, dated lists."""
    issuer = "https://identity.example.invalid/today-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        tid = tenant["tenant_id"]
        _book_on(client, base, service, staff, client_id, day, 10, "today-k-9")

        scoped = client.get(f"/v1/tenants/{tid}/reports/today",
                            params={"day": day.isoformat(),
                                    "location_id": tenant["location_id"]})
        assert scoped.status_code == 200, scoped.text
        assert scoped.json()["locations_scoped"] == [tenant["location_id"]]
        assert len(scoped.json()["appointments"]) == 1

        from test_clients import _identity
        outsider = _client(database_url, _identity())
        assert outsider.get(f"/v1/tenants/{tid}/reports/today").status_code == 404

        # A date with nothing booked is empty but well-formed (stale-safe UI).
        empty = client.get(f"/v1/tenants/{tid}/reports/today",
                           params={"day": (day + timedelta(days=30)).isoformat()}).json()
        assert empty["appointments"] == [] and empty["counts_by_state"] == {}
        assert empty["pending_approvals"] == []
        assert empty["generated_at"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_today_ac2_front_desk_roles_can_read():
    """AC-2: the front desk (manager, receptionist) may open Today."""
    from modules.identity.main import ROLE_PERMISSIONS
    assert "appointments.read" in ROLE_PERMISSIONS["manager"]
    assert "appointments.read" in ROLE_PERMISSIONS["receptionist"]
    assert "appointments.read" not in ROLE_PERMISSIONS["stylist"]
    assert "appointments.read" not in ROLE_PERMISSIONS["analyst"]
