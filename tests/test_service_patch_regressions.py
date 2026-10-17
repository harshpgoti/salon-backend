"""Required service patch validation and frozen booking history (TASK-073)."""

import os
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from test_booking_holds import _bookable
from test_clients import _client, _salon, _teardown
from test_service_catalogue import _service


@pytest.fixture
def database_url():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required for real PostgreSQL service regressions")
    return url


@pytest.mark.parametrize("field", [
    "name", "duration_minutes", "pre_buffer_minutes",
    "post_buffer_minutes", "price_minor",
])
def test_required_service_patch_null_is_422_and_preserves_values(database_url, field):
    issuer = "https://identity.example.invalid/service-null/" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    client = TestClient(_client(database_url, owner).app, raise_server_exceptions=False)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        created = _service(client, base, pre_buffer_minutes=10, post_buffer_minutes=15)
        assert created.status_code == 201, created.text
        original = created.json()
        rejected = client.patch(f"{base}/services/{original['id']}", json={
            field: None, "expected_version": original["version"],
        })
        assert rejected.status_code == 422, rejected.text
        assert any(error["loc"][-1] == field for error in rejected.json()["detail"])
        persisted = client.get(f"{base}/services").json()["items"][0]
        for key in ["name", "duration_minutes", "pre_buffer_minutes",
                    "post_buffer_minutes", "price_minor", "version"]:
            assert persisted[key] == original[key]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_nullable_service_fields_clear_with_versioned_partial_update(database_url):
    issuer = "https://identity.example.invalid/service-clear/" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        category = client.post(f"{base}/service-categories", json={"name": "Colour"})
        assert category.status_code == 201, category.text
        original = _service(client, base, description="Description",
                            category_id=category.json()["id"],
                            rebooking_interval_days=42).json()
        path = f"{base}/services/{original['id']}"
        changed = client.patch(path, json={
            "description": None, "category_id": None, "rebooking_interval_days": None,
            "price_minor": 7000, "expected_version": original["version"],
        })
        assert changed.status_code == 200, changed.text
        body = changed.json()
        assert body["version"] == original["version"] + 1
        assert body["price_minor"] == 7000
        assert body["description"] is None and body["category_id"] is None
        assert body["rebooking_interval_days"] is None
        for field in ["name", "duration_minutes", "pre_buffer_minutes", "post_buffer_minutes"]:
            assert body[field] == original[field]
        stale = client.patch(path, json={"name": "Stale", "expected_version": original["version"]})
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"]["code"] == "service_version_conflict"
        persisted = client.get(f"{base}/services").json()["items"][0]
        assert persisted["name"] == original["name"] and persisted["version"] == body["version"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_booked_snapshot_survives_service_edit_archive_and_restore(database_url):
    issuer = "https://identity.example.invalid/service-history/" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, _, service, staff, client_id = _bookable(client, database_url, tenant)
        service_path = f"{base}/services/{service}"
        prepared = client.patch(service_path, json={
            "name": "Cut and Finish", "duration_minutes": 45, "price_minor": 6500,
            "pre_buffer_minutes": 10, "post_buffer_minutes": 15, "expected_version": 1,
        })
        assert prepared.status_code == 200, prepared.text
        original = prepared.json()
        day = date.today() + timedelta(days=1)
        while day.weekday() == 6:
            day += timedelta(days=1)
        booked = client.post(f"{base}/appointments", json={
            "client_id": client_id, "staff_id": staff, "service_id": service,
            "starts_at": f"{day.isoformat()}T14:00:00+00:00",
            "idempotency_key": "history-" + uuid.uuid4().hex,
        })
        assert booked.status_code == 201, booked.text
        appointment = booked.json()["appointment_id"]
        edited = client.patch(service_path, json={
            "name": "New Catalogue Name", "duration_minutes": 90, "price_minor": 7000,
            "pre_buffer_minutes": 0, "post_buffer_minutes": 0,
            "expected_version": original["version"],
        })
        assert edited.status_code == 200, edited.text
        assert client.post(service_path + "/archive").status_code == 200
        assert client.get(f"{base}/services").json()["items"] == []
        for restored in [False, True]:
            if restored:
                assert client.post(service_path + "/restore").status_code == 200
            listing = client.get(f"{base}/appointments")
            assert listing.status_code == 200, listing.text
            row = next(item for item in listing.json()["items"] if item["id"] == appointment)
            assert row["service_name"] == "Cut and Finish"
            assert row["price_minor"] == 6500 and row["currency"] == "CAD"
            assert row["service_version"] == original["version"]
            for field in ["duration_minutes", "pre_buffer_minutes", "post_buffer_minutes"]:
                assert row[field] == original[field]
            with engine.begin() as connection:
                connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                                   {"t": tenant["tenant_id"]})
                snapshot = connection.execute(text(
                    "SELECT service_name, price_minor, currency, service_version, "
                    "duration_minutes, pre_buffer_minutes, post_buffer_minutes "
                    "FROM appointment_services WHERE tenant_id=:t AND appointment_id=:a"
                ), {"t": tenant["tenant_id"], "a": appointment}).mappings().one()
                assert dict(snapshot) == {
                    "service_name": original["name"], "price_minor": original["price_minor"],
                    "currency": "CAD", "service_version": original["version"],
                    "duration_minutes": original["duration_minutes"],
                    "pre_buffer_minutes": original["pre_buffer_minutes"],
                    "post_buffer_minutes": original["post_buffer_minutes"],
                }
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
