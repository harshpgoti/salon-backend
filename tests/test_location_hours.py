"""Tests for weekly hours and dated exceptions (TASK-017, FR-022/FR-024)."""

import os
from datetime import date, datetime, timedelta
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


def test_weekly_hours_publish_multiple_intervals_per_day(database_url):
    """AC-1: split hours persist per weekday with a local effective date."""
    issuer = "https://identity.example.invalid/hours-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    loc = tenant["location_id"]
    try:
        resp = client.put(f"{base}/locations/{loc}/hours", json={"intervals": [
            {"weekday": 0, "opens_at": "09:00", "closes_at": "12:00"},
            {"weekday": 0, "opens_at": "13:00", "closes_at": "17:00"},
            {"weekday": 5, "opens_at": "10:00", "closes_at": "14:00"},
        ]})
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["intervals"]) == 3

        listed = client.get(f"{base}/locations/{loc}/hours").json()["items"]
        monday = [i for i in listed if i["weekday"] == 0]
        assert len(monday) == 2
        assert listed[0]["effective_from"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_invalid_and_overlapping_hours_are_rejected(database_url):
    """AC-2: an overnight or overlapping interval is refused, not stored."""
    issuer = "https://identity.example.invalid/hours-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    loc = tenant["location_id"]
    try:
        overnight = client.put(f"{base}/locations/{loc}/hours", json={"intervals": [
            {"weekday": 0, "opens_at": "20:00", "closes_at": "04:00"}]})
        assert overnight.status_code == 422
        assert overnight.json()["detail"]["code"] == "interval_closes_before_opens"

        overlap = client.put(f"{base}/locations/{loc}/hours", json={"intervals": [
            {"weekday": 0, "opens_at": "09:00", "closes_at": "13:00"},
            {"weekday": 0, "opens_at": "12:00", "closes_at": "17:00"}]})
        assert overlap.status_code == 422
        assert overlap.json()["detail"]["code"] == "hours_overlap"

        bad_weekday = client.put(f"{base}/locations/{loc}/hours", json={"intervals": [
            {"weekday": 9, "opens_at": "09:00", "closes_at": "17:00"}]})
        assert bad_weekday.status_code == 422

        bad_date = client.put(f"{base}/locations/{loc}/hours", json={
            "effective_from": "1800-01-01",
            "intervals": [{"weekday": 0, "opens_at": "09:00", "closes_at": "17:00"}]})
        assert bad_date.status_code == 422
        assert bad_date.json()["detail"]["code"] == "date_out_of_range"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_weekly_hours_reject_nonexistent_spring_transition_time(database_url):
    """AC-2: recurring local clocks cannot target a DST gap in the location zone."""
    issuer = "https://identity.example.invalid/hours-weekly-dst-gap"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    location = tenant["location_id"]
    try:
        rejected = client.put(f"{base}/locations/{location}/hours", json={
            "effective_from": "2027-01-01",
            "intervals": [{"weekday": 6, "opens_at": "02:30", "closes_at": "04:00"}],
        })
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["detail"]["code"] == "nonexistent_local_time"
        assert client.get(f"{base}/locations/{location}/hours").json()["items"] == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_future_weekly_hours_keep_prior_period_and_publish_effective_dates(database_url):
    """AC-1/AC-2: weekly versions apply only inside their local effective period."""
    issuer = "https://identity.example.invalid/hours-effective-dates"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    location = tenant["location_id"]
    try:
        today = date.today()
        change_on = today + timedelta(days=21)
        prior_day = change_on - timedelta(days=7)
        initial = client.put(f"{base}/locations/{location}/hours", json={
            "effective_from": today.isoformat(),
            "intervals": [{"weekday": day, "opens_at": "09:00", "closes_at": "17:00"} for day in range(7)],
        })
        assert initial.status_code == 200, initial.text
        service = client.post(f"{base}/services", json={
            "name": "Effective hours cut", "duration_minutes": 30, "price_minor": 4000}).json()["id"]
        staff = client.post(f"{base}/staff", json={"display_name": "Effective hours stylist"}).json()["id"]
        assert client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [location]}).status_code == 200
        assert client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]}).status_code == 200
        shift = client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": location, "weekday": change_on.weekday(), "starts_at": "09:00", "ends_at": "17:00"})
        assert shift.status_code == 201, shift.text
        upcoming = client.put(f"{base}/locations/{location}/hours", json={
            "effective_from": change_on.isoformat(),
            "intervals": [{"weekday": change_on.weekday(), "opens_at": "11:00", "closes_at": "15:00"}],
        })
        assert upcoming.status_code == 200, upcoming.text

        timeline = client.get(f"{base}/locations/{location}/hours").json()["items"]
        periods = [row for row in timeline if row["weekday"] == change_on.weekday()]
        assert {(row["effective_from"], row["effective_to"], row["opens_at"]) for row in periods} == {
            (today.isoformat(), (change_on - timedelta(days=1)).isoformat(), "09:00"),
            (change_on.isoformat(), None, "11:00"),
        }

        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant["tenant_id"]})
            slug = connection.execute(text("SELECT booking_slug FROM locations WHERE tenant_id=:tenant AND id=:location"),
                                      {"tenant": tenant["tenant_id"], "location": location}).scalar_one()
        zone = ZoneInfo("America/Toronto")
        before = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": prior_day.isoformat(), "date_to": prior_day.isoformat(),
        })
        after = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": change_on.isoformat(), "date_to": change_on.isoformat(),
        })
        assert before.status_code == 200 and before.json()["slots"], before.text
        assert after.status_code == 200 and after.json()["slots"], after.text
        before_times = {datetime.fromisoformat(row["starts_at"]).astimezone(zone).strftime("%H:%M")
                        for row in before.json()["slots"]}
        after_times = {datetime.fromisoformat(row["starts_at"]).astimezone(zone).strftime("%H:%M")
                       for row in after.json()["slots"]}
        assert {"09:00", "16:30"}.issubset(before_times)
        assert "09:00" not in after_times and {"11:00", "14:30"}.issubset(after_times)

        public = client.get(f"/v1/public/salons/{slug}/hours")
        assert public.status_code == 200, public.text
        public_body = public.json()
        assert public_body["timezone"] == "America/Toronto"
        public_periods = [row for row in public_body["weekly"] if row["weekday"] == change_on.weekday()]
        assert [(row["effective_from"], row["opens_at"]) for row in public_periods] == [
            (today.isoformat(), "09:00"), (change_on.isoformat(), "11:00"),
        ]
        assert "tenant_id" not in public_body and "location_id" not in public_body
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_republish_supersedes_and_keeps_history(database_url):
    """AC-2: changed rules retain the prior version; history is readable."""
    issuer = "https://identity.example.invalid/hours-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    loc = tenant["location_id"]
    try:
        client.put(f"{base}/locations/{loc}/hours", json={"intervals": [
            {"weekday": 0, "opens_at": "09:00", "closes_at": "17:00"}]})
        client.put(f"{base}/locations/{loc}/hours", json={"intervals": [
            {"weekday": 0, "opens_at": "08:00", "closes_at": "16:00"}]})

        current = client.get(f"{base}/locations/{loc}/hours").json()["items"]
        assert len(current) == 1 and current[0]["opens_at"] == "08:00"

        history = client.get(f"{base}/locations/{loc}/hours",
                             params={"include_history": "true"}).json()["items"]
        assert len(history) == 2
        assert any(h["superseded_at"] for h in history)
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_closed_and_custom_exceptions_are_validated(database_url):
    """AC-1: a closure has no hours; a custom day must carry a valid interval."""
    issuer = "https://identity.example.invalid/hours-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    loc = tenant["location_id"]
    try:
        closed = client.post(f"{base}/locations/{loc}/exceptions",
                             json={"exception_date": "2026-12-25", "kind": "closed", "label": "Christmas"})
        assert closed.status_code == 201

        custom = client.post(f"{base}/locations/{loc}/exceptions",
                             json={"exception_date": "2026-12-24", "kind": "custom",
                                   "opens_at": "10:00", "closes_at": "15:00"})
        assert custom.status_code == 201

        # A closed day cannot carry hours.
        bad_closed = client.post(f"{base}/locations/{loc}/exceptions",
                                 json={"exception_date": "2026-12-26", "kind": "closed",
                                       "opens_at": "10:00", "closes_at": "15:00"})
        assert bad_closed.status_code == 422

        # A custom day must carry hours.
        bad_custom = client.post(f"{base}/locations/{loc}/exceptions",
                                 json={"exception_date": "2026-12-27", "kind": "custom"})
        assert bad_custom.status_code == 422

        # The same date twice is a conflict, not a silent overwrite.
        duplicate = client.post(f"{base}/locations/{loc}/exceptions",
                                json={"exception_date": "2026-12-25", "kind": "closed"})
        assert duplicate.status_code == 409
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_unpublished_exception_is_hidden_from_public_hours(database_url):
    """AC-1: only published exceptions appear in the client-facing hours."""
    issuer = "https://identity.example.invalid/hours-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    loc = tenant["location_id"]
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant["tenant_id"]})
            slug = connection.execute(text("SELECT booking_slug FROM locations WHERE tenant_id=:tenant AND id=:location"),
                                      {"tenant": tenant["tenant_id"], "location": loc}).scalar_one()
        client.put(f"{base}/locations/{loc}/hours", json={"intervals": [
            {"weekday": 0, "opens_at": "09:00", "closes_at": "17:00"}]})
        exc = client.post(f"{base}/locations/{loc}/exceptions",
                          json={"exception_date": "2026-11-11", "kind": "closed", "label": "Remembrance Day"}).json()

        public = client.get(f"{base}/locations/{loc}/public-hours").json()
        assert public["weekly"] and public["exceptions"] == []
        anonymous = client.get(f"/v1/public/salons/{slug}/hours")
        assert anonymous.status_code == 200 and anonymous.json()["exceptions"] == []

        published = client.post(f"{base}/locations/{loc}/exceptions/{exc['id']}/publish", json={"published": True})
        assert published.status_code == 200 and published.json()["published_at"]

        public = client.get(f"{base}/locations/{loc}/public-hours").json()
        assert len(public["exceptions"]) == 1
        anonymous = client.get(f"/v1/public/salons/{slug}/hours")
        assert anonymous.status_code == 200 and len(anonymous.json()["exceptions"]) == 1
        assert "id" not in anonymous.json()["exceptions"][0]

        # Retracting removes it again.
        client.post(f"{base}/locations/{loc}/exceptions/{exc['id']}/publish", json={"published": False})
        assert client.get(f"{base}/locations/{loc}/public-hours").json()["exceptions"] == []
        assert client.get(f"/v1/public/salons/{slug}/hours").json()["exceptions"] == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_hours_are_tenant_scoped(database_url):
    """AC-2: another tenant cannot read or write this location's hours."""
    issuer = "https://identity.example.invalid/hours-6"
    other_issuer = "https://identity.example.invalid/hours-6b"
    engine, owner, tenant = _salon(database_url, issuer)
    other_engine, other_owner, other_tenant = _salon(database_url, other_issuer)
    client = _client(database_url, owner)
    other_client = _client(database_url, other_owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    other_base = f"/v1/tenants/{other_tenant['tenant_id']}"
    loc = tenant["location_id"]
    try:
        client.put(f"{base}/locations/{loc}/hours", json={"intervals": [
            {"weekday": 0, "opens_at": "09:00", "closes_at": "17:00"}]})
        assert other_client.get(f"{other_base}/locations/{loc}/hours").status_code == 404
        assert other_client.put(f"{other_base}/locations/{loc}/hours", json={"intervals": []}).status_code == 404
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], other_issuer)
