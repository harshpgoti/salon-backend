"""Tests for weekly hours and dated exceptions (TASK-017, FR-022/FR-024)."""

import os

import pytest
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
        client.put(f"{base}/locations/{loc}/hours", json={"intervals": [
            {"weekday": 0, "opens_at": "09:00", "closes_at": "17:00"}]})
        exc = client.post(f"{base}/locations/{loc}/exceptions",
                          json={"exception_date": "2026-11-11", "kind": "closed", "label": "Remembrance Day"}).json()

        public = client.get(f"{base}/locations/{loc}/public-hours").json()
        assert public["weekly"] and public["exceptions"] == []

        published = client.post(f"{base}/locations/{loc}/exceptions/{exc['id']}/publish", json={"published": True})
        assert published.status_code == 200 and published.json()["published_at"]

        public = client.get(f"{base}/locations/{loc}/public-hours").json()
        assert len(public["exceptions"]) == 1

        # Retracting removes it again.
        client.post(f"{base}/locations/{loc}/exceptions/{exc['id']}/publish", json={"published": False})
        assert client.get(f"{base}/locations/{loc}/public-hours").json()["exceptions"] == []
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