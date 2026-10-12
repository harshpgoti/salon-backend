"""Tests for race-safe holds and booking commits (TASK-020, FR-029)."""

import os
import threading
import uuid
from datetime import date, timedelta
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


def _bookable(client, database_url, tenant):
    base = f"/v1/tenants/{tenant['tenant_id']}"
    loc = tenant["location_id"]
    slug = _slug(database_url, tenant["tenant_id"], loc)
    client.put(f"{base}/locations/{loc}/hours", json={
        "intervals": [{"weekday": d, "opens_at": "09:00", "closes_at": "17:00"} for d in range(6)]})
    service = client.post(f"{base}/services",
                          json={"name": "Cut", "duration_minutes": 60, "price_minor": 6000}).json()["id"]
    staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
    client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [loc]})
    client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
    client.post(f"{base}/staff/{staff}/shifts", json={
        "location_id": loc, "weekday": (date.today() + timedelta(days=1)).weekday(),
        "starts_at": "09:00", "ends_at": "17:00"})
    client_row = client.post(f"{base}/clients", json={"name": "Booker"}).json()["id"]
    return base, slug, service, staff, client_row


def _first_slot(client, slug, service, day):
    resp = client.get(f"/v1/public/salons/{slug}/availability", params={
        "service_id": service, "date_from": day.isoformat(), "date_to": day.isoformat()})
    assert resp.status_code == 200, resp.text
    slots = resp.json()["slots"]
    assert slots, "expected at least one slot"
    return slots[0]["starts_at"]


