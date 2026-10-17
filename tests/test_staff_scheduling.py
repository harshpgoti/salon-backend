"""Tests for staff profiles, qualifications, shifts, leave and locations (TASK-016, FR-020)."""

import os
import uuid
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from test_clients import _salon, _client, _teardown


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _setup(client, base, name="Ava Stylist"):
    staff = client.post(f"{base}/staff", json={"display_name": name}).json()["id"]
    return staff


def _set_location_hours(client, base, location_id, day, opens="09:00", closes="17:00"):
    response = client.put(f"{base}/locations/{location_id}/hours", json={
        "intervals": [{"weekday": day.weekday(), "opens_at": opens, "closes_at": closes}]})
    assert response.status_code == 200, response.text


def test_shift_breaks_must_fall_inside_the_shift(database_url):
    """AC-1: a shift is a valid local interval; breaks are validated, not ignored."""
    issuer = "https://identity.example.invalid/staff-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        staff = _setup(client, base)
        loc = tenant["location_id"]
        client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [loc]})

        ok = client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": loc, "weekday": 0, "starts_at": "09:00", "ends_at": "17:00",
            "breaks": [{"start": "12:00", "end": "12:30"}, {"start": "15:00", "end": "15:15"}]})
        assert ok.status_code == 201, ok.text
        assert len(ok.json()["breaks"]) == 2

        outside = client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": loc, "weekday": 0, "starts_at": "09:00", "ends_at": "17:00",
            "breaks": [{"start": "07:00", "end": "07:30"}]})
        assert outside.status_code == 422
        assert outside.json()["detail"]["code"] == "break_outside_shift"

        overlapping = client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": loc, "weekday": 0, "starts_at": "09:00", "ends_at": "17:00",
            "breaks": [{"start": "12:00", "end": "13:00"}, {"start": "12:30", "end": "13:30"}]})
        assert overlapping.status_code == 422
        assert overlapping.json()["detail"]["code"] == "breaks_overlap"

        backwards = client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": loc, "weekday": 0, "starts_at": "17:00", "ends_at": "09:00"})
        assert backwards.status_code == 422
        assert backwards.json()["detail"]["code"] == "shift_ends_before_start"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_shift_at_unassigned_location_is_refused(database_url):
    """AC-1: staff scheduling is constrained by location assignment."""
    issuer = "https://identity.example.invalid/staff-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        staff = _setup(client, base)
        # No locations assigned yet.
        refused = client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": tenant["location_id"], "weekday": 1, "starts_at": "10:00", "ends_at": "14:00"})
        assert refused.status_code == 422
        assert refused.json()["detail"]["code"] == "location_not_assigned"

        client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [tenant["location_id"]]})
        allowed = client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": tenant["location_id"], "weekday": 1, "starts_at": "10:00", "ends_at": "14:00"})
        assert allowed.status_code == 201
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_qualifications_are_constrained_to_owned_services(database_url):
    """AC-1: a qualification names a service the tenant owns; a foreign id is refused."""
    issuer = "https://identity.example.invalid/staff-3"
    other_issuer = "https://identity.example.invalid/staff-3b"
    engine, owner, tenant = _salon(database_url, issuer)
    other_engine, other_owner, other_tenant = _salon(database_url, other_issuer)
    client = _client(database_url, owner)
    other_client = _client(database_url, other_owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    other_base = f"/v1/tenants/{other_tenant['tenant_id']}"
    try:
        staff = _setup(client, base)
        svc = client.post(f"{base}/services",
                          json={"name": "Balayage", "duration_minutes": 120, "price_minor": 18000}).json()["id"]
        foreign = other_client.post(f"{other_base}/services",
                                    json={"name": "Other", "duration_minutes": 30, "price_minor": 100}).json()["id"]

        assert client.put(f"{base}/staff/{staff}/qualifications",
                          json={"service_ids": [svc]}).status_code == 200
        assert client.get(f"{base}/staff/{staff}/qualifications").json()["service_ids"] == [svc]

        # A service belonging to another tenant cannot be attached.
        refused = client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [foreign]})
        assert refused.status_code == 404
        assert refused.json()["detail"]["code"] == "service_not_found"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], other_issuer)


