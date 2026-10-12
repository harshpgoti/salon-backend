"""Tests for staff profiles, qualifications, shifts, leave and locations (TASK-016, FR-020)."""

import os
import uuid

import pytest
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