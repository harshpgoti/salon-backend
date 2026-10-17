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


def _bookable(client, database_url, tenant, day=None):
    base = f"/v1/tenants/{tenant['tenant_id']}"
    loc = tenant["location_id"]
    slug = _slug(database_url, tenant["tenant_id"], loc)
    client.put(f"{base}/locations/{loc}/hours", json={
        "intervals": [{"weekday": d, "opens_at": "09:00", "closes_at": "17:00"} for d in range(7)]})
    service = client.post(f"{base}/services",
                          json={"name": "Cut", "duration_minutes": 60, "price_minor": 6000}).json()["id"]
    staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
    client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [loc]})
    client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
    shift_days = [day or date.today() + timedelta(days=1)]
    # Calendar fixtures book on the next weekday, while hold fixtures book
    # tomorrow. Cover both when tomorrow is Sunday.
    if day is None and shift_days[0].weekday() == 6:
        shift_days.append(shift_days[0] + timedelta(days=1))
    for shift_day in shift_days:
        client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": loc, "weekday": shift_day.weekday(),
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


def _confirmation_payload(owner_token, key=None):
    return {"owner_token": owner_token, "client_name": "Synthetic Booker",
            "email": "booker@example.invalid", "country": "CA",
            "policy_accepted": True, "policy_version": 1,
            "idempotency_key": key or "c-" + uuid.uuid4().hex}


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
                                json=_confirmation_payload(hold.json()["owner_token"]))
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


def test_public_confirmation_creates_unverified_contact_and_replay_safe_receipt(database_url):
    """TASK-021: public contact and current policy produce one scoped receipt, never consent."""
    issuer = "https://identity.example.invalid/public-receipt"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, _ = _bookable(client, database_url, tenant)
        start = _first_slot(client, slug, service, date.today() + timedelta(days=1))
        hold = client.post(f"/v1/public/salons/{slug}/holds", json={
            "service_id": service, "staff_id": staff, "starts_at": start,
            "idempotency_key": "public-receipt-" + uuid.uuid4().hex,
        })
        assert hold.status_code == 201, hold.text
        terms = client.get(f"/v1/public/salons/{slug}").json()["booking_policy"]
        payload = {
            "owner_token": hold.json()["owner_token"],
            "client_name": "Synthetic Public Booker",
            "email": " Booker@Example.Invalid ",
            "country": "CA",
            "policy_accepted": True,
            "policy_version": terms["version"],
            "marketing_opt_in": True,
            "idempotency_key": "public-confirm-" + uuid.uuid4().hex,
        }
        confirmed = client.post(
            f"/v1/public/salons/{slug}/holds/{hold.json()['hold_id']}/confirm", json=payload)
        assert confirmed.status_code == 200, confirmed.text
        first_receipt = confirmed.json()
        assert first_receipt["marketing_opt_in_recorded"] is False
        assert isinstance(first_receipt["management_token"], str)

        replay = client.post(
            f"/v1/public/salons/{slug}/holds/{hold.json()['hold_id']}/confirm", json=payload)
        assert replay.status_code == 200, replay.text
        assert replay.json()["appointment_id"] == first_receipt["appointment_id"]
        assert replay.json()["management_token"] == first_receipt["management_token"]
        assert replay.json()["replayed"] is True

        managed = client.get(
            f"/v1/public/salons/{slug}/manage/{first_receipt['appointment_id']}",
            headers={"Authorization": f"Bearer {first_receipt['management_token']}"})
        assert managed.status_code == 200, managed.text
        assert managed.json()["service"] == "Cut"
        assert "client" not in managed.json() and "tenant_id" not in managed.json()

        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                               {"t": tenant["tenant_id"]})
            saved = connection.execute(text("""SELECT c.id, c.name, c.source,
                    p.kind, p.normalized_value, p.display_value, p.verification, p.source AS contact_source
                    FROM clients c JOIN client_contacts p
                      ON p.tenant_id=c.tenant_id AND p.client_id=c.id
                    WHERE c.tenant_id=:tenant AND EXISTS (
                        SELECT 1 FROM appointments a WHERE a.tenant_id=c.tenant_id
                          AND a.client_id=c.id AND a.id=:appointment)"""),
                {"tenant": tenant["tenant_id"], "appointment": first_receipt["appointment_id"]}).mappings().one()
            consent_count = connection.execute(text("""SELECT count(*) FROM consents
                    WHERE tenant_id=:tenant AND client_id=:client AND purpose='marketing'"""),
                {"tenant": tenant["tenant_id"], "client": saved["id"]}).scalar_one()
        assert saved["name"] == "Synthetic Public Booker"
        assert saved["source"] == "public_booking" and saved["contact_source"] == "public_booking"
        assert saved["kind"] == "email" and saved["normalized_value"] == "Booker@example.invalid"
        assert saved["display_value"] == "Booker@Example.Invalid" and saved["verification"] == "unverified"
        assert consent_count == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_public_confirmation_rejects_missing_contact_policy_and_stale_policy_version(database_url):
    """TASK-021: validation is fail-closed and policy versions are checked at commit."""
    issuer = "https://identity.example.invalid/public-policy"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, _ = _bookable(client, database_url, tenant)
        start = _first_slot(client, slug, service, date.today() + timedelta(days=1))
        hold = client.post(f"/v1/public/salons/{slug}/holds", json={
            "service_id": service, "staff_id": staff, "starts_at": start,
            "idempotency_key": "public-policy-" + uuid.uuid4().hex,
        }).json()
        confirm_url = f"/v1/public/salons/{slug}/holds/{hold['hold_id']}/confirm"
        missing_contact = _confirmation_payload(hold["owner_token"])
        missing_contact.pop("email")
        assert client.post(confirm_url, json=missing_contact).status_code == 422
        unaccepted = _confirmation_payload(hold["owner_token"])
        unaccepted["policy_accepted"] = False
        assert client.post(confirm_url, json=unaccepted).json()["detail"]["code"] == "booking_policy_required"
        malformed_contact = _confirmation_payload(hold["owner_token"])
        malformed_contact["email"] = "not-an-email"
        assert client.post(confirm_url, json=malformed_contact).json()["detail"]["code"] == "contact_not_normalizable"

        changed = client.patch(f"{base}/settings", json={"cancellation_policy": "Synthetic updated terms"})
        assert changed.status_code == 200, changed.text
        stale = _confirmation_payload(hold["owner_token"])
        assert client.post(confirm_url, json=stale).json()["detail"]["code"] == "booking_policy_changed"

        fresh_terms = client.get(f"/v1/public/salons/{slug}").json()["booking_policy"]
        current = _confirmation_payload(hold["owner_token"])
        current["policy_version"] = fresh_terms["version"]
        confirmed = client.post(confirm_url, json=current)
        assert confirmed.status_code == 200, confirmed.text
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_management_token_is_booking_scoped_and_expires(database_url):
    """TASK-021: forged/cross-booking tokens are indistinguishable and expired links deny."""
    issuer = "https://identity.example.invalid/public-management-scope"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, slug, service, staff, _ = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        slots = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": day.isoformat(), "date_to": day.isoformat()}).json()["slots"]
        assert len(slots) >= 5
        receipts = []
        for index, slot in enumerate((slots[0], slots[4])):
            hold = client.post(f"/v1/public/salons/{slug}/holds", json={
                "service_id": service, "staff_id": slot["staff_id"], "starts_at": slot["starts_at"],
                "idempotency_key": f"management-hold-{index}-" + uuid.uuid4().hex,
            }).json()
            confirmed = client.post(
                f"/v1/public/salons/{slug}/holds/{hold['hold_id']}/confirm",
                json=_confirmation_payload(hold["owner_token"]))
            assert confirmed.status_code == 200, confirmed.text
            receipts.append(confirmed.json())

        own = client.get(
            f"/v1/public/salons/{slug}/manage/{receipts[0]['appointment_id']}",
            headers={"Authorization": f"Bearer {receipts[0]['management_token']}"})
        crossed = client.get(
            f"/v1/public/salons/{slug}/manage/{receipts[1]['appointment_id']}",
            headers={"Authorization": f"Bearer {receipts[0]['management_token']}"})
        forged = client.get(
            f"/v1/public/salons/{slug}/manage/{receipts[0]['appointment_id']}",
            headers={"Authorization": f"Bearer {'f' * 64}"})
        assert own.status_code == 200
        assert own.headers["cache-control"] == "no-store"
        assert own.headers["referrer-policy"] == "no-referrer"
        assert crossed.status_code == forged.status_code == 404

        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                               {"t": tenant["tenant_id"]})
            connection.execute(text("""UPDATE appointments SET management_token_expires_at=now()-interval '1 second'
                    WHERE tenant_id=:tenant AND id=:appointment"""),
                {"tenant": tenant["tenant_id"], "appointment": receipts[1]["appointment_id"]})
        expired = client.get(
            f"/v1/public/salons/{slug}/manage/{receipts[1]['appointment_id']}",
            headers={"Authorization": f"Bearer {receipts[1]['management_token']}"})
        assert expired.status_code == 409
        assert expired.json()["detail"]["code"] == "management_link_expired"
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
                    json=_confirmation_payload(hold["owner_token"]))

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


def test_resource_required_by_service_is_reserved_across_different_staff(database_url):
    """AC-1: required resources have one conflict winner even across staff assignments."""
    issuer = "https://identity.example.invalid/hold-resource-race"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, first_staff, client_id = _bookable(client, database_url, tenant)
        location = tenant["location_id"]
        room = client.post(f"{base}/resources", json={
            "location_id": location, "name": "Shared booking room", "kind": "room"})
        assert room.status_code == 201, room.text
        configured = client.put(f"{base}/services/{service}/resources", json={
            "location_id": location, "expected_version": 1,
            "resource_ids": [room.json()["id"]]})
        assert configured.status_code == 200, configured.text

        second_staff = client.post(f"{base}/staff", json={"display_name": "Noah"}).json()["id"]
        assert client.put(f"{base}/staff/{second_staff}/locations", json={
            "location_ids": [location]}).status_code == 200
        assert client.put(f"{base}/staff/{second_staff}/qualifications", json={
            "service_ids": [service]}).status_code == 200
        assert client.post(f"{base}/staff/{second_staff}/shifts", json={
            "location_id": location, "weekday": (date.today() + timedelta(days=1)).weekday(),
            "starts_at": "09:00", "ends_at": "17:00"}).status_code == 201

        starts_at = _first_slot(client, slug, service, date.today() + timedelta(days=1))
        first_key = "resource-first-" + uuid.uuid4().hex
        first_payload = {"service_id": service, "staff_id": first_staff,
                         "starts_at": starts_at, "idempotency_key": first_key}
        first = client.post(f"/v1/public/salons/{slug}/holds", json=first_payload)
        assert first.status_code == 201, first.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                               {"t": tenant["tenant_id"]})
            connection.execute(text("""UPDATE booking_holds SET expires_at=now()-interval '1 second'
                    WHERE tenant_id=:tenant AND id=:hold"""),
                                {"tenant": tenant["tenant_id"], "hold": first.json()["hold_id"]})
        replay = client.post(f"/v1/public/salons/{slug}/holds", json=first_payload)
        second = client.post(f"/v1/public/salons/{slug}/holds", json={
            "service_id": service, "staff_id": second_staff, "starts_at": starts_at,
            "idempotency_key": "resource-second-" + uuid.uuid4().hex})

        assert replay.status_code == 201 and replay.json()["replayed"] is True, replay.text
        assert replay.json()["hold_id"] == first.json()["hold_id"]
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["code"] == "slot_unavailable"

        confirmed = client.post(
            f"/v1/public/salons/{slug}/holds/{replay.json()['hold_id']}/confirm",
            json=_confirmation_payload(replay.json()["owner_token"],
                                       "resource-confirm-" + uuid.uuid4().hex))
        assert confirmed.status_code == 200, confirmed.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                               {"t": tenant["tenant_id"]})
            rows = connection.execute(text("""SELECT resource_id::text FROM reservations
                    WHERE tenant_id=:tenant AND appointment_id=:appointment AND state='active'
                      AND resource_id IS NOT NULL"""),
                                      {"tenant": tenant["tenant_id"],
                                       "appointment": confirmed.json()["appointment_id"]}).scalars().all()
        assert rows == [room.json()["id"]]
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
                           json=_confirmation_payload("y" * 32)).status_code == 404
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
        payload = _confirmation_payload(hold["owner_token"])
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
                                json=_confirmation_payload(hold["owner_token"])).json()
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
                            json=_confirmation_payload(hold["owner_token"]))
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
                                json=_confirmation_payload(retry.json()["owner_token"]))
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
