"""Tests for closure impact resolution (TASK-026, FR-025/FR-026).

AC-1: Closure blocks new slots and lists existing appointments to resolve
AC-2: Affected notices and unreachable follow-up tasks reflect explicit decisions
"""

import os
import uuid
from datetime import date, timedelta, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text
from test_clients import _salon, _client, _teardown


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _bookable_salon_with_appointment(client, database_url, tenant):
    """Set up a salon with hours, a service, qualified staff, and one appointment."""
    base = f"/v1/tenants/{tenant['tenant_id']}"
    loc = tenant["location_id"]
    # Publish hours for tomorrow
    tomorrow = date.today() + timedelta(days=1)
    client.put(f"{base}/locations/{loc}/hours", json={
        "intervals": [{"weekday": tomorrow.weekday(), "opens_at": "09:00", "closes_at": "17:00"}]})
    # Add a service
    service = client.post(f"{base}/services", json={
        "name": "Cut", "duration_minutes": 60, "price_minor": 6000}).json()["id"]
    # Add staff
    staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
    client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [loc]})
    client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
    client.post(f"{base}/staff/{staff}/shifts", json={
        "location_id": loc, "weekday": tomorrow.weekday(),
        "starts_at": "09:00", "ends_at": "17:00"})
    # Add a client
    client_row = client.post(f"{base}/clients", json={"name": "Client One"}).json()
    client_id = client_row["id"]
    # Book an appointment for tomorrow at 10:00 UTC
    starts = datetime.combine(tomorrow, datetime.min.time()).replace(
        hour=10, tzinfo=ZoneInfo("America/Toronto"))
    booked = client.post(f"{base}/appointments", json={
        "client_id": client_id, "staff_id": staff, "service_id": service,
        "location_id": loc,
        "starts_at": starts.isoformat(), "source": "staff",
        "idempotency_key": "test-closure-key-1"})
    assert booked.status_code == 201, booked.text
    response = booked.json()
    appt_id = response["appointment_id"]
    return loc, service, staff, client_id, appt_id, tomorrow