def test_hold_and_confirm_creates_an_appointment_with_a_snapshot(database_url):
    """AC-1: a hold converts to a confirmed appointment carrying its price/duration snapshot."""
    issuer = "https://identity.example.invalid/hold-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        start = _first_slot(client, slug, service, day)

        hold = client.post(f"/v1/public/salons/{slug}/holds",
                           json={"service_id": service, "staff_id": staff, "starts_at": start,
                                 "idempotency_key": "h-" + uuid.uuid4().hex})
        assert hold.status_code == 201, hold.text
        assert hold.json()["expires_in_seconds"] == 300

        confirmed = client.post(f"/v1/public/salons/{slug}/holds/{hold.json()['hold_id']}/confirm",
                                json={"owner_token": hold.json()["owner_token"], "client_id": client_id,
                                      "idempotency_key": "c-" + uuid.uuid4().hex})
        assert confirmed.status_code == 200, confirmed.text
        appointment_id = confirmed.json()["appointment_id"]

        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            snap = connection.execute(
                text("""SELECT service_name, price_minor, duration_minutes FROM appointment_services
                        WHERE tenant_id=:t AND appointment_id=:a"""),
                {"t": tenant["tenant_id"], "a": appointment_id}).mappings().one()
            assert snap["service_name"] == "Cut" and snap["price_minor"] == 6000
            assert snap["duration_minutes"] == 60
            transitions = connection.execute(
                text("SELECT count(*) FROM appointment_transitions WHERE tenant_id=:t AND appointment_id=:a"),
                {"t": tenant["tenant_id"], "a": appointment_id}).scalar_one()
            assert transitions == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_confirmed_slot_disappears_from_availability(database_url):
    """AC-1: a confirmation reserves the slot, so it is no longer offered."""
    issuer = "https://identity.example.invalid/hold-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        start = _first_slot(client, slug, service, day)
        before = len(client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": day.isoformat(), "date_to": day.isoformat()}).json()["slots"])

        hold = client.post(f"/v1/public/salons/{slug}/holds",
                           json={"service_id": service, "staff_id": staff, "starts_at": start,
                                 "idempotency_key": "h-" + uuid.uuid4().hex}).json()
        client.post(f"/v1/public/salons/{slug}/holds/{hold['hold_id']}/confirm",
                    json={"owner_token": hold["owner_token"], "client_id": client_id,
                          "idempotency_key": "c-" + uuid.uuid4().hex})

        after = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": day.isoformat(), "date_to": day.isoformat()}).json()["slots"]
        # The confirmed 60-minute service blocks its own range, so fewer slots are offered.
        assert len(after) < before
        assert all(s["starts_at"] != start for s in after)
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_second_hold_on_the_same_slot_is_refused(database_url):
    """AC-1: only one hold can own a staff member's range."""
    issuer = "https://identity.example.invalid/hold-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        start = _first_slot(client, slug, service, day)

        first = client.post(f"/v1/public/salons/{slug}/holds",
                            json={"service_id": service, "staff_id": staff, "starts_at": start,
                                  "idempotency_key": "h-" + uuid.uuid4().hex})
        assert first.status_code == 201
        second = client.post(f"/v1/public/salons/{slug}/holds",
                             json={"service_id": service, "staff_id": staff, "starts_at": start,
                                   "idempotency_key": "h-" + uuid.uuid4().hex})
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "slot_unavailable"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_concurrent_holds_have_exactly_one_winner(database_url):
    """AC-1 (QA-04): N concurrent attempts on one slot yield exactly one 201."""
    issuer = "https://identity.example.invalid/hold-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        start = _first_slot(client, slug, service, day)

        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(6)

        def attempt(index):
            local = _client(database_url, owner)
            barrier.wait()
            resp = local.post(f"/v1/public/salons/{slug}/holds",
                              json={"service_id": service, "staff_id": staff, "starts_at": start,
                                    "idempotency_key": f"race-{index}-{uuid.uuid4().hex}"})
            with lock:
                results.append(resp.status_code)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [code for code in results if code == 201]
        assert len(winners) == 1, f"expected exactly one winner, got {results}"
        # Every loser is told the slot is unavailable, never a 500.
        assert all(code in (201, 409) for code in results), results
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_wrong_token_cannot_read_or_confirm_a_hold(database_url):
    """AC-1: a hold is addressable only by its owner token."""
    issuer = "https://identity.example.invalid/hold-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        start = _first_slot(client, slug, service, day)
        hold = client.post(f"/v1/public/salons/{slug}/holds",
                           json={"service_id": service, "staff_id": staff, "starts_at": start,
                                 "idempotency_key": "h-" + uuid.uuid4().hex}).json()

        assert client.get(f"/v1/public/salons/{slug}/holds/{hold['hold_id']}",
                          params={"owner_token": "x" * 32}).status_code == 404
        assert client.post(f"/v1/public/salons/{slug}/holds/{hold['hold_id']}/confirm",
                           json={"owner_token": "y" * 32, "client_id": client_id,
                                 "idempotency_key": "c-" + uuid.uuid4().hex}).status_code == 404
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_confirm_replay_returns_the_same_appointment(database_url):
    """AC-1: confirming a converted hold again is a replay, not a second appointment."""
    issuer = "https://identity.example.invalid/hold-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        start = _first_slot(client, slug, service, day)
        hold = client.post(f"/v1/public/salons/{slug}/holds",
                           json={"service_id": service, "staff_id": staff, "starts_at": start,
                                 "idempotency_key": "h-" + uuid.uuid4().hex}).json()
        payload = {"owner_token": hold["owner_token"], "client_id": client_id,
                   "idempotency_key": "c-" + uuid.uuid4().hex}
        first = client.post(f"/v1/public/salons/{slug}/holds/{hold['hold_id']}/confirm", json=payload)
        second = client.post(f"/v1/public/salons/{slug}/holds/{hold['hold_id']}/confirm", json=payload)
        assert first.json()["appointment_id"] == second.json()["appointment_id"]
        assert second.json()["replayed"] is True
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            count = connection.execute(
                text("SELECT count(*) FROM appointments WHERE tenant_id=:t"),
                {"t": tenant["tenant_id"]}).scalar_one()
        assert count == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_replaying_a_converted_hold_key_returns_the_receipt(database_url):
    """AC-2: same key after confirm is a receipt lookup; the slot stays booked once."""
    issuer = "https://identity.example.invalid/hold-8"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        start = _first_slot(client, slug, service, day)
        key = "h-" + uuid.uuid4().hex
        hold = client.post(f"/v1/public/salons/{slug}/holds",
                           json={"service_id": service, "staff_id": staff, "starts_at": start,
                                 "idempotency_key": key}).json()
        confirmed = client.post(f"/v1/public/salons/{slug}/holds/{hold['hold_id']}/confirm",
                                json={"owner_token": hold["owner_token"], "client_id": client_id,
                                      "idempotency_key": "c-" + uuid.uuid4().hex}).json()
        replay = client.post(f"/v1/public/salons/{slug}/holds",
                             json={"service_id": service, "staff_id": staff, "starts_at": start,
                                   "idempotency_key": key})
        assert replay.status_code == 201, replay.text
        body = replay.json()
        assert body["replayed"] is True and body["state"] == "converted"
        assert body["appointment_id"] == confirmed["appointment_id"]
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            active = connection.execute(
                text("""SELECT count(*) FROM reservations
                        WHERE tenant_id=:t AND staff_id=:s AND state='active'"""),
                {"t": tenant["tenant_id"], "s": staff}).scalar_one()
        # Exactly one active occupant: the appointment. No dangling reactivated hold.
        assert active == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_expired_hold_releases_the_slot_for_a_new_hold(database_url):
    """AC-2: a stale hold expires safely and the freed slot books again."""
    issuer = "https://identity.example.invalid/hold-7"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        start = _first_slot(client, slug, service, day)
        hold = client.post(f"/v1/public/salons/{slug}/holds",
                           json={"service_id": service, "staff_id": staff, "starts_at": start,
                                 "idempotency_key": "h-" + uuid.uuid4().hex}).json()
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            # Expiry lives on the hold row; the reservation's active state is
            # derived from it by the sweep, so only the hold needs ageing.
            connection.execute(
                text("""UPDATE booking_holds SET expires_at = now() - interval '1 minute'
                        WHERE tenant_id=:t AND id=:id"""),
                {"t": tenant["tenant_id"], "id": hold["hold_id"]})
        stale = client.post(f"/v1/public/salons/{slug}/holds/{hold['hold_id']}/confirm",
                            json={"owner_token": hold["owner_token"], "client_id": client_id,
                                  "idempotency_key": "c-" + uuid.uuid4().hex})
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"]["code"] == "hold_expired"
        retry = client.post(f"/v1/public/salons/{slug}/holds",
                            json={"service_id": service, "staff_id": staff, "starts_at": start,
                                  "idempotency_key": "h-" + uuid.uuid4().hex})
        assert retry.status_code == 201, retry.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            state = connection.execute(
                text("SELECT state FROM booking_holds WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": hold["hold_id"]}).scalar_one()
        assert state == "expired"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_same_key_after_expiry_refreshes_the_hold_on_the_same_slot(database_url):
    """AC-2: retrying with the same key after expiry is a new hold, not a 500."""
    issuer = "https://identity.example.invalid/hold-9"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        start = _first_slot(client, slug, service, day)
        key = "h-" + uuid.uuid4().hex
        hold = client.post(f"/v1/public/salons/{slug}/holds",
                           json={"service_id": service, "staff_id": staff, "starts_at": start,
                                 "idempotency_key": key}).json()
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            connection.execute(
                text("""UPDATE booking_holds SET expires_at = now() - interval '1 minute'
                        WHERE tenant_id=:t AND id=:id"""),
                {"t": tenant["tenant_id"], "id": hold["hold_id"]})
        retry = client.post(f"/v1/public/salons/{slug}/holds",
                            json={"service_id": service, "staff_id": staff, "starts_at": start,
                                  "idempotency_key": key})
        assert retry.status_code == 201, retry.text
        assert retry.json()["replayed"] is True
        confirmed = client.post(f"/v1/public/salons/{slug}/holds/{hold['hold_id']}/confirm",
                                json={"owner_token": retry.json()["owner_token"], "client_id": client_id,
                                      "idempotency_key": "c-" + uuid.uuid4().hex})
        assert confirmed.status_code == 200, confirmed.text
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_same_key_with_different_booking_is_rejected(database_url):
    """AC-2: api-contracts — identical replay returns the receipt, different body is 409."""
    issuer = "https://identity.example.invalid/hold-10"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        slots = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": day.isoformat(),
            "date_to": day.isoformat()}).json()["slots"]
        assert len(slots) >= 2, "need two slots for the conflict probe"
        key = "h-" + uuid.uuid4().hex
        first = client.post(f"/v1/public/salons/{slug}/holds",
                            json={"service_id": service, "staff_id": staff,
                                  "starts_at": slots[0]["starts_at"], "idempotency_key": key})
        assert first.status_code == 201, first.text
        conflict = client.post(f"/v1/public/salons/{slug}/holds",
                               json={"service_id": service, "staff_id": staff,
                                     "starts_at": slots[1]["starts_at"], "idempotency_key": key})
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["detail"]["code"] == "idempotency_conflict"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_concurrent_creates_on_one_key_yield_a_single_hold(database_url):
    """AC-1: racers on the same idempotency key never create two holds."""
    issuer = "https://identity.example.invalid/hold-11"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        start = _first_slot(client, slug, service, day)
        key = "h-" + uuid.uuid4().hex
        results, lock = [], threading.Lock()

        def attempt():
            resp = client.post(f"/v1/public/salons/{slug}/holds",
                               json={"service_id": service, "staff_id": staff, "starts_at": start,
                                     "idempotency_key": key})
            with lock:
                results.append(resp.status_code)

        threads = [threading.Thread(target=attempt) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert all(code == 201 for code in results), results
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            count = connection.execute(
                text("SELECT count(*) FROM booking_holds WHERE tenant_id=:t"),
                {"t": tenant["tenant_id"]}).scalar_one()
        assert count == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)