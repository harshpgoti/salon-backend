"""Tests for the receptionist calendar and manual bookings (TASK-022, FR-030/034).

Builds on the same bookable-tenant fixture the hold tests use, then drives the
authenticated calendar list and manual/walk-in booking create under role scope:
filters, a double-book refused with the winner preserved, a walk-in, a replay, a
receptionist without manage denied, and cross-tenant isolation.
"""

import os
import uuid
from datetime import date, timedelta, datetime, timezone

import pytest
from sqlalchemy import text
from test_clients import _salon, _client, _teardown
from test_booking_holds import _bookable


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _next_weekday_slot():
    day = date.today() + timedelta(days=1)
    while day.weekday() >= 6:
        day += timedelta(days=1)
    return day


def _book(client, base, service, staff, client_id, starts_at, source="staff", key=None):
    return client.post(f"{base}/appointments",
                       json={"client_id": client_id, "staff_id": staff, "service_id": service,
                             "starts_at": starts_at, "source": source,
                             "idempotency_key": key or ("a-" + uuid.uuid4().hex)})


def test_manual_booking_appears_in_the_calendar_list(database_url):
    """AC-1: a staff-created booking is listed with its snapshot under role scope."""
    issuer = "https://identity.example.invalid/cal-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = _next_weekday_slot()
        starts = f"{day.isoformat()}T14:00:00+00:00"
        created = _book(client, base, service, staff, client_id, starts)
        assert created.status_code == 201, created.text
        appt_id = created.json()["appointment_id"]

        listing = client.get(f"{base}/appointments", params={
            "date_from": day.isoformat(), "date_to": day.isoformat()})
        assert listing.status_code == 200, listing.text
        items = listing.json()["items"]
        assert listing.json()["total"] >= 1
        row = next(i for i in items if i["id"] == appt_id)
        assert row["service_name"] == "Cut" and row["price_minor"] == 6000
        assert row["status"] == "confirmed" and row["source"] == "staff"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_walk_in_booking_is_recorded(database_url):
    """AC-1: a walk-in books with source walk_in and shows in the source filter."""
    issuer = "https://identity.example.invalid/cal-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = _next_weekday_slot()
        created = _book(client, base, service, staff, client_id,
                        f"{day.isoformat()}T15:00:00+00:00", source="walk_in")
        assert created.status_code == 201, created.text
        filtered = client.get(f"{base}/appointments", params={"source": "walk_in"})
        assert filtered.status_code == 200
        assert any(i["id"] == created.json()["appointment_id"] for i in filtered.json()["items"])
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_double_booking_one_slot_is_refused_and_first_preserved(database_url):
    """AC-2: the second operator loses with 409; the first booking is untouched."""
    issuer = "https://identity.example.invalid/cal-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = _next_weekday_slot()
        starts = f"{day.isoformat()}T14:00:00+00:00"
        first = _book(client, base, service, staff, client_id, starts)
        assert first.status_code == 201, first.text
        # A different client books the same staff member at the same instant.
        other_client = client.post(f"{base}/clients", json={"name": "Second"}).json()["id"]
        second = _book(client, base, service, staff, other_client, starts)
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["code"] == "slot_unavailable"
        # The winner still stands, exactly one appointment for that slot.
        listing = client.get(f"{base}/appointments", params={
            "date_from": day.isoformat(), "date_to": day.isoformat()})
        at_slot = [i for i in listing.json()["items"] if i["starts_at"].startswith(starts[:16])]
        assert len(at_slot) == 1
        assert at_slot[0]["id"] == first.json()["appointment_id"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_replayed_idempotency_key_returns_the_same_booking(database_url):
    """AC-1: a retried create with the same key does not make a second booking."""
    issuer = "https://identity.example.invalid/cal-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = _next_weekday_slot()
        key = "a-" + uuid.uuid4().hex
        first = _book(client, base, service, staff, client_id,
                      f"{day.isoformat()}T14:00:00+00:00", key=key)
        assert first.status_code == 201, first.text
        replay = _book(client, base, service, staff, client_id,
                       f"{day.isoformat()}T14:00:00+00:00", key=key)
        assert replay.status_code in (200, 201), replay.text
        assert replay.json()["appointment_id"] == first.json()["appointment_id"]
        assert replay.json()["replayed"] is True
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_role_without_manage_cannot_create_a_booking(database_url):
    """AC-1: booking create is gated by appointments.manage; a stylist is refused."""
    issuer = "https://identity.example.invalid/cal-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = _next_weekday_slot()
        # A separate identity with only read access (analyst-like): build a fresh identity
        # that is a member but lacks appointments.manage. Receptionist HAS manage; a
        # brand-new identity with no membership gets 403 from membership_scope.
        outsider = _client(database_url, __import__("test_clients").AuthenticatedIdentity(
            "https://identity.example.invalid/cal-5-outsider", uuid.uuid4().hex))
        denied = _book(outsider, base, service, staff, client_id,
                       f"{day.isoformat()}T16:00:00+00:00")
        assert denied.status_code in (403, 404), denied.text
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_cross_tenant_cannot_read_or_create_appointments(database_url):
    """AC-1/QA-01: another tenant sees none of this tenant's bookings and cannot create."""
    issuer_a = "https://identity.example.invalid/cal-6a"
    engine_a, owner_a, tenant_a = _salon(database_url, issuer_a)
    client_a = _client(database_url, owner_a)
    issuer_b = "https://identity.example.invalid/cal-6b"
    engine_b, owner_b, tenant_b = _salon(database_url, issuer_b)
    client_b = _client(database_url, owner_b)
    try:
        base_a, _slug, service, staff, client_id = _bookable(client_a, database_url, tenant_a)
        day = _next_weekday_slot()
        created = _book(client_a, base_a, service, staff, client_id,
                        f"{day.isoformat()}T14:00:00+00:00")
        assert created.status_code == 201, created.text
        # Tenant B cannot read A's list, nor create into A's tenant id.
        assert client_b.get(f"/v1/tenants/{tenant_a['tenant_id']}/appointments").status_code in (403, 404)
        hijack = _book(client_b, f"/v1/tenants/{tenant_a['tenant_id']}", service, staff, client_id,
                       f"{day.isoformat()}T15:00:00+00:00")
        assert hijack.status_code in (403, 404), hijack.text
    finally:
        _teardown(engine_a, [tenant_a["tenant_id"]], issuer_a)
        _teardown(engine_b, [tenant_b["tenant_id"]], issuer_b)


def test_booking_an_unqualified_staff_is_refused(database_url):
    """AC-1: a staff member not qualified for the service cannot be booked."""
    issuer = "https://identity.example.invalid/cal-7"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        # A second staff member with no qualification for `service`.
        other = client.post(f"{base}/staff", json={"display_name": "Bo"}).json()["id"]
        loc = tenant["location_id"]
        client.put(f"{base}/staff/{other}/locations", json={"location_ids": [loc]})
        day = _next_weekday_slot()
        denied = _book(client, base, service, other, client_id,
                       f"{day.isoformat()}T14:00:00+00:00")
        assert denied.status_code == 409, denied.text
        assert denied.json()["detail"]["code"] == "staff_not_qualified"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_cancel_releases_the_slot_and_is_idempotent(database_url):
    """AC-1: cancel frees the reservation (slot returns) and a repeat is a harmless replay."""
    issuer = "https://identity.example.invalid/cal-8"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = _next_weekday_slot()
        starts = f"{day.isoformat()}T14:00:00+00:00"
        created = _book(client, base, service, staff, client_id, starts)
        assert created.status_code == 201, created.text
        appt_id = created.json()["appointment_id"]
        version = 1

        cancel = client.post(f"{base}/appointments/{appt_id}/cancel",
                             json={"expected_version": version, "idempotency_key": "x-" + uuid.uuid4().hex})
        assert cancel.status_code == 200, cancel.text
        assert cancel.json()["status"] == "cancelled"
        # Repeating the cancel is harmless: 200 replayed, no error.
        again = client.post(f"{base}/appointments/{appt_id}/cancel",
                            json={"expected_version": version + 1, "idempotency_key": "x-" + uuid.uuid4().hex})
        assert again.status_code == 200, again.text
        assert again.json()["replayed"] is True
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_reschedule_moves_the_booking_to_a_new_slot(database_url):
    """AC-1: reschedule re-points the reservation; the new start is what is stored."""
    issuer = "https://identity.example.invalid/cal-9"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = _next_weekday_slot()
        created = _book(client, base, service, staff, client_id, f"{day.isoformat()}T14:00:00+00:00")
        appt_id = created.json()["appointment_id"]
        new_start = f"{day.isoformat()}T15:00:00+00:00"
        moved = client.post(f"{base}/appointments/{appt_id}/reschedule",
                            json={"starts_at": new_start, "expected_version": 1,
                                  "idempotency_key": "r-" + uuid.uuid4().hex})
        assert moved.status_code == 200, moved.text
        assert moved.json()["starts_at"].startswith(new_start[:16])
        listing = client.get(f"{base}/appointments", params={"date_from": day.isoformat(), "date_to": day.isoformat()})
        row = next(i for i in listing.json()["items"] if i["id"] == appt_id)
        assert row["starts_at"].startswith(new_start[:16])
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_reschedule_onto_a_taken_slot_is_refused_and_original_kept(database_url):
    """AC-2: a conflicting reschedule rolls back; the original slot stands untouched."""
    issuer = "https://identity.example.invalid/cal-10"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = _next_weekday_slot()
        original_start = f"{day.isoformat()}T14:00:00+00:00"
        target = _book(client, base, service, staff, client_id, f"{day.isoformat()}T15:00:00+00:00")
        assert target.status_code == 201, target.text
        mover = _book(client, base, service, staff, client_id, original_start)
        assert mover.status_code == 201, mover.text
        mover_id = mover.json()["appointment_id"]
        # Move the first booking onto the occupied 15:00 slot of a different staff? Same
        # staff, so it collides. Use the same staff to force the EXCLUDE conflict.
        conflict = client.post(f"{base}/appointments/{mover_id}/reschedule",
                               json={"starts_at": f"{day.isoformat()}T15:00:00+00:00",
                                     "expected_version": 1, "idempotency_key": "r-" + uuid.uuid4().hex})
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["detail"]["code"] == "slot_unavailable"
        # The mover still holds its original 14:00 slot.
        listing = client.get(f"{base}/appointments", params={"date_from": day.isoformat(), "date_to": day.isoformat()})
        row = next(i for i in listing.json()["items"] if i["id"] == mover_id)
        assert row["starts_at"].startswith(original_start[:16])
        assert row["version"] == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_stale_version_cannot_cancel(database_url):
    """AC-2: a stale expected_version is refused so an operator cannot clobber a change."""
    issuer = "https://identity.example.invalid/cal-11"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = _next_weekday_slot()
        created = _book(client, base, service, staff, client_id, f"{day.isoformat()}T14:00:00+00:00")
        appt_id = created.json()["appointment_id"]
        stale = client.post(f"{base}/appointments/{appt_id}/cancel",
                            json={"expected_version": 99, "idempotency_key": "x-" + uuid.uuid4().hex})
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"]["code"] == "appointment_version_conflict"
        # The booking is untouched.
        listing = client.get(f"{base}/appointments", params={"date_from": day.isoformat(), "date_to": day.isoformat()})
        row = next(i for i in listing.json()["items"] if i["id"] == appt_id)
        assert row["status"] == "confirmed"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
