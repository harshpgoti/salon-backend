"""Scheduling clock and public DST regressions (TASK-074)."""

import os
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from modules.booking import availability
from test_clients import _client, _salon, _teardown
from test_public_availability import _bookable_salon


@pytest.fixture
def database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required for PostgreSQL time regressions")
    return url


@pytest.fixture
def salon(database_url):
    issuer = "https://identity.example.invalid/time/" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    client = TestClient(_client(database_url, owner).app, raise_server_exceptions=False)
    try:
        yield client, tenant
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


@pytest.mark.parametrize("zone,day", [
    ("America/Toronto", date(2027, 3, 14)),
    ("America/Vancouver", date(2026, 3, 8)),
    ("America/St_Johns", date(2027, 3, 14)),
])
def test_nonexistent_spring_time_has_no_instant(zone, day):
    assert availability._local_instant(day, 150, ZoneInfo(zone)) is None


def test_vancouver_valid_2027_time_is_not_treated_as_a_gap():
    # BC remains UTC-7 after March 2026 (current IANA data); no 2027 spring gap.
    instant = availability._local_instant(date(2027, 3, 14), 150, ZoneInfo("America/Vancouver"))
    assert instant.astimezone(timezone.utc) == datetime(2027, 3, 14, 9, 30, tzinfo=timezone.utc)


def test_repeated_fall_time_uses_first_occurrence():
    instant = availability._local_instant(date(2026, 11, 1), 90, ZoneInfo("America/Toronto"))
    assert instant.astimezone(timezone.utc) == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    assert instant.fold == 0


@pytest.mark.parametrize("minutes", [-1, 1441])
def test_invalid_minute_has_no_instant(minutes):
    assert availability._local_instant(date(2026, 11, 1), minutes, ZoneInfo("UTC")) is None


def test_end_of_day_is_next_local_midnight():
    instant = availability._local_instant(date(2026, 11, 1), 1440, ZoneInfo("America/Toronto"))
    assert instant.isoformat() == "2026-11-02T00:00:00-05:00"


def test_entire_skipped_local_day_has_no_instant():
    assert availability._local_instant(date(2011, 12, 30), 720, ZoneInfo("Pacific/Apia")) is None


@pytest.mark.parametrize("opens,closes", [("25:00", "26:00"), ("12:99", "14:00"), ("24:00", "24:30")])
def test_invalid_hours_return_422_without_rules(salon, opens, closes):
    client, tenant = salon
    path = f"/v1/tenants/{tenant['tenant_id']}/locations/{tenant['location_id']}/hours"
    rejected = client.put(path, json={"intervals": [
        {"weekday": 0, "opens_at": opens, "closes_at": closes},
    ]})
    assert rejected.status_code == 422, rejected.text
    assert client.get(path).json()["items"] == []


@pytest.mark.parametrize("starts,ends,breaks", [
    ("25:00", "26:00", []), ("09:00", "25:00", []),
    ("09:00", "17:00", [{"start": "10:90", "end": "12:00"}]),
])
def test_invalid_shift_or_break_clock_is_422_without_write(salon, starts, ends, breaks):
    client, tenant = salon
    base = f"/v1/tenants/{tenant['tenant_id']}"
    staff = client.post(f"{base}/staff", json={"display_name": "Synthetic Staff"})
    assert staff.status_code == 201, staff.text
    assigned = client.put(f"{base}/staff/{staff.json()['id']}/locations",
                          json={"location_ids": [tenant["location_id"]]})
    assert assigned.status_code == 200, assigned.text
    path = f"{base}/staff/{staff.json()['id']}/shifts"
    rejected = client.post(path, json={
        "location_id": tenant["location_id"], "weekday": 0,
        "starts_at": starts, "ends_at": ends, "breaks": breaks,
    })
    assert rejected.status_code == 422, rejected.text
    assert client.get(path).json()["items"] == []


def test_dated_custom_hours_reject_nonexistent_boundary(salon):
    client, tenant = salon
    path = f"/v1/tenants/{tenant['tenant_id']}/locations/{tenant['location_id']}/exceptions"
    rejected = client.post(path, json={
        "exception_date": "2027-03-14", "kind": "custom",
        "opens_at": "02:30", "closes_at": "04:00",
    })
    assert rejected.status_code == 422, rejected.text
    assert client.get(path).json()["items"] == []


@pytest.mark.parametrize("day,close,break_end", [
    (date(2027, 3, 14), "04:00", None), (date(2026, 11, 1), "03:00", None),
    (date(2027, 3, 14), "04:00", "03:15"), (date(2026, 11, 1), "03:00", "02:15"),
])
def test_public_dst_slots_preserve_elapsed_duration_and_buffers(
        salon, database_url, monkeypatch, day, close, break_end):
    client, tenant = salon
    base = f"/v1/tenants/{tenant['tenant_id']}"
    slug, service, staff = _bookable_salon(client, database_url, tenant, weekdays=range(7), shift_day=day.weekday())
    published = client.put(f"{base}/locations/{tenant['location_id']}/hours", json={"intervals": [
        {"weekday": day.weekday(), "opens_at": "00:00", "closes_at": close},
    ]})
    assert published.status_code == 200, published.text
    shifted = client.post(f"{base}/staff/{staff}/shifts", json={
        "location_id": tenant["location_id"], "weekday": day.weekday(),
        "starts_at": "00:00", "ends_at": close,
        "breaks": [{"start": "01:30", "end": break_end}] if break_end else [],
    })
    assert shifted.status_code == 201, shifted.text
    updated = client.patch(f"{base}/services/{service}", json={
        "pre_buffer_minutes": 15, "post_buffer_minutes": 15, "expected_version": 1,
    })
    assert updated.status_code == 200, updated.text
    now = datetime.combine(day - timedelta(days=1), datetime.min.time(), timezone.utc)

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)

    monkeypatch.setattr(availability, "datetime", FrozenDateTime)
    response = client.get(f"/v1/public/salons/{slug}/availability", params={
        "service_id": service, "date_from": day.isoformat(), "date_to": day.isoformat(),
    })
    assert response.status_code == 200, response.text
    body = response.json()
    slots = body["slots"]
    assert slots, body
    assert len({(s["staff_id"], s["starts_at"]) for s in slots}) == len(slots)
    zone = ZoneInfo("America/Toronto")
    opening = datetime.combine(day, datetime.min.time(), zone).astimezone(timezone.utc)
    closing = datetime.combine(day, datetime.strptime(close, "%H:%M").time(), zone).astimezone(timezone.utc)
    for slot in slots:
        start = datetime.fromisoformat(slot["starts_at"])
        end = datetime.fromisoformat(slot["ends_at"])
        assert end - start == timedelta(minutes=60), slot
        assert start.astimezone(timezone.utc) - timedelta(minutes=15) >= opening
        assert end.astimezone(timezone.utc) + timedelta(minutes=15) <= closing
        assert "client_id" not in slot and "appointment_id" not in slot
        if break_end:
            break_start = datetime.combine(day, datetime.strptime("01:30", "%H:%M").time(), zone)
            break_close = datetime.combine(day, datetime.strptime(break_end, "%H:%M").time(), zone)
            assert (end.astimezone(timezone.utc) + timedelta(minutes=15) <= break_start.astimezone(timezone.utc)
                    or start.astimezone(timezone.utc) - timedelta(minutes=15) >= break_close.astimezone(timezone.utc))
    if day.month == 3 and not break_end:
        assert not any(s["local_time"].startswith("02:") for s in slots)
        crossing = next(s for s in slots if s["local_time"] == "01:00")
        assert datetime.fromisoformat(crossing["ends_at"]).hour == 3


def test_full_day_hours_and_shift_accept_24_only_as_end(salon):
    client, tenant = salon
    base = f"/v1/tenants/{tenant['tenant_id']}"
    hours = client.put(f"{base}/locations/{tenant['location_id']}/hours", json={"intervals": [
        {"weekday": 0, "opens_at": "00:00", "closes_at": "24:00"},
    ]})
    assert hours.status_code == 200, hours.text
    listed = client.get(f"{base}/locations/{tenant['location_id']}/hours")
    assert listed.status_code == 200, listed.text
    assert listed.json()["items"][0]["closes_at"] == "24:00"
    staff = client.post(f"{base}/staff", json={"display_name": "All day"}).json()["id"]
    assigned = client.put(f"{base}/staff/{staff}/locations",
                          json={"location_ids": [tenant["location_id"]]})
    assert assigned.status_code == 200, assigned.text
    shifted = client.post(f"{base}/staff/{staff}/shifts", json={
        "location_id": tenant["location_id"], "weekday": 0,
        "starts_at": "00:00", "ends_at": "24:00",
    })
    assert shifted.status_code == 201, shifted.text


def test_custom_exception_can_publish_a_24_end_without_losing_it(salon):
    client, tenant = salon
    path = f"/v1/tenants/{tenant['tenant_id']}/locations/{tenant['location_id']}/exceptions"
    created = client.post(path, json={
        "exception_date": "2027-03-14", "kind": "custom",
        "opens_at": "00:00", "closes_at": "24:00",
    })
    assert created.status_code == 201, created.text
    published = client.post(f"{path}/{created.json()['id']}/publish", json={"published": True})
    assert published.status_code == 200, published.text
    assert published.json()["closes_at"] == "24:00"
    assert client.get(path).json()["items"][0]["closes_at"] == "24:00"
