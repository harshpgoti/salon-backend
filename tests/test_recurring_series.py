"""Tests for explicit recurring appointment series (FR-037, TASK-064).

Builds on the recurring series API and the TASK-020 reservation engine.
Each test creates its own tenant (unique issuer) and tears down after.

Run ONLY your file:
  cd H:\salon\backend
  $env:DATABASE_URL='postgresql+psycopg://salon_app:local-dev-only@127.0.0.1:5433/salon'; .\.venv\Scripts\python.exe -m pytest -q tests/test_recurring_series.py -p no:cacheprovider
"""

import os
import uuid
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from test_clients import _salon, _client, _teardown


def _recurring_series_create(client, tenant_id, slug, service, staff, client_id,
                              start_date, frequency, local_time, end_rule,
                              location_id, occurrence_count=None, until_date=None,
                              idempotency_key=None, approved_indices=None):
    """Helper: call the preview endpoint then the create endpoint."""
    if idempotency_key is None:
        idempotency_key = f"rec-ser-{uuid.uuid4().hex}"
    # Run preview
    resp = client.post(f"/v1/tenants/{tenant_id}/recurring-series/preview", json={
        "client_id": str(client_id),
        "staff_id": str(staff),
        "service_id": service,
        "location_id": location_id,
        "frequency": frequency,
        "interval_weeks": 1,
        "local_time": local_time,
        "start_date": start_date.isoformat(),
        "end_rule": end_rule,
        "occurrence_count": occurrence_count,
        "until_date": until_date,
        "idempotency_key": idempotency_key,
    })
    assert resp.status_code == 200, resp.text
    preview = resp.json()

    # Create series with approved indices (all marked valid in our preview)
    create_request = {
        "client_id": str(client_id),
        "staff_id": str(staff),
        "service_id": service,
        "location_id": location_id,
        "frequency": frequency,
        "interval_weeks": 1,
        "local_time": local_time,
        "start_date": start_date.isoformat(),
        "end_rule": end_rule,
        "occurrence_count": occurrence_count,
        "until_date": until_date,
        "approved_indices": approved_indices if approved_indices is not None else
            [item["index"] for item in preview["occurrences"] if item["valid"]],
        "idempotency_key": idempotency_key,
    }
    create_resp = client.post(f"/v1/tenants/{tenant_id}/recurring-series", json=create_request)
    assert create_resp.status_code == 201, create_resp.text
    result = create_resp.json()
    result["preview"] = preview
    result["request"] = create_request
    return result


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def test_ac1_confirmed_occurrences_only(database_url):
    """AC-1: Conflict preview creates only approved individually valid occurrences."""
    issuer = "https://identity.example.invalid/rec-001"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base = f"/v1/tenants/{tenant['tenant_id']}"
        # Create a location and set hours (needed for availability)
        client.put(f"{base}/locations/{tenant['location_id']}/hours", json={
            "intervals": [{"weekday": d, "opens_at": "09:00", "closes_at": "17:00"} for d in range(6)]})
        service = client.post(f"{base}/services",
                              json={"name": "Cut", "duration_minutes": 60, "price_minor": 6000}).json()["id"]
        staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
        client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [tenant["location_id"]]})
        client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
        client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": tenant["location_id"], "weekday": 0, "starts_at": "09:00", "ends_at": "17:00"})
        client_row = client.post(f"{base}/clients", json={"name": "Booker"}).json()["id"]

        # Book a manual appointment that will occupy one of the 4 proposed slots,
        # so the preview will mark that occurrence invalid.
        day = date.today() + timedelta(days=(7 - date.today().weekday()))
        preview_payload = {"client_id": client_row, "staff_id": staff, "service_id": service,
                           "location_id": tenant["location_id"], "frequency": "weekly",
                           "local_time": "14:30", "end_rule": "count", "occurrence_count": 1,
                           "idempotency_key": f"rec-range-{uuid.uuid4().hex}"}
        past = client.post(f"{base}/recurring-series/preview", json={
            **preview_payload, "start_date": (day - timedelta(days=14)).isoformat()})
        assert past.status_code == 422
        assert past.json()["detail"]["code"] == "series_start_out_of_range"
        too_wide = client.post(f"{base}/recurring-series/preview", json={
            **preview_payload, "interval_weeks": 12, "occurrence_count": 6,
            "start_date": day.isoformat()})
        assert too_wide.status_code == 422
        assert too_wide.json()["detail"]["code"] == "series_horizon_too_wide"
        manual = client.post(f"{base}/appointments", json={
            "location_id": tenant["location_id"], "client_id": client_row,
            "staff_id": staff, "service_id": service,
            "starts_at": datetime.combine(day, time(14, 30), ZoneInfo("America/Toronto")).isoformat(),
            "source": "staff", "idempotency_key": f"rec-manual-{uuid.uuid4().hex}"})
        assert manual.status_code == 201, manual.text
        create = _recurring_series_create(client, tenant["tenant_id"], None, service, staff,
                                          client_row, day, "weekly", "14:30", "count",
                                          tenant["location_id"],
                                          occurrence_count=4, approved_indices=[1, 2, 3])
        assert create["preview"]["occurrences"][0]["reason"] == "slot_unavailable"
        assert [item["index"] for item in create["preview"]["occurrences"] if item["valid"]] == [1, 2, 3]
        # Verify 4 appointments were created
        appt_resp = client.get(f"{base}/appointments", params={"limit": 100})
        assert appt_resp.status_code == 200, appt_resp.text
        created_rows = {a["id"]: a for a in appt_resp.json()["items"]}
        created_ids = set(created_rows)
        assert len(created_ids) == 4, f"Expected one manual plus 3 approved appointments, got {len(created_ids)}"
        assert all(created_rows[item["appointment_id"]]["status"] == "confirmed"
                   for item in create["created_appointments"])
        foreign_engine, foreign_owner, foreign_tenant = _salon(
            database_url, "https://identity.example.invalid/rec-foreign")
        try:
            foreign_client = _client(database_url, foreign_owner)
            foreign_cancel = foreign_client.post(
                f"/v1/tenants/{foreign_tenant['tenant_id']}/recurring-series/{create['series_id']}/cancel",
                json={"mode": "series", "expected_version": create["version"]})
            assert foreign_cancel.status_code == 404
            assert foreign_cancel.json()["detail"]["code"] == "series_not_found"
        finally:
            _teardown(foreign_engine, [foreign_tenant["tenant_id"]], "https://identity.example.invalid/rec-foreign")
        replay = client.post(f"{base}/recurring-series", json=create["request"])
        assert replay.status_code == 201 and replay.json()["replayed"] is True
        assert replay.json()["created_appointments"] == create["created_appointments"]
        changed = {**create["request"], "approved_indices": [2, 3]}
        conflict = client.post(f"{base}/recurring-series", json=changed)
        assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "idempotency_conflict"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_ac2_cancel_preserves_history(database_url):
    """AC-2: Range/single edits cancellations and DST exceptions preserve history."""
    issuer = "https://identity.example.invalid/rec-002"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base = f"/v1/tenants/{tenant['tenant_id']}"
        client.put(f"{base}/locations/{tenant['location_id']}/hours", json={
            "intervals": [{"weekday": d, "opens_at": "09:00", "closes_at": "17:00"} for d in range(6)]})
        service = client.post(f"{base}/services",
                              json={"name": "Cut", "duration_minutes": 60, "price_minor": 6000}).json()["id"]
        staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
        client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [tenant["location_id"]]})
        client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
        client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": tenant["location_id"], "weekday": 0, "starts_at": "09:00", "ends_at": "17:00"})
        client_row = client.post(f"{base}/clients", json={"name": "Booker"}).json()["id"]

        # Create a 3-occurrence series
        create = _recurring_series_create(client, tenant["tenant_id"], None, service, staff,
                                          client_row, date.today() + timedelta(days=(7-date.today().weekday())), "weekly", "14:30", "count",
                                          tenant["location_id"],
                                          occurrence_count=3)
        # All 3 appointments should exist
        appt_resp = client.get(f"{base}/appointments", params={"limit": 100})
        created_ids = {a["id"] for a in appt_resp.json()["items"]}
        assert len(created_ids) == 3, f"Expected 3 appointments, got {len(created_ids)}"

        edit_resp = client.patch(f"{base}/recurring-series/{create['series_id']}", json={
            "mode": "occurrence", "occurrence_index": 0,
            "expected_version": create["version"], "local_time": "15:00"})
        assert edit_resp.status_code == 200, edit_resp.text
        assert edit_resp.json()["updated_indices"] == [0]

        range_edit = client.patch(f"{base}/recurring-series/{create['series_id']}", json={
            "mode": "from", "occurrence_index": 1,
            "expected_version": edit_resp.json()["version"], "local_time": "16:00"})
        assert range_edit.status_code == 200, range_edit.text
        assert range_edit.json()["updated_indices"] == [1, 2]
        stale = client.patch(f"{base}/recurring-series/{create['series_id']}", json={
            "mode": "from", "occurrence_index": 1,
            "expected_version": create["version"], "local_time": "16:30"})
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "series_version_conflict"

        # Cancel a different occurrence while retaining edit/cancellation history.
        cancel_resp = client.post(f"{base}/recurring-series/{create.get('series_id', 'todo')}/cancel", json={
            "mode": "occurrence",
            "occurrence_index": 2,
            "expected_version": range_edit.json()["version"],
            "reason": "test cancel"
        })
        assert cancel_resp.status_code == 200, cancel_resp.text

        # The other two should still be confirmed
        appt_resp2 = client.get(f"{base}/appointments", params={"limit": 100})
        created_ids2 = {a["id"] for a in appt_resp2.json()["items"]}
        # After cancel, we expect 2 remaining confirmed + 1 cancelled
        assert len(created_ids2) == 3, f"Expected 3 total (1 cancelled + 2 active), got {len(created_ids2)}"
        rows = {a["id"]: a for a in appt_resp2.json()["items"]}
        by_occurrence = {item["appointment_id"]: rows[item["appointment_id"]]
                         for item in create["created_appointments"]}
        assert by_occurrence[create["created_appointments"][0]["appointment_id"]]["starts_at"].endswith("19:00:00+00:00")
        assert by_occurrence[create["created_appointments"][1]["appointment_id"]]["starts_at"].endswith("20:00:00+00:00")
        assert by_occurrence[create["created_appointments"][2]["appointment_id"]]["status"] == "cancelled"
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id',:tenant,true)"), {"tenant": tenant["tenant_id"]})
            changes = connection.execute(text("SELECT count(*) FROM recurring_series_changes WHERE tenant_id=:tenant AND series_id=:series"),
                                         {"tenant": tenant["tenant_id"], "series": create["series_id"]}).scalar_one()
            assert changes == 3
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_dst_wall_times_are_never_silently_shifted():
    from modules.booking.recurring import _local_start
    zone = ZoneInfo("America/Toronto")
    gap, gap_reason = _local_start(date(2027, 3, 14), "02:30", zone)
    duplicate, duplicate_reason = _local_start(date(2027, 11, 7), "01:30", zone)
    valid, valid_reason = _local_start(date(2027, 3, 14), "03:30", zone)
    assert (gap, gap_reason) == (None, "dst_gap")
    assert (duplicate, duplicate_reason) == (None, "dst_ambiguous")
    assert valid is not None and valid_reason is None


def test_recurring_slots_respect_published_location_hours(database_url):
    """Recurring preview, commit, and edit must fit the salon's effective hours."""
    issuer = "https://identity.example.invalid/rec-hours"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base = f"/v1/tenants/{tenant['tenant_id']}"
        client.put(f"{base}/locations/{tenant['location_id']}/hours", json={
            "intervals": [{"weekday": 0, "opens_at": "10:00", "closes_at": "15:00"}]})
        service = client.post(f"{base}/services", json={
            "name": "Cut", "duration_minutes": 60, "price_minor": 6000}).json()["id"]
        staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
        client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [tenant["location_id"]]})
        client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
        client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": tenant["location_id"], "weekday": 0,
            "starts_at": "09:00", "ends_at": "17:00"})
        client_id = client.post(f"{base}/clients", json={"name": "Booker"}).json()["id"]
        monday = date.today() + timedelta(days=(7 - date.today().weekday()))
        common = {"client_id": client_id, "staff_id": staff, "service_id": service,
                  "location_id": tenant["location_id"], "frequency": "weekly",
                  "interval_weeks": 1, "start_date": monday.isoformat(),
                  "end_rule": "count", "occurrence_count": 1}

        closed_slot = {**common, "local_time": "16:00",
                       "idempotency_key": f"rec-closed-{uuid.uuid4().hex}"}
        preview = client.post(f"{base}/recurring-series/preview", json=closed_slot)
        assert preview.status_code == 200, preview.text
        assert preview.json()["occurrences"][0]["valid"] is False
        assert preview.json()["occurrences"][0]["reason"] == "slot_unavailable"
        rejected = client.post(f"{base}/recurring-series", json={
            **closed_slot, "approved_indices": [0]})
        assert rejected.status_code == 409, rejected.text

        valid_slot = {**common, "local_time": "14:00",
                      "idempotency_key": f"rec-open-{uuid.uuid4().hex}"}
        valid_preview = client.post(f"{base}/recurring-series/preview", json=valid_slot)
        assert valid_preview.status_code == 200 and valid_preview.json()["occurrences"][0]["valid"]
        created = client.post(f"{base}/recurring-series", json={
            **valid_slot, "approved_indices": [0]})
        assert created.status_code == 201, created.text
        edited = client.patch(f"{base}/recurring-series/{created.json()['series_id']}", json={
            "mode": "occurrence", "occurrence_index": 0,
            "expected_version": created.json()["version"], "local_time": "16:00"})
        assert edited.status_code == 409, edited.text
        appointment = client.get(f"{base}/appointments", params={"limit": 10}).json()["items"][0]
        assert appointment["starts_at"].endswith("18:00:00+00:00")
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