def test_leave_is_recorded_and_validated(database_url):
    """AC-1: approved leave is stored with an ordered date range."""
    issuer = "https://identity.example.invalid/staff-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        staff = _setup(client, base)
        leave = client.post(f"{base}/staff/{staff}/leave",
                            json={"starts_on": "2026-12-24", "ends_on": "2026-12-26", "reason": "holiday", "approved": True})
        assert leave.status_code == 201
        listed = client.get(f"{base}/staff/{staff}/leave").json()["items"]
        assert len(listed) == 1 and listed[0]["approved"] is True

        # End before start is rejected by the model.
        bad = client.post(f"{base}/staff/{staff}/leave",
                          json={"starts_on": "2026-12-26", "ends_on": "2026-12-24"})
        assert bad.status_code == 422
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_deactivation_is_a_date_and_previews_future_work(database_url):
    """AC-2: deactivation reports affected future appointments and does not delete history."""
    issuer = "https://identity.example.invalid/staff-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        staff = _setup(client, base)
        preview = client.get(f"{base}/staff/{staff}/deactivation-preview").json()
        # Until the appointments table exists (TASK-020) there is nothing to resolve.
        assert preview["requires_resolution"] is False

        deactivated = client.post(f"{base}/staff/{staff}/deactivate", json={"deactivated_on": "2026-12-01"})
        assert deactivated.status_code == 200
        assert deactivated.json()["deactivated_on"] == "2026-12-01"

        # A future deactivation must not remove the staff member yet (FR-020).
        assert len(client.get(f"{base}/staff").json()["items"]) == 1

        # Once the date has passed, the staff member stops being offered but the row survives.
        past = client.post(f"{base}/staff/{staff}/deactivate", json={"deactivated_on": "2020-01-01"})
        assert past.status_code == 200
        assert client.get(f"{base}/staff").json()["items"] == []
        assert len(client.get(f"{base}/staff", params={"include_deactivated": "true"}).json()["items"]) == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_deactivation_refuses_unresolved_bookings_from_effective_date(database_url):
    """AC-2: preview is date-specific and deactivation waits for booking resolution."""
    issuer = "https://identity.example.invalid/staff-deactivation-resolution"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        staff = _setup(client, base)
        location = tenant["location_id"]
        day = date.today() + timedelta(days=8)
        _set_location_hours(client, base, location, day)
        service = client.post(f"{base}/services", json={
            "name": "Resolution cut", "duration_minutes": 30, "price_minor": 4000}).json()["id"]
        person = client.post(f"{base}/clients", json={"name": "Synthetic affected booking"}).json()["id"]
        client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [location]})
        client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
        assert client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": location, "weekday": day.weekday(), "starts_at": "09:00", "ends_at": "17:00"}).status_code == 201
        start = datetime.combine(day, time(10), ZoneInfo("America/Toronto"))
        booked = client.post(f"{base}/appointments", json={
            "location_id": location, "client_id": person, "staff_id": staff, "service_id": service,
            "starts_at": start.isoformat(), "source": "staff", "idempotency_key": uuid.uuid4().hex})
        assert booked.status_code == 201, booked.text

        preview = client.get(f"{base}/staff/{staff}/deactivation-preview", params={"on": day.isoformat()})
        assert preview.status_code == 200, preview.text
        assert preview.json()["requires_resolution"] is True
        assert [row["id"] for row in preview.json()["affected_appointments"]] == [booked.json()["appointment_id"]]

        refused = client.post(f"{base}/staff/{staff}/deactivate", json={"deactivated_on": day.isoformat()})
        assert refused.status_code == 409, refused.text
        assert refused.json()["detail"]["code"] == "staff_bookings_require_resolution"

        cancelled = client.post(f"{base}/appointments/{booked.json()['appointment_id']}/cancel", json={
            "expected_version": 1, "reason": "Synthetic resolution", "idempotency_key": uuid.uuid4().hex})
        assert cancelled.status_code == 200, cancelled.text
        completed = client.post(f"{base}/staff/{staff}/deactivate", json={"deactivated_on": day.isoformat()})
        assert completed.status_code == 200, completed.text
        assert completed.json()["deactivated_on"] == day.isoformat()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_manual_booking_observes_qualification_shift_and_approved_leave(database_url):
    """AC-1: a valid identity cannot commit a booking outside its configured schedule."""
    issuer = "https://identity.example.invalid/staff-schedule-commit"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        staff = _setup(client, base)
        location = tenant["location_id"]
        day = date.today() + timedelta(days=8)
        _set_location_hours(client, base, location, day)
        service = client.post(f"{base}/services", json={
            "name": "Schedule cut", "duration_minutes": 30, "price_minor": 4000}).json()["id"]
        person = client.post(f"{base}/clients", json={"name": "Synthetic scheduled booking"}).json()["id"]
        client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [location]})
        client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
        client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": location, "weekday": day.weekday(), "starts_at": "09:00", "ends_at": "11:00"})
        start = datetime.combine(day, time(12), ZoneInfo("America/Toronto"))
        payload = {"location_id": location, "client_id": person, "staff_id": staff, "service_id": service,
                   "starts_at": start.isoformat(), "source": "staff", "idempotency_key": uuid.uuid4().hex}
        outside_shift = client.post(f"{base}/appointments", json=payload)
        assert outside_shift.status_code == 409, outside_shift.text
        assert outside_shift.json()["detail"]["code"] == "slot_unavailable"

        client.post(f"{base}/staff/{staff}/leave", json={
            "starts_on": day.isoformat(), "ends_on": day.isoformat(), "approved": True})
        on_leave = client.post(f"{base}/appointments", json={**payload, "starts_at": datetime.combine(
            day, time(10), ZoneInfo("America/Toronto")).isoformat(), "idempotency_key": uuid.uuid4().hex})
        assert on_leave.status_code == 409, on_leave.text
        assert on_leave.json()["detail"]["code"] == "slot_unavailable"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_manual_booking_can_use_a_later_disjoint_shift(database_url):
    """AC-1: any matching effective shift can contain the complete booking."""
    issuer = "https://identity.example.invalid/staff-disjoint-shifts"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        staff = _setup(client, base)
        location = tenant["location_id"]
        day = date.today() + timedelta(days=8)
        _set_location_hours(client, base, location, day)
        service = client.post(f"{base}/services", json={
            "name": "Split shift cut", "duration_minutes": 30, "price_minor": 4000}).json()["id"]
        person = client.post(f"{base}/clients", json={"name": "Synthetic split shift booking"}).json()["id"]
        client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [location]})
        client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
        for start, end in (("09:00", "11:00"), ("13:00", "17:00")):
            response = client.post(f"{base}/staff/{staff}/shifts", json={
                "location_id": location, "weekday": day.weekday(), "starts_at": start, "ends_at": end})
            assert response.status_code == 201, response.text
        start = datetime.combine(day, time(14), ZoneInfo("America/Toronto"))
        booked = client.post(f"{base}/appointments", json={
            "location_id": location, "client_id": person, "staff_id": staff, "service_id": service,
            "starts_at": start.isoformat(), "source": "staff", "idempotency_key": uuid.uuid4().hex})
        assert booked.status_code == 201, booked.text
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_reschedule_rechecks_the_candidate_staff_shift(database_url):
    """AC-1: moving an existing reservation cannot bypass staff hours."""
    issuer = "https://identity.example.invalid/staff-reschedule-policy"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        staff = _setup(client, base)
        location = tenant["location_id"]
        day = date.today() + timedelta(days=8)
        _set_location_hours(client, base, location, day)
        service = client.post(f"{base}/services", json={
            "name": "Reschedule cut", "duration_minutes": 30, "price_minor": 4000}).json()["id"]
        person = client.post(f"{base}/clients", json={"name": "Synthetic moved booking"}).json()["id"]
        client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [location]})
        client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
        client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": location, "weekday": day.weekday(), "starts_at": "09:00", "ends_at": "11:00"})
        start = datetime.combine(day, time(10), ZoneInfo("America/Toronto"))
        booked = client.post(f"{base}/appointments", json={
            "location_id": location, "client_id": person, "staff_id": staff, "service_id": service,
            "starts_at": start.isoformat(), "source": "staff", "idempotency_key": uuid.uuid4().hex})
        assert booked.status_code == 201, booked.text
        moved = client.post(f"{base}/appointments/{booked.json()['appointment_id']}/reschedule", json={
            "starts_at": datetime.combine(day, time(12), ZoneInfo("America/Toronto")).isoformat(),
            "expected_version": 1, "idempotency_key": uuid.uuid4().hex})
        assert moved.status_code == 409, moved.text
        assert moved.json()["detail"]["code"] == "slot_unavailable"
        current = client.get(f"{base}/appointments").json()["items"][0]
        assert current["starts_at"].startswith(start.astimezone(ZoneInfo("UTC")).isoformat()[:19])
        assert current["version"] == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_public_availability_uses_candidate_date_and_location_shift(database_url):
    """AC-1: future deactivation and shifts at another location remove public slots."""
    issuer = "https://identity.example.invalid/staff-availability-effective-date"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        staff = _setup(client, base)
        location = tenant["location_id"]
        day = date.today() + timedelta(days=8)
        _set_location_hours(client, base, location, day)
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            slug = connection.execute(text("SELECT booking_slug FROM locations WHERE tenant_id=:t AND id=:l"),
                                       {"t": tenant["tenant_id"], "l": location}).scalar_one()
        client.put(f"{base}/locations/{location}/hours", json={
            "intervals": [{"weekday": day.weekday(), "opens_at": "09:00", "closes_at": "17:00"}]})
        service = client.post(f"{base}/services", json={
            "name": "Effective shift cut", "duration_minutes": 30, "price_minor": 4000}).json()["id"]
        client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [location]})
        client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
        assert client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": location, "weekday": day.weekday(), "starts_at": "09:00", "ends_at": "17:00",
            "effective_from": day.isoformat(), "effective_to": day.isoformat()}).status_code == 201
        slots = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": day.isoformat(), "date_to": day.isoformat()})
        assert slots.status_code == 200, slots.text
        assert slots.json()["slots"]
        assert client.post(f"{base}/staff/{staff}/deactivate", json={"deactivated_on": day.isoformat()}).status_code == 200
        unavailable = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": day.isoformat(), "date_to": day.isoformat()})
        assert unavailable.status_code == 200, unavailable.text
        assert unavailable.json()["slots"] == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_public_hold_confirmation_rechecks_newly_approved_leave(database_url):
    """AC-1: leave recorded after a hold prevents the hold becoming a booking."""
    issuer = "https://identity.example.invalid/staff-hold-policy"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        staff = _setup(client, base)
        location = tenant["location_id"]
        day = date.today() + timedelta(days=8)
        _set_location_hours(client, base, location, day)
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            slug = connection.execute(text("SELECT booking_slug FROM locations WHERE tenant_id=:t AND id=:l"),
                                       {"t": tenant["tenant_id"], "l": location}).scalar_one()
        service = client.post(f"{base}/services", json={
            "name": "Held schedule cut", "duration_minutes": 30, "price_minor": 4000}).json()["id"]
        person = client.post(f"{base}/clients", json={"name": "Synthetic hold client"}).json()["id"]
        client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [location]})
        client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
        client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": location, "weekday": day.weekday(), "starts_at": "09:00", "ends_at": "11:00"})
        held = client.post(f"/v1/public/salons/{slug}/holds", json={
            "service_id": service, "staff_id": staff,
            "starts_at": datetime.combine(day, time(10), ZoneInfo("America/Toronto")).isoformat(),
            "idempotency_key": uuid.uuid4().hex})
        assert held.status_code == 201, held.text
        leave = client.post(f"{base}/staff/{staff}/leave", json={
            "starts_on": day.isoformat(), "ends_on": day.isoformat(), "approved": True})
        assert leave.status_code == 201, leave.text
        confirmed = client.post(f"/v1/public/salons/{slug}/holds/{held.json()['hold_id']}/confirm", json={
            "owner_token": held.json()["owner_token"],
            "client_name": "Synthetic Shift Client", "email": "shift@example.invalid",
            "policy_accepted": True, "policy_version": 1,
            "idempotency_key": uuid.uuid4().hex})
        assert confirmed.status_code == 409, confirmed.text
        assert confirmed.json()["detail"]["code"] == "slot_unavailable"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_staff_is_tenant_scoped(database_url):
    """AC-2: staff belong to one tenant and are invisible to another."""
    issuer = "https://identity.example.invalid/staff-6"
    other_issuer = "https://identity.example.invalid/staff-6b"
    engine, owner, tenant = _salon(database_url, issuer)
    other_engine, other_owner, other_tenant = _salon(database_url, other_issuer)
    client = _client(database_url, owner)
    other_client = _client(database_url, other_owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    other_base = f"/v1/tenants/{other_tenant['tenant_id']}"
    try:
        staff = _setup(client, base)
        assert other_client.get(f"{other_base}/staff").json()["items"] == []
        assert other_client.get(f"{other_base}/staff/{staff}/shifts").status_code == 404
        assert other_client.post(f"{other_base}/staff/{staff}/deactivate",
                                 json={"deactivated_on": "2026-12-01"}).status_code == 404
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], other_issuer)
