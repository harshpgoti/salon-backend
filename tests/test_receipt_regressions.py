"""Real PostgreSQL checkout replay and signed-money regressions (TASK-075)."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from test_clients import _client, _salon, _teardown
from test_public_availability import _bookable_salon


@pytest.fixture
def checkout():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required for checkout regressions")
    issuer = "https://identity.example.invalid/checkout/" + uuid.uuid4().hex
    engine, owner, tenant = _salon(url, issuer)
    client = TestClient(_client(url, owner).app, raise_server_exceptions=False)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        day = date.today() + timedelta(days=1)
        _, service, staff = _bookable_salon(
            client, url, tenant, weekdays=range(7), shift_day=day.weekday())
        person = client.post(f"{base}/clients", json={"name": "Synthetic Checkout"}).json()["id"]
        appointments = []
        for hour in (10, 12):
            starts = datetime.combine(day, time(hour), ZoneInfo("America/Toronto")).isoformat()
            booked = client.post(f"{base}/appointments", json={
                "location_id": tenant["location_id"], "service_id": service, "staff_id": staff,
                "client_id": person, "starts_at": starts, "idempotency_key": uuid.uuid4().hex,
            })
            assert booked.status_code == 201, booked.text
            appointments.append(booked.json()["appointment_id"])
        yield client, base, appointments, engine, tenant
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def _payment(**changes):
    return {"kind": "payment", "amount_minor": 2500, "method": "cash",
            "idempotency_key": uuid.uuid4().hex, **changes}


def _entries(base, appointment):
    return f"{base}/appointments/{appointment}/receipt-entries"


def test_identical_receipt_replay_returns_saved_id_once(checkout):
    client, base, appointments, engine, tenant = checkout
    path = _entries(base, appointments[0])
    payload = _payment(external_reference="synthetic-ref", note="Synthetic entry")
    first = client.post(path, json=payload)
    replay = client.post(path, json=payload)
    assert first.status_code == replay.status_code == 201, (first.text, replay.text)
    assert replay.json() == {"entry_id": first.json()["entry_id"], "replayed": True}
    assert len(client.get(path).json()["items"]) == 1
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
        count = connection.execute(text(
            "SELECT count(*) FROM tenant_audit_events WHERE tenant_id=:t "
            "AND action='receipt.recorded' AND resource_id=:id"),
            {"t": tenant["tenant_id"], "id": first.json()["entry_id"]}).scalar_one()
    assert count == 1


@pytest.mark.parametrize("field,value", [
    ("amount_minor", 3500), ("kind", "adjustment"), ("method", "card"),
    ("external_reference", "different"), ("note", "Changed"),
])
def test_changed_receipt_fact_with_same_key_is_conflict(checkout, field, value):
    client, base, appointments, _, _ = checkout
    path = _entries(base, appointments[0])
    payload = _payment()
    saved = client.post(path, json=payload)
    assert saved.status_code == 201, saved.text
    changed = client.post(path, json={**payload, field: value})
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["code"] == "idempotency_conflict"
    items = client.get(path).json()["items"]
    assert len(items) == 1 and items[0]["amount_minor"] == 2500


def test_same_key_for_other_appointment_is_conflict(checkout):
    client, base, appointments, _, _ = checkout
    payload = _payment()
    saved = client.post(_entries(base, appointments[0]), json=payload)
    assert saved.status_code == 201, saved.text
    wrong = client.post(_entries(base, appointments[1]), json=payload)
    assert wrong.status_code == 409, wrong.text
    assert client.get(_entries(base, appointments[1])).json()["items"] == []


def test_concurrent_receipt_retries_commit_one_entry(checkout):
    client, base, appointments, _, _ = checkout
    path, payload = _entries(base, appointments[0]), _payment()
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post(path, json=payload), range(4)))
    assert all(r.status_code == 201 for r in responses), [r.text for r in responses]
    assert len({r.json()["entry_id"] for r in responses}) == 1
    assert sum(not r.json()["replayed"] for r in responses) == 1
    assert len(client.get(path).json()["items"]) == 1


@pytest.mark.parametrize("kind,amount", [
    ("refund", 100), ("refund", 0), ("adjustment", 0), ("payment", -100),
    ("payment", 1.5), ("payment", True), ("payment", "100"), ("payment", 2147483648),
    ("adjustment", -2147483649),
])
def test_invalid_minor_units_never_create_facts(checkout, kind, amount):
    client, base, appointments, _, _ = checkout
    path = _entries(base, appointments[0])
    rejected = client.post(path, json=_payment(kind=kind, amount_minor=amount))
    assert rejected.status_code == 422, rejected.text
    assert client.get(path).json()["items"] == []


def test_refund_and_adjustment_have_signed_recorded_total(checkout):
    client, base, appointments, _, _ = checkout
    path = _entries(base, appointments[0])
    for kind, amount in [("payment", 2500), ("refund", -500), ("adjustment", -100)]:
        posted = client.post(path, json=_payment(kind=kind, amount_minor=amount))
        assert posted.status_code == 201, posted.text
    total = client.get(f"{base}/appointments/{appointments[0]}/receipt-total")
    assert total.status_code == 200, total.text
    assert total.json() == {"appointment_id": appointments[0], "recorded_total_minor": 1900,
                           "currency": "CAD", "status": "recorded"}
    assert {r["currency"] for r in client.get(path).json()["items"]} == {"CAD"}


def test_other_tenant_cannot_read_or_write_appointment_receipts(checkout):
    client, _, appointments, _, _ = checkout
    foreign = f"/v1/tenants/{uuid.uuid4()}/appointments/{appointments[0]}/receipt-entries"
    assert client.get(foreign).status_code in (403, 404)
    assert client.post(foreign, json=_payment()).status_code in (403, 404)