def test_closure_impact_records_affected_appointment(database_url):
    """AC-1: publishing a closure registers affected appointments for resolution."""
    issuer = "https://identity.example.invalid/closure-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, service, staff, client_id, appt_id, tomorrow = _bookable_salon_with_appointment(
            client, database_url, tenant)
        # Create a closure exception for tomorrow and publish it
        exc = client.post(f"{base}/locations/{loc}/exceptions", json={
            "exception_date": tomorrow.isoformat(), "kind": "closed"}).json()["id"]
        client.post(f"{base}/locations/{loc}/exceptions/{exc}/publish", json={"published": True})
        # Record the impact
        impact = client.post(f"{base}/closures/{loc}/impacts", json={
            "appointment_id": appt_id, "location_id": loc,
            "exception_date": tomorrow.isoformat()}).json()
        assert impact["status"] == "active"
        assert impact["appointment_id"] == appt_id
        assert impact["client_id"] == client_id
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_closure_impact_rejects_unpublished_exception(database_url):
    """AC-1: an unpublished exception cannot have impacts recorded against it."""
    issuer = "https://identity.example.invalid/closure-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, service, staff, client_id, appt_id, tomorrow = _bookable_salon_with_appointment(
            client, database_url, tenant)
        # Create a closure exception but do NOT publish it
        exc = client.post(f"{base}/locations/{loc}/exceptions", json={
            "exception_date": tomorrow.isoformat(), "kind": "closed"}).json()["id"]
        # Recording impact against an unpublished exception should fail
        resp = client.post(f"{base}/closures/{loc}/impacts", json={
            "appointment_id": appt_id, "location_id": loc,
            "exception_date": tomorrow.isoformat()})
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "exception_not_published"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_closure_impact_resolved_updates_status(database_url):
    """AC-2: resolving an impact as cancelled changes its status."""
    issuer = "https://identity.example.invalid/closure-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, service, staff, client_id, appt_id, tomorrow = _bookable_salon_with_appointment(
            client, database_url, tenant)
        exc = client.post(f"{base}/locations/{loc}/exceptions", json={
            "exception_date": tomorrow.isoformat(), "kind": "closed"}).json()["id"]
        client.post(f"{base}/locations/{loc}/exceptions/{exc}/publish", json={"published": True})
        impact = client.post(f"{base}/closures/{loc}/impacts", json={
            "appointment_id": appt_id, "location_id": loc,
            "exception_date": tomorrow.isoformat()}).json()
        impact_id = impact["id"]
        # Resolve as cancelled using the staff member as the resolver
        resolution = {
            "impact_id": impact_id, "resolution": "cancelled",
            "reason": "Storm closure",
            "impact_version": 1, "appointment_version": 1,
            "idempotency_key": "closure-resolve-cancel-1"}
        response = client.post(f"{base}/closures/{loc}/impacts/resolve", json=resolution)
        assert response.status_code == 200, response.text
        resolved = response.json()
        replay = client.post(f"{base}/closures/{loc}/impacts/resolve", json=resolution)
        assert replay.status_code == 200, replay.text
        assert replay.json() == resolved
        conflicting_retry = client.post(f"{base}/closures/{loc}/impacts/resolve", json={
            **resolution, "reason": "Changed after submission"})
        assert conflicting_retry.status_code == 409
        assert conflicting_retry.json()["detail"]["code"] == "idempotency_conflict"
        assert resolved["resolution"] == "cancelled"
        assert resolved["resolved_at"] is not None
        appointment = client.get(f"{base}/appointments", params={
            "limit": 100, "date_from": tomorrow.isoformat(), "date_to": tomorrow.isoformat()
        }).json()
        assert next(row for row in appointment["items"] if row["id"] == appt_id)["status"] == "cancelled"
        # Listing active impacts should no longer show this one
        active = client.get(f"{base}/closures/{loc}/impacts",
                            params={"exception_date": tomorrow.isoformat()}).json()
        assert len(active["impacts"]) == 0
        # Listing all (including resolved) should show it
        all_impacts = client.get(f"{base}/closures/{loc}/impacts",
                                 params={"exception_date": tomorrow.isoformat(),
                                         "include_resolved": "true"}).json()
        assert len(all_impacts["impacts"]) == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_closure_impact_left_keeps_reason_null(database_url):
    """AC-2: an explicit leave decision does not invent a reason."""
    issuer = "https://identity.example.invalid/closure-left-null"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, _, _, _, appt_id, tomorrow = _bookable_salon_with_appointment(
            client, database_url, tenant)
        exc = client.post(f"{base}/locations/{loc}/exceptions", json={
            "exception_date": tomorrow.isoformat(), "kind": "closed"}).json()["id"]
        client.post(f"{base}/locations/{loc}/exceptions/{exc}/publish", json={"published": True})
        impact = client.post(f"{base}/closures/{loc}/impacts", json={
            "appointment_id": appt_id, "location_id": loc,
            "exception_date": tomorrow.isoformat()}).json()
        resolved = client.post(f"{base}/closures/{loc}/impacts/resolve", json={
            "impact_id": impact["id"], "resolution": "left",
            "impact_version": 1, "appointment_version": 1,
            "idempotency_key": "closure-leave-null-1"})
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["resolution_reason"] is None
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_closure_impact_unsent_returns_active_only(database_url):
    """AC-2: the unsent endpoint returns only active, unresolved impacts for follow-up."""
    issuer = "https://identity.example.invalid/closure-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, service, staff, client_id, appt_id, tomorrow = _bookable_salon_with_appointment(
            client, database_url, tenant)
        exc = client.post(f"{base}/locations/{loc}/exceptions", json={
            "exception_date": tomorrow.isoformat(), "kind": "closed"}).json()["id"]
        client.post(f"{base}/locations/{loc}/exceptions/{exc}/publish", json={"published": True})
        client.post(f"{base}/closures/{loc}/impacts", json={
            "appointment_id": appt_id, "location_id": loc,
            "exception_date": tomorrow.isoformat()})
        # Unsent list should show the active impact
        unsent = client.get(f"{base}/closures/{loc}/impacts/unsent").json()
        assert unsent["total"] == 1
        assert unsent["unsent_impacts"][0]["appointment_id"] == appt_id
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def _book_at(client, base, location_id, service, staff, client_id, tomorrow, hour, key):
    starts = datetime.combine(tomorrow, datetime.min.time()).replace(
        hour=hour, tzinfo=ZoneInfo("America/Toronto"))
    created = client.post(f"{base}/appointments", json={
        "client_id": client_id, "staff_id": staff, "service_id": service,
        "location_id": location_id,
        "starts_at": starts.isoformat(), "source": "staff",
        "idempotency_key": key})
    assert created.status_code == 201, created.text
    return created.json()["appointment_id"]


def test_closure_preview_lists_live_and_skips_history(database_url):
    """AC-1: preview finds overlapping live appointments, never history."""
    issuer = "https://identity.example.invalid/closure-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, service, staff, client_id, appt_id, tomorrow = _bookable_salon_with_appointment(
            client, database_url, tenant)
        doomed = _book_at(client, base, loc, service, staff, client_id, tomorrow, 14, "closure-prev-2")
        cancelled = client.post(f"{base}/appointments/{doomed}/transitions", json={
            "to_status": "cancelled", "expected_version": 1,
            "idempotency_key": "closure-prev-cancel-1", "reason": "client request"})
        assert cancelled.status_code == 200, cancelled.text
        exc = client.post(f"{base}/locations/{loc}/exceptions", json={
            "exception_date": tomorrow.isoformat(), "kind": "closed"}).json()["id"]
        client.post(f"{base}/locations/{loc}/exceptions/{exc}/publish", json={"published": True})
        preview = client.get(f"{base}/closures/{loc}/impacts/preview",
                             params={"exception_date": tomorrow.isoformat()}).json()
        found = {item["appointment_id"] for item in preview["items"]}
        assert appt_id in found
        assert doomed not in found
        assert preview["total"] == 1
        # Recording the impact marks it in later previews.
        rec = client.post(f"{base}/closures/{loc}/impacts", json={
            "appointment_id": appt_id, "location_id": loc,
            "exception_date": tomorrow.isoformat()})
        assert rec.status_code == 200, rec.text
        recorded = rec.json()
        again = client.get(f"{base}/closures/{loc}/impacts/preview",
                           params={"exception_date": tomorrow.isoformat()}).json()
        marked = next(i for i in again["items"] if i["appointment_id"] == appt_id)
        assert marked["impact_id"] == recorded["id"]
        assert marked["impact_status"] == "active"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_closure_preview_requires_published_closure(database_url):
    """AC-1: a normal open date cannot be presented as an affected closure."""
    issuer = "https://identity.example.invalid/closure-preview-open-day"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, _, _, _, _, tomorrow = _bookable_salon_with_appointment(client, database_url, tenant)
        preview = client.get(f"{base}/closures/{loc}/impacts/preview",
                             params={"exception_date": tomorrow.isoformat()})
        assert preview.status_code == 404
        assert preview.json()["detail"]["code"] == "published_closure_not_found"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_reduced_custom_hours_flag_only_outside_appointments_and_block_new_booking(database_url):
    """AC-1: published shorter hours resolve displaced bookings and block commits."""
    issuer = "https://identity.example.invalid/closure-reduced-hours"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, service, staff, client_id, outside_id, tomorrow = _bookable_salon_with_appointment(
            client, database_url, tenant)
        inside_id = _book_at(client, base, loc, service, staff, client_id, tomorrow, 14,
                             "reduced-hours-inside")
        exception = client.post(f"{base}/locations/{loc}/exceptions", json={
            "exception_date": tomorrow.isoformat(), "kind": "custom",
            "opens_at": "11:00", "closes_at": "17:00"}).json()["id"]
        client.post(f"{base}/locations/{loc}/exceptions/{exception}/publish", json={"published": True})

        preview = client.get(f"{base}/closures/{loc}/impacts/preview",
                             params={"exception_date": tomorrow.isoformat()})
        assert preview.status_code == 200, preview.text
        assert [item["appointment_id"] for item in preview.json()["items"]] == [outside_id]
        recorded = client.post(f"{base}/closures/{loc}/impacts", json={
            "appointment_id": outside_id, "location_id": loc,
            "exception_date": tomorrow.isoformat()})
        assert recorded.status_code == 200, recorded.text
        unaffected = client.post(f"{base}/closures/{loc}/impacts", json={
            "appointment_id": inside_id, "location_id": loc,
            "exception_date": tomorrow.isoformat()})
        assert unaffected.status_code == 422
        assert unaffected.json()["detail"]["code"] == "appointment_not_affected"

        outside_time = datetime.combine(tomorrow, datetime.min.time()).replace(
            hour=9, tzinfo=ZoneInfo("America/Toronto"))
        rescheduled = client.post(f"{base}/appointments/{outside_id}/reschedule", json={
            "starts_at": outside_time.isoformat(), "expected_version": 1,
            "idempotency_key": "reduced-hours-reschedule"})
        assert rescheduled.status_code == 409, rescheduled.text
        assert rescheduled.json()["detail"]["code"] == "slot_unavailable"

        new_client = client.post(f"{base}/clients", json={"name": "Early Client"}).json()
        blocked = client.post(f"{base}/appointments", json={
            "client_id": new_client["id"], "staff_id": staff, "service_id": service,
            "location_id": loc, "starts_at": outside_time.isoformat(), "source": "staff",
            "idempotency_key": "reduced-hours-early-booking"})
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["detail"]["code"] == "slot_unavailable"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_closure_unsent_flags_unreachable_follow_up(database_url):
    """AC-2: suppressed or contact-less clients surface as staff follow-up."""
    issuer = "https://identity.example.invalid/closure-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, service, staff, client_id, appt_id, tomorrow = _bookable_salon_with_appointment(
            client, database_url, tenant)
        # Client One has no contacts: unreachable by definition.
        # Second client with an email contact that is suppressed on email.
        second = client.post(f"{base}/clients", json={
            "name": "Quiet Client",
            "contacts": [{"kind": "email", "value": "quiet@example.com", "is_primary": True}],
        }).json()
        appt2 = _book_at(client, base, loc, service, staff, second["id"], tomorrow, 14, "closure-fu-2")
        exc = client.post(f"{base}/locations/{loc}/exceptions", json={
            "exception_date": tomorrow.isoformat(), "kind": "closed"}).json()["id"]
        client.post(f"{base}/locations/{loc}/exceptions/{exc}/publish", json={"published": True})
        client.post(f"{base}/closures/{loc}/impacts", json={
            "appointment_id": appt_id, "location_id": loc,
            "exception_date": tomorrow.isoformat()})
        client.post(f"{base}/closures/{loc}/impacts", json={
            "appointment_id": appt2, "location_id": loc,
            "exception_date": tomorrow.isoformat()})
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                               {"t": tenant["tenant_id"]})
            connection.execute(
                text("""INSERT INTO suppressions (tenant_id, id, client_id, contact_value,
                                                  channel, reason)
                        VALUES (:t, gen_random_uuid(), :c, 'quiet@example.com',
                                'email', 'unsubscribe')"""),
                {"t": tenant["tenant_id"], "c": second["id"]})
        unsent = client.get(f"{base}/closures/{loc}/impacts/unsent").json()
        assert unsent["total"] == 2
        by_appt = {item["appointment_id"]: item for item in unsent["unsent_impacts"]}
        lonely = by_appt[appt_id]
        assert lonely["reachable"] is False and lonely["follow_up"] is True
        assert lonely["contact_kind"] is None
        quiet = by_appt[appt2]
        assert quiet["reachable"] is False and quiet["follow_up"] is True
        assert quiet["contact_kind"] == "email"
        assert quiet["suppressed_channels"] == ["email"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_published_closure_rejects_direct_manual_booking(database_url):
    """AC-1: booking commands cannot bypass a published location closure."""
    issuer = "https://identity.example.invalid/closure-7"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, service, staff, client_id, _, tomorrow = _bookable_salon_with_appointment(
            client, database_url, tenant)
        exc = client.post(f"{base}/locations/{loc}/exceptions", json={
            "exception_date": tomorrow.isoformat(), "kind": "closed"}).json()["id"]
        client.post(f"{base}/locations/{loc}/exceptions/{exc}/publish", json={"published": True})
        attempt = client.post(f"{base}/appointments", json={
            "client_id": client_id, "staff_id": staff, "service_id": service,
            "location_id": loc,
            "starts_at": datetime.combine(tomorrow, datetime.min.time()).replace(
                hour=14, tzinfo=ZoneInfo("America/Toronto")).isoformat(),
            "source": "staff", "idempotency_key": "closure-direct-block-1"})
        assert attempt.status_code == 409, attempt.text
        assert attempt.json()["detail"]["code"] == "slot_unavailable"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_closure_impact_rejects_appointment_outside_closed_location_date(database_url):
    """AC-1: impacts belong to a booking actually affected by that location closure."""
    issuer = "https://identity.example.invalid/closure-8"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, _, _, _, appt_id, tomorrow = _bookable_salon_with_appointment(
            client, database_url, tenant)
        other_date = tomorrow + timedelta(days=1)
        exc = client.post(f"{base}/locations/{loc}/exceptions", json={
            "exception_date": other_date.isoformat(), "kind": "closed"}).json()["id"]
        client.post(f"{base}/locations/{loc}/exceptions/{exc}/publish", json={"published": True})
        attempt = client.post(f"{base}/closures/{loc}/impacts", json={
            "appointment_id": appt_id, "location_id": loc,
            "exception_date": other_date.isoformat()})
        assert attempt.status_code == 422, attempt.text
        assert attempt.json()["detail"]["code"] == "appointment_not_affected"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_published_closure_rejects_reschedule_into_closed_date(database_url):
    """AC-1: reschedule commands share the published closure guard."""
    issuer = "https://identity.example.invalid/closure-9"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, _, _, _, appt_id, tomorrow = _bookable_salon_with_appointment(
            client, database_url, tenant)
        exc = client.post(f"{base}/locations/{loc}/exceptions", json={
            "exception_date": tomorrow.isoformat(), "kind": "closed"}).json()["id"]
        client.post(f"{base}/locations/{loc}/exceptions/{exc}/publish", json={"published": True})
        attempt = client.post(f"{base}/appointments/{appt_id}/reschedule", json={
            "starts_at": datetime.combine(tomorrow, datetime.min.time()).replace(
                hour=14, tzinfo=ZoneInfo("America/Toronto")).isoformat(),
            "expected_version": 1, "idempotency_key": "closure-reschedule-1"})
        assert attempt.status_code == 409, attempt.text
        assert attempt.json()["detail"]["code"] == "slot_unavailable"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_closure_permissions_mirror_hours_scopes():
    """AC-1: managers resolve closures, receptionists read them (like hours)."""
    from modules.identity.main import ROLE_PERMISSIONS
    assert "closures.manage" in ROLE_PERMISSIONS["manager"]
    assert "closures.read" in ROLE_PERMISSIONS["manager"]
    assert "closures.read" in ROLE_PERMISSIONS["receptionist"]
    assert "closures.manage" not in ROLE_PERMISSIONS["receptionist"]
    assert "closures.manage" not in ROLE_PERMISSIONS["stylist"]
