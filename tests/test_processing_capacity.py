"""TASK-067 integration checks through salon catalogue and booking HTTP routes."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier
from sqlalchemy import text
import pytest
from test_clients import _salon, _client, _teardown


@pytest.fixture
def database_url():
    value = os.getenv("DATABASE_URL")
    if not value:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return value


def test_service_phases_and_resource_pool_capacity_are_configurable(database_url):
    issuer = "https://identity.example.invalid/processing-" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        service = client.post(f"{base}/services", json={
            "name": "Colour with processing", "duration_minutes": 60,
            "price_minor": 9000}).json()["id"]
        pool = client.post(f"{base}/resources", json={
            "location_id": tenant["location_id"], "name": "Processing stations",
            "kind": "room", "capacity": 2})
        assert pool.status_code == 201, pool.text
        assert pool.json()["capacity"] == 2

        missing_version = client.put(f"{base}/services/{service}/resources", json={
            "location_id": tenant["location_id"], "resource_ids": [pool.json()["id"]]})
        assert missing_version.status_code == 422

        requirement = client.put(f"{base}/services/{service}/resources", json={
            "location_id": tenant["location_id"], "expected_version": 1,
            "resource_ids": [pool.json()["id"]],
            "quantities": {pool.json()["id"]: 2}})
        assert requirement.status_code == 200, requirement.text
        configured = client.get(f"{base}/services/{service}/resources",
                                params={"location_id": tenant["location_id"]}).json()
        assert configured["items"][0]["quantity"] == 2
        assert configured["version"] == requirement.json()["version"]
        stale = client.put(f"{base}/services/{service}/resources", json={
            "location_id": tenant["location_id"], "expected_version": 1,
            "resource_ids": [pool.json()["id"]], "quantities": {pool.json()["id"]: 2}})
        assert stale.status_code == 409
        reduced = client.patch(f"{base}/resources/{pool.json()['id']}", json={"capacity": 1})
        assert reduced.status_code == 409
        assert reduced.json()["detail"]["code"] == "capacity_below_configured_quantity"

        phases = [{"sequence": 1, "duration_minutes": 20,
                   "mode": "attended", "staff_count": 2},
                  {"sequence": 2, "duration_minutes": 40,
                   "mode": "gap", "staff_count": 0}]
        saved = client.put(f"{base}/services/{service}/phases", json={
            "expected_version": requirement.json()["version"], "phases": phases})
        assert saved.status_code == 200, saved.text
        expected = [{**phases[0], "start_offset_minutes": 0},
                    {**phases[1], "start_offset_minutes": 20}]
        assert saved.json()["phases"] == expected
        assert client.get(f"{base}/services/{service}/phases").json()["phases"] == expected
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_invalid_phase_and_pool_configuration_fails_closed(database_url):
    issuer = "https://identity.example.invalid/processing-invalid-" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        service = client.post(f"{base}/services", json={
            "name": "Phase validation", "duration_minutes": 60,
            "price_minor": 9000}).json()["id"]
        invalid = client.put(f"{base}/services/{service}/phases", json={
            "expected_version": 1,
            "phases": [{"sequence": 1, "duration_minutes": 25,
                        "mode": "attended", "staff_count": 1},
                       {"sequence": 2, "duration_minutes": 25,
                        "mode": "gap", "staff_count": 0}]})
        assert invalid.status_code == 422
        assert invalid.json()["detail"]["code"] == "phase_duration_mismatch"
        assert client.get(f"{base}/services/{service}/phases").json()["phases"] == []

        pool = client.post(f"{base}/resources", json={
            "location_id": tenant["location_id"], "name": "Small pool",
            "kind": "chair", "capacity": 1}).json()
        over = client.put(f"{base}/services/{service}/resources", json={
            "location_id": tenant["location_id"], "expected_version": 1,
            "resource_ids": [pool["id"]],
            "quantities": {pool["id"]: 2}})
        assert over.status_code == 422
        assert over.json()["detail"]["code"] == "resource_quantity_exceeds_capacity"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_pool_units_are_exclusive_and_allow_only_configured_capacity(database_url):
    issuer = "https://identity.example.invalid/pool-race-" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    location = tenant["location_id"]
    try:
        client.put(f"{base}/locations/{location}/hours", json={
            "intervals": [{"weekday": d, "opens_at": "09:00", "closes_at": "17:00"}
                          for d in range(7)]})
        service = client.post(f"{base}/services", json={
            "name": "Shared processing station", "duration_minutes": 60,
            "price_minor": 5000}).json()["id"]
        pool = client.post(f"{base}/resources", json={
            "location_id": location, "name": "Two stations", "kind": "room", "capacity": 2}).json()
        assert client.put(f"{base}/services/{service}/resources", json={
            "location_id": location, "expected_version": 1,
            "resource_ids": [pool["id"]],
            "quantities": {pool["id"]: 1}}).status_code == 200
        staff_ids = []
        for n in range(3):
            staff = client.post(f"{base}/staff", json={"display_name": f"Pool Stylist {n}"}).json()["id"]
            assert client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [location]}).status_code == 200
            assert client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]}).status_code == 200
            assert client.post(f"{base}/staff/{staff}/shifts", json={
                "location_id": location, "weekday": (date.today() + timedelta(days=1)).weekday(),
                "starts_at": "09:00", "ends_at": "17:00"}).status_code in (200, 201)
            staff_ids.append(staff)
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id',:t,true)"),
                               {"t": tenant["tenant_id"]})
            slug = connection.execute(text(
                "SELECT booking_slug FROM locations WHERE tenant_id=:t AND id=:l"),
                {"t": tenant["tenant_id"], "l": location}).scalar_one()
        day = date.today() + timedelta(days=1)
        slot = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": day.isoformat(), "date_to": day.isoformat()
        }).json()["slots"][0]["starts_at"]
        for staff in staff_ids[:2]:
            hold = client.post(f"/v1/public/salons/{slug}/holds", json={
                "service_id": service, "staff_id": staff, "starts_at": slot,
                "idempotency_key": "pool-" + uuid.uuid4().hex})
            assert hold.status_code == 201, hold.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id',:t,true)"),
                               {"t": tenant["tenant_id"]})
            units = connection.execute(text("""SELECT resource_unit FROM reservations
                WHERE tenant_id=:t AND resource_id=:r AND state='active' ORDER BY resource_unit"""),
                {"t": tenant["tenant_id"], "r": pool["id"]}).scalars().all()
        assert units == [1, 2]
        exhausted = client.post(f"/v1/public/salons/{slug}/holds", json={
            "service_id": service, "staff_id": staff_ids[2], "starts_at": slot,
            "idempotency_key": "pool-" + uuid.uuid4().hex})
        assert exhausted.status_code == 409
        assert exhausted.json()["detail"]["code"] == "slot_unavailable"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_capacity_reduction_serializes_with_requirement_increase(database_url):
    issuer = "https://identity.example.invalid/pool-config-race-" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        service = client.post(f"{base}/services", json={
            "name": "Capacity race", "duration_minutes": 30, "price_minor": 5000}).json()["id"]
        pool = client.post(f"{base}/resources", json={
            "location_id": tenant["location_id"], "name": "Race pool",
            "kind": "room", "capacity": 2}).json()
        barrier = Barrier(2)

        def configure_requirement():
            barrier.wait()
            return client.put(f"{base}/services/{service}/resources", json={
                "location_id": tenant["location_id"], "expected_version": 1,
                "resource_ids": [pool["id"]], "quantities": {pool["id"]: 2}})

        def reduce_capacity():
            barrier.wait()
            return client.patch(f"{base}/resources/{pool['id']}", json={"capacity": 1})

        with ThreadPoolExecutor(max_workers=2) as executor:
            requirement_future = executor.submit(configure_requirement)
            capacity_future = executor.submit(reduce_capacity)
            requirement, capacity = requirement_future.result(), capacity_future.result()
        assert requirement.status_code == 200 or requirement.status_code == 422
        assert capacity.status_code == 200 or capacity.status_code == 409
        configured = client.get(f"{base}/services/{service}/resources",
                                 params={"location_id": tenant["location_id"]}).json()
        capacity_now = client.get(f"{base}/resources", params={"location_id": tenant["location_id"]}).json()
        current_pool = next(item for item in capacity_now["items"] if item["id"] == pool["id"])
        assert all(item["quantity"] <= current_pool["capacity"] for item in configured["items"])
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_hold_reserves_and_confirms_attended_phase_assignments(database_url):
    issuer = "https://identity.example.invalid/phase-booking-" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    location = tenant["location_id"]
    try:
        client.put(f"{base}/locations/{location}/hours", json={
            "intervals": [{"weekday": d, "opens_at": "09:00", "closes_at": "17:00"}
                          for d in range(7)]})
        service = client.post(f"{base}/services", json={
            "name": "Colour and process", "duration_minutes": 60,
            "price_minor": 8000}).json()["id"]
        staff_ids = []
        for n in range(2):
            staff = client.post(f"{base}/staff", json={"display_name": f"Phase Stylist {n}"}).json()["id"]
            client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [location]})
            client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
            client.post(f"{base}/staff/{staff}/shifts", json={
                "location_id": location, "weekday": (date.today() + timedelta(days=1)).weekday(),
                "starts_at": "09:00", "ends_at": "09:20"})
            staff_ids.append(staff)
        phases = [{"sequence": 1, "duration_minutes": 20, "mode": "attended", "staff_count": 2},
                  {"sequence": 2, "duration_minutes": 40, "mode": "gap", "staff_count": 0}]
        saved = client.put(f"{base}/services/{service}/phases", json={
            "expected_version": 1, "phases": phases})
        assert saved.status_code == 200, saved.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id',:t,true)"),
                               {"t": tenant["tenant_id"]})
            slug = connection.execute(text(
                "SELECT booking_slug FROM locations WHERE tenant_id=:t AND id=:l"),
                {"t": tenant["tenant_id"], "l": location}).scalar_one()
        day = date.today() + timedelta(days=1)
        slot = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": day.isoformat(), "date_to": day.isoformat()
        }).json()["slots"][0]["starts_at"]
        wait_client = client.post(f"{base}/clients", json={"name": "Synthetic Waitlist Client"}).json()["id"]
        entry = client.post(f"{base}/waitlist", json={"client_id": wait_client, "service_id": service,
            "location_id": location, "staff_id": staff_ids[0], "date_from": day.isoformat(),
            "date_to": day.isoformat()})
        assert entry.status_code == 201, entry.text
        offer = client.post(f"{base}/waitlist/{entry.json()['entry_id']}/offers", json={
            "starts_at": slot, "idempotency_key": "phase-offer-" + uuid.uuid4().hex})
        assert offer.status_code == 201, offer.text
        hold = client.post(f"/v1/public/salons/{slug}/holds", json={
            "service_id": service, "staff_id": staff_ids[0], "starts_at": slot,
            "idempotency_key": "phase-" + uuid.uuid4().hex})
        assert hold.status_code == 201, hold.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id',:t,true)"),
                               {"t": tenant["tenant_id"]})
            reserved = connection.execute(text("""SELECT staff_id::text,phase_sequence,
                    lower(occupied_range),upper(occupied_range) FROM reservations
                    WHERE tenant_id=:t AND hold_id=:hold AND staff_id IS NOT NULL ORDER BY staff_id"""),
                {"t": tenant["tenant_id"], "hold": hold.json()["hold_id"]}).all()
        assert len(reserved) == 2 and {row.phase_sequence for row in reserved} == {1}
        assert {row.staff_id for row in reserved} == set(staff_ids)
        terms = client.get(f"/v1/public/salons/{slug}").json()["booking_policy"]
        confirmed = client.post(f"/v1/public/salons/{slug}/holds/{hold.json()['hold_id']}/confirm", json={
            "owner_token": hold.json()["owner_token"], "client_name": "Synthetic Phase Client",
            "email": "phase@example.invalid", "country": "CA", "policy_accepted": True,
            "policy_version": terms["version"], "idempotency_key": "phase-confirm-" + uuid.uuid4().hex})
        assert confirmed.status_code == 200, confirmed.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id',:t,true)"),
                               {"t": tenant["tenant_id"]})
            snapshot = connection.execute(text("""SELECT processing_phases FROM appointment_services
                WHERE tenant_id=:t AND appointment_id=:a"""),
                {"t": tenant["tenant_id"], "a": confirmed.json()["appointment_id"]}).scalar_one()
            assigned = connection.execute(text("""SELECT count(*) FROM reservations
                WHERE tenant_id=:t AND appointment_id=:a AND staff_id IS NOT NULL AND state='active'"""),
                {"t": tenant["tenant_id"], "a": confirmed.json()["appointment_id"]}).scalar_one()
        assert snapshot[0]["mode"] == "attended" and snapshot[1]["mode"] == "gap"
        assert len(snapshot[0]["staff_ids"]) == 2 and snapshot[1]["staff_ids"] == []
        assert assigned == 2

        manual_client = client.post(f"{base}/clients", json={"name": "Synthetic Manual Client"}).json()["id"]
        manual = client.post(f"{base}/appointments", json={"location_id": location,
            "client_id": manual_client, "staff_id": staff_ids[0], "service_id": service,
            "starts_at": slot, "source": "staff", "idempotency_key": "phase-manual-" + uuid.uuid4().hex})
        assert manual.status_code == 409 and manual.json()["detail"]["code"] == "slot_unavailable"
        claim = client.post(f"/v1/public/salons/{slug}/offers/{offer.json()['offer_id']}/claim", json={
            "owner_token": offer.json()["owner_token"], "idempotency_key": "phase-claim-" + uuid.uuid4().hex})
        assert claim.status_code == 409 and claim.json()["detail"]["code"] == "slot_unavailable"
        recurring = client.post(f"{base}/recurring-series/preview", json={
            "client_id": wait_client, "staff_id": staff_ids[0], "service_id": service,
            "location_id": location, "frequency": "weekly", "interval_weeks": 1,
            "local_time": slot[11:16], "start_date": day.isoformat(), "end_rule": "count",
            "occurrence_count": 1, "idempotency_key": "phase-series-" + uuid.uuid4().hex})
        assert recurring.status_code == 200, recurring.text
        assert recurring.json()["occurrences"][0]["valid"] is False
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)

