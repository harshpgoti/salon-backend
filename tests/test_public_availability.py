"""Tests for public availability and booking policy (TASK-019, FR-023/FR-027/FR-028)."""

import os
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import create_engine, text
from test_clients import _salon, _client, _teardown


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _slug(database_url, tenant_id, location_id):
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
            return connection.execute(
                text("SELECT booking_slug FROM locations WHERE tenant_id=:t AND id=:l"),
                {"t": tenant_id, "l": location_id}).scalar_one()
    finally:
        engine.dispose()


def _bookable_salon(client, database_url, tenant, weekdays=range(6), shift_day=None):
    """Publish hours, one service and one qualified staff member; return (slug, service_id, staff_id)."""
    base = f"/v1/tenants/{tenant['tenant_id']}"
    loc = tenant["location_id"]
    slug = _slug(database_url, tenant["tenant_id"], loc)
    client.put(f"{base}/locations/{loc}/hours", json={
        "intervals": [{"weekday": d, "opens_at": "09:00", "closes_at": "17:00"} for d in weekdays]})
    service = client.post(f"{base}/services",
                          json={"name": "Cut", "duration_minutes": 60, "price_minor": 6000}).json()["id"]
    staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
    client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [loc]})
    client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
    client.post(f"{base}/staff/{staff}/shifts", json={
        "location_id": loc, "weekday": shift_day if shift_day is not None
        else (date.today() + timedelta(days=1)).weekday(),
        "starts_at": "09:00", "ends_at": "17:00"})
    return slug, service, staff


def test_availability_intersects_hours_shift_and_qualification(database_url):
    """AC-1: only policy-valid slots are offered, in the salon timezone."""
    issuer = "https://identity.example.invalid/avail-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        slug, service, staff = _bookable_salon(client, database_url, tenant)
        tomorrow = date.today() + timedelta(days=1)
        resp = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": tomorrow.isoformat(),
            "date_to": (tomorrow + timedelta(days=1)).isoformat()})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["timezone"] == "America/Toronto"
        assert len(body["slots"]) > 0
        slot = body["slots"][0]
        assert slot["staff_name"] == "Ava" and slot["timezone"] == "America/Toronto"
        # The public payload never carries client or appointment detail (FR-028).
        assert "client" not in str(body).lower()
        assert "appointment" not in str(body).lower()

        # Unqualifying the staff member removes every slot.
        client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": []})
        empty = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": tomorrow.isoformat(),
            "date_to": (tomorrow + timedelta(days=1)).isoformat()}).json()
        assert empty["slots"] == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_approved_leave_removes_availability(database_url):
    """AC-1: approved leave is intersected out of the offer."""
    issuer = "https://identity.example.invalid/avail-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        slug, service, staff = _bookable_salon(client, database_url, tenant)
        tomorrow = date.today() + timedelta(days=1)
        params = {"service_id": service, "date_from": tomorrow.isoformat(),
                  "date_to": (tomorrow + timedelta(days=1)).isoformat()}
        assert len(client.get(f"/v1/public/salons/{slug}/availability", params=params).json()["slots"]) > 0

        client.post(f"{base}/staff/{staff}/leave", json={
            "starts_on": tomorrow.isoformat(), "ends_on": (tomorrow + timedelta(days=1)).isoformat(),
            "approved": True})
        assert client.get(f"/v1/public/salons/{slug}/availability", params=params).json()["slots"] == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_published_closure_removes_availability(database_url):
    """AC-1: a published closed exception removes that day's slots."""
    issuer = "https://identity.example.invalid/avail-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        slug, service, staff = _bookable_salon(client, database_url, tenant)
        tomorrow = date.today() + timedelta(days=1)
        params = {"service_id": service, "date_from": tomorrow.isoformat(),
                  "date_to": (tomorrow + timedelta(days=1)).isoformat()}
        assert len(client.get(f"/v1/public/salons/{slug}/availability", params=params).json()["slots"]) > 0

        exc = client.post(f"{base}/locations/{tenant['location_id']}/exceptions",
                          json={"exception_date": tomorrow.isoformat(), "kind": "closed"}).json()
        # Unpublished exceptions do not yet affect the client view.
        assert len(client.get(f"/v1/public/salons/{slug}/availability", params=params).json()["slots"]) > 0
        client.post(f"{base}/locations/{tenant['location_id']}/exceptions/{exc['id']}/publish",
                    json={"published": True})
        assert client.get(f"/v1/public/salons/{slug}/availability", params=params).json()["slots"] == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_graceful_errors_and_bounds(database_url):
    """AC-1: bad input and unknown salons return generic errors, never internals."""
    issuer = "https://identity.example.invalid/avail-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        slug, service, staff = _bookable_salon(client, database_url, tenant)
        tomorrow = date.today() + timedelta(days=1)

        unknown = client.get("/v1/public/salons/no-such-salon/availability", params={
            "service_id": service, "date_from": tomorrow.isoformat(), "date_to": tomorrow.isoformat()})
        assert unknown.status_code == 404
        assert unknown.json()["detail"]["code"] == "salon_not_found"

        inverted = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": tomorrow.isoformat(),
            "date_to": (tomorrow - timedelta(days=5)).isoformat()})
        assert inverted.status_code == 422

        too_wide = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": tomorrow.isoformat(),
            "date_to": (tomorrow + timedelta(days=90)).isoformat()})
        assert too_wide.status_code == 422

        # A service the salon does not offer is a generic 404, not a reason string.
        unknown_service = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": str(uuid.uuid4()), "date_from": tomorrow.isoformat(),
            "date_to": tomorrow.isoformat()})
        assert unknown_service.status_code == 404
        assert unknown_service.json()["detail"]["code"] == "availability_not_found"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_service_needing_a_resource_is_not_publicly_bookable(database_url):
    """AC-1: R1 public booking offers only services that need no exclusive resource."""
    issuer = "https://identity.example.invalid/avail-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        slug, service, staff = _bookable_salon(client, database_url, tenant)
        loc = tenant["location_id"]
        room = client.post(f"{base}/resources", json={"location_id": loc, "name": "Room", "kind": "room"}).json()["id"]
        client.put(f"{base}/services/{service}/resources", json={"resource_ids": [room]})

        tomorrow = date.today() + timedelta(days=1)
        resp = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": tomorrow.isoformat(), "date_to": tomorrow.isoformat()})
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "availability_not_found"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


import uuid  # noqa: E402  (used by the bounds test above)