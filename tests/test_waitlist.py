"""Tests for waitlist offers and expiring slot claims (TASK-065, FR-038).

AC-1: a slot a cancellation freed is offered to a waiting client, and holding
the offer token claims it atomically - appointment, service snapshot and
reservation land in one transaction.
AC-2: no double booking and no consent/frequency-cap bypass. Two claims on one
offer yield exactly one booking; the loser is told a named 409; the claim books
through the same `reservations` EXCLUDE constraint as any other booking; and
claiming records no consent and triggers no outbound message (FR-040).

The suite exercises the production FastAPI factory, so it covers the same route
registration and dependencies as the deployed API.
"""

import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from test_clients import _salon, _teardown
from test_booking_holds import _bookable, _first_slot
from test_booking_manage import _confirm

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.identity.main import AuthenticatedIdentity, create_app, require_identity


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _waitlist_client(database_url: str, identity: AuthenticatedIdentity) -> TestClient:
    """Exercise waitlist through the same production application factory."""
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_identity] = lambda: identity
    return TestClient(app)


def _bookable_day(client, base, tenant, staff):
    """A day with published hours that also has a shift, so a real slot exists."""
    day = date.today() + timedelta(days=1)
    if day.weekday() == 6:
        # _bookable publishes Monday-Saturday hours only, so Sunday has none.
        day += timedelta(days=1)
        client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": tenant["location_id"], "weekday": day.weekday(),
            "starts_at": "09:00", "ends_at": "17:00"})
    return day


def _waitlist_ready(client, database_url, tenant):
    base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
    day = _bookable_day(client, base, tenant, staff)
    return base, slug, service, staff, client_id, day


def _set_tenant(connection, tenant_id):
    connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                       {"t": tenant_id})


def _create_entry(client, base, client_id, service, staff, day):
    resp = client.post(f"{base}/waitlist", json={
        "client_id": client_id, "service_id": service, "staff_id": staff,
        "date_from": day.isoformat(), "date_to": day.isoformat()})
    assert resp.status_code == 201, resp.text
    return resp.json()["entry_id"]


def _offer(client, base, entry_id, starts_at, minutes=60):
    resp = client.post(f"{base}/waitlist/{entry_id}/offers", json={
        "starts_at": starts_at, "idempotency_key": "o-" + uuid.uuid4().hex,
        "expires_in_minutes": minutes})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _claim(client, slug, offer, client_id):
    return client.post(f"/v1/public/salons/{slug}/offers/{offer['offer_id']}/claim",
                       json={"owner_token": offer["owner_token"],
                             "idempotency_key": "c-" + uuid.uuid4().hex})


def test_a_freed_slot_is_offered_to_the_waitlist_and_claimed(database_url):
    """AC-1: cancel frees the slot, the offer hands it to the waitlist, the claim books."""
    issuer = "https://identity.example.invalid/wait-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    tenant_id = tenant["tenant_id"]
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        start = _first_slot(client, slug, service, day)
        room = client.post(f"{base}/resources", json={
            "location_id": tenant["location_id"], "name": "Waitlist room", "kind": "room"})
        assert room.status_code == 201, room.text
        requirement = client.put(f"{base}/services/{service}/resources", json={
            "location_id": tenant["location_id"], "expected_version": 1,
            "resource_ids": [room.json()["id"]]})
        assert requirement.status_code == 200, requirement.text
        # Book the slot, then cancel it: the reservation is released, the range is free.
        appointment_id, _token = _confirm(client, slug, service, staff, client_id, day)
        cancelled = client.post(f"{base}/appointments/{appointment_id}/cancel", json={
            "expected_version": 1, "reason": "client cancelled",
            "idempotency_key": "x-" + uuid.uuid4().hex})
        assert cancelled.status_code == 200, cancelled.text

        entry_id = _create_entry(client, base, client_id, service, staff, day)
        offer = _offer(client, base, entry_id, start)
        assert offer["state"] == "offered" and len(offer["owner_token"]) >= 16

        claimed = _claim(client, slug, offer, client_id)
        assert claimed.status_code == 201, claimed.text
        body = claimed.json()
        assert body["replayed"] is False
        assert body["appointment_id"] != appointment_id

        with engine.begin() as connection:
            _set_tenant(connection, tenant_id)
            booked = connection.execute(
                text("""SELECT status, source, starts_at FROM appointments
                        WHERE tenant_id=:t AND id=:a"""),
                {"t": tenant_id, "a": body["appointment_id"]}).mappings().one()
            assert booked["status"] == "confirmed"
            assert booked["source"] == "public_booking"
            assert booked["starts_at"] == datetime.fromisoformat(start)
            snapshot = connection.execute(
                text("""SELECT service_name, price_minor, duration_minutes FROM appointment_services
                        WHERE tenant_id=:t AND appointment_id=:a"""),
                {"t": tenant_id, "a": body["appointment_id"]}).mappings().one()
            assert snapshot["service_name"] == "Cut" and snapshot["price_minor"] == 6000
            assert snapshot["duration_minutes"] == 60
            transitions = connection.execute(
                text("SELECT count(*) FROM appointment_transitions WHERE tenant_id=:t AND appointment_id=:a"),
                {"t": tenant_id, "a": body["appointment_id"]}).scalar_one()
            assert transitions == 1
            stored = connection.execute(
                text("""SELECT state, appointment_id::text AS appointment_id FROM slot_offers
                        WHERE tenant_id=:t AND id=:o"""),
                {"t": tenant_id, "o": offer["offer_id"]}).mappings().one()
            assert stored["state"] == "claimed"
            assert stored["appointment_id"] == body["appointment_id"]
            entry_state = connection.execute(
                text("SELECT state FROM waitlist_entries WHERE tenant_id=:t AND id=:e"),
                {"t": tenant_id, "e": entry_id}).scalar_one()
            assert entry_state == "fulfilled"
            # One owner of that range: the cancelled booking released its reservation.
            active = connection.execute(
                text("""SELECT count(*) FROM reservations
                        WHERE tenant_id=:t AND staff_id=:s AND state='active'"""),
                {"t": tenant_id, "s": staff}).scalar_one()
            assert active == 1
            assert connection.execute(
                text("""SELECT count(*) FROM reservations WHERE tenant_id=:t
                        AND appointment_id=:a AND resource_id=:resource AND state='active'"""),
                {"t": tenant_id, "a": body["appointment_id"],
                 "resource": room.json()["id"]}).scalar_one() == 1
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_offer_must_fit_the_waiting_window_and_claim_revalidates_hours(database_url):
    issuer = "https://identity.example.invalid/wait-window"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
    tenant_id = tenant["tenant_id"]
    try:
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        outside = datetime.combine(day + timedelta(days=1), datetime.min.time().replace(hour=14),
                                  ZoneInfo("America/Toronto")).isoformat()
        outside_offer = client.post(f"{base}/waitlist/{entry_id}/offers", json={
            "starts_at": outside, "idempotency_key": "out-" + uuid.uuid4().hex})
        assert outside_offer.status_code == 409
        assert outside_offer.json()["detail"]["code"] == "slot_outside_waitlist_window"

        start = datetime.combine(day, datetime.min.time().replace(hour=14),
                                 ZoneInfo("America/Toronto")).isoformat()
        offer = _offer(client, base, entry_id, start)
        hours = client.put(f"{base}/locations/{tenant['location_id']}/hours", json={
            "intervals": [{"weekday": weekday, "opens_at": "09:00", "closes_at": "12:00"}
                          for weekday in range(6)]})
        assert hours.status_code == 200, hours.text
        claim = _claim(client, slug, offer, client_id)
        assert claim.status_code == 409
        assert claim.json()["detail"]["code"] == "slot_unavailable"
        with engine.begin() as connection:
            _set_tenant(connection, tenant_id)
            state = connection.execute(text("SELECT state FROM slot_offers WHERE tenant_id=:t AND id=:id"),
                                        {"t": tenant_id, "id": offer["offer_id"]}).scalar_one()
            assert state == "retracted"
            assert connection.execute(text("SELECT count(*) FROM appointments WHERE tenant_id=:t"),
                                      {"t": tenant_id}).scalar_one() == 0
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_claim_loses_cleanly_when_another_staff_member_takes_the_required_resource(database_url):
    issuer = "https://identity.example.invalid/wait-resource-race"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
    tenant_id = tenant["tenant_id"]
    try:
        room = client.post(f"{base}/resources", json={
            "location_id": tenant["location_id"], "name": "Shared waitlist room", "kind": "room"}).json()["id"]
        assert client.put(f"{base}/services/{service}/resources", json={
            "location_id": tenant["location_id"], "expected_version": 1,
            "resource_ids": [room]}).status_code == 200
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        start = datetime.combine(day, datetime.min.time().replace(hour=14),
                                 ZoneInfo("America/Toronto"))
        offer = _offer(client, base, entry_id, start.isoformat())

        other_staff = client.post(f"{base}/staff", json={"display_name": "Morgan"}).json()["id"]
        assert client.put(f"{base}/staff/{other_staff}/locations", json={
            "location_ids": [tenant["location_id"]]}).status_code == 200
        assert client.put(f"{base}/staff/{other_staff}/qualifications", json={
            "service_ids": [service]}).status_code == 200
        assert client.post(f"{base}/staff/{other_staff}/shifts", json={
            "location_id": tenant["location_id"], "weekday": day.weekday(),
            "starts_at": "09:00", "ends_at": "17:00"}).status_code == 201
        competing = client.post(f"{base}/appointments", json={
            "location_id": tenant["location_id"], "client_id": client_id,
            "staff_id": other_staff, "service_id": service,
            "starts_at": start.isoformat(), "source": "walk_in",
            "idempotency_key": "resource-race-" + uuid.uuid4().hex})
        assert competing.status_code == 201, competing.text

        claim = _claim(client, slug, offer, client_id)
        assert claim.status_code == 409
        assert claim.json()["detail"]["code"] == "slot_unavailable"
        with engine.begin() as connection:
            _set_tenant(connection, tenant_id)
            state = connection.execute(text("SELECT state FROM slot_offers WHERE tenant_id=:t AND id=:id"),
                                        {"t": tenant_id, "id": offer["offer_id"]}).scalar_one()
            assert state == "retracted"
            assert connection.execute(text("SELECT count(*) FROM appointments WHERE tenant_id=:t"),
                                      {"t": tenant_id}).scalar_one() == 1
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_two_claims_on_one_offer_leave_exactly_one_booking(database_url):
    """AC-2: the first claim wins with 201; the loser is told so by name, not double-booked."""
    issuer = "https://identity.example.invalid/wait-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    tenant_id = tenant["tenant_id"]
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        start = _first_slot(client, slug, service, day)
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        offer = _offer(client, base, entry_id, start)

        first = _claim(client, slug, offer, client_id)
        assert first.status_code == 201, first.text
        second = _claim(client, slug, offer, client_id)
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["code"] == "offer_claimed"

        with engine.begin() as connection:
            _set_tenant(connection, tenant_id)
            appointments = connection.execute(
                text("""SELECT id::text AS id FROM appointments
                        WHERE tenant_id=:t AND staff_id=:s AND starts_at=:start"""),
                {"t": tenant_id, "s": staff, "start": start}).scalars().all()
            assert appointments == [first.json()["appointment_id"]]
            active = connection.execute(
                text("""SELECT count(*) FROM reservations
                        WHERE tenant_id=:t AND staff_id=:s AND state='active'"""),
                {"t": tenant_id, "s": staff}).scalar_one()
            assert active == 1
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_concurrent_claims_have_exactly_one_winner(database_url):
    """AC-2 (QA-04): racing claims on one offer yield exactly one 201, never a double."""
    issuer = "https://identity.example.invalid/wait-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    tenant_id = tenant["tenant_id"]
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        start = _first_slot(client, slug, service, day)
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        offer = _offer(client, base, entry_id, start)

        results, lock, barrier = [], threading.Lock(), threading.Barrier(6)

        def attempt(index):
            local = _waitlist_client(database_url, owner)
            barrier.wait()
            resp = _claim(local, slug, offer, client_id)
            with lock:
                results.append((resp.status_code, resp.json().get("detail", {}).get("code")))

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        winners = [code for code, _ in results if code == 201]
        assert len(winners) == 1, f"expected exactly one winner, got {results}"
        # Every loser is told a named conflict, never a 500.
        assert all(code in (201, 409) for code, _ in results), results
        assert all(name is None or name in ("offer_claimed", "slot_unavailable")
                   for _, name in results), results
        with engine.begin() as connection:
            _set_tenant(connection, tenant_id)
            appointments = connection.execute(
                text("""SELECT count(*) FROM appointments
                        WHERE tenant_id=:t AND staff_id=:s AND starts_at=:start"""),
                {"t": tenant_id, "s": staff, "start": start}).scalar_one()
            assert appointments == 1
            active = connection.execute(
                text("""SELECT count(*) FROM reservations
                        WHERE tenant_id=:t AND staff_id=:s AND state='active'"""),
                {"t": tenant_id, "s": staff}).scalar_one()
            assert active == 1
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_a_claim_loses_the_race_against_a_booking_that_took_the_slot(database_url):
    """AC-2: the claim books through the same EXCLUDE constraint, so it cannot win twice."""
    issuer = "https://identity.example.invalid/wait-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    tenant_id = tenant["tenant_id"]
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        start = _first_slot(client, slug, service, day)
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        offer = _offer(client, base, entry_id, start)

        # Someone books the offered start directly, before the client claims it.
        booked = client.post(f"{base}/appointments", json={
            "client_id": client_id, "staff_id": staff, "service_id": service,
            "starts_at": start, "idempotency_key": "b-" + uuid.uuid4().hex})
        assert booked.status_code == 201, booked.text

        claimed = _claim(client, slug, offer, client_id)
        assert claimed.status_code == 409, claimed.text
        assert claimed.json()["detail"]["code"] == "slot_unavailable"

        with engine.begin() as connection:
            _set_tenant(connection, tenant_id)
            # Exactly one owner of that range, and the offer was retracted so the
            # stale link cannot keep competing for a slot it already lost.
            occupied = connection.execute(
                text("""SELECT count(*) FROM reservations
                        WHERE tenant_id=:t AND staff_id=:s AND state='active'
                          AND occupied_range && tstzrange(:from_ts,:to_ts,'[)')"""),
                {"t": tenant_id, "s": staff, "from_ts": start,
                 "to_ts": (datetime.fromisoformat(start) + timedelta(minutes=60)).isoformat()}
            ).scalar_one()
            assert occupied == 1
            state = connection.execute(
                text("SELECT state FROM slot_offers WHERE tenant_id=:t AND id=:o"),
                {"t": tenant_id, "o": offer["offer_id"]}).scalar_one()
            assert state == "retracted"
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_an_expired_offer_cannot_be_claimed(database_url):
    """AC-2: a lapsed offer is answered with the named expiry, never booked."""
    issuer = "https://identity.example.invalid/wait-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    tenant_id = tenant["tenant_id"]
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        start = _first_slot(client, slug, service, day)
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        offer = _offer(client, base, entry_id, start, minutes=5)

        with engine.begin() as connection:
            _set_tenant(connection, tenant_id)
            # Age only the offer row; the same issued token still proves possession.
            connection.execute(
                text("""UPDATE slot_offers SET expires_at = now() - interval '1 minute'
                        WHERE tenant_id=:t AND id=:o"""),
                {"t": tenant_id, "o": offer["offer_id"]})

        claimed = _claim(client, slug, offer, client_id)
        assert claimed.status_code == 409, claimed.text
        assert claimed.json()["detail"]["code"] == "offer_expired"
        with engine.begin() as connection:
            _set_tenant(connection, tenant_id)
            # The sweep expired it by state transition, and nothing was booked.
            state = connection.execute(
                text("SELECT state FROM slot_offers WHERE tenant_id=:t AND id=:o"),
                {"t": tenant_id, "o": offer["offer_id"]}).scalar_one()
            assert state == "expired"
            appointments = connection.execute(
                text("SELECT count(*) FROM appointments WHERE tenant_id=:t"),
                {"t": tenant_id}).scalar_one()
            assert appointments == 0
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_claiming_records_no_consent_and_no_outbound_message(database_url):
    """AC-2 (FR-040): the claim endpoint books; it never opts a client in."""
    issuer = "https://identity.example.invalid/wait-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    tenant_id = tenant["tenant_id"]
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        start = _first_slot(client, slug, service, day)
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        offer = _offer(client, base, entry_id, start)

        def _counts():
            with engine.begin() as connection:
                _set_tenant(connection, tenant_id)
                return (connection.execute(
                            text("SELECT count(*) FROM consents WHERE tenant_id=:t AND client_id=:c"),
                            {"t": tenant_id, "c": client_id}).scalar_one(),
                        connection.execute(
                            text("SELECT count(*) FROM communications WHERE tenant_id=:t"),
                            {"t": tenant_id}).scalar_one())

        assert _counts() == (0, 0)
        claimed = _claim(client, slug, offer, client_id)
        assert claimed.status_code == 201, claimed.text
        # No consent row and no marketing communication were written by the claim.
        assert _counts() == (0, 0)
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_an_offer_is_not_claimable_from_another_salon_link(database_url):
    """AC-2 (FR-028): a cross-tenant offer is an indistinguishable 404."""
    issuer = "https://identity.example.invalid/wait-7"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    other_engine, other_owner, other_tenant = _salon(database_url, "https://identity.example.invalid/wait-7b")
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        start = _first_slot(client, slug, service, day)
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        offer = _offer(client, base, entry_id, start)

        stranger = _waitlist_client(database_url, other_owner)
        claimed = _claim(stranger, other_tenant["booking_slug"], offer, client_id)
        assert claimed.status_code == 404, claimed.text
        assert claimed.json()["detail"]["code"] == "offer_not_found"
        # The real offer is untouched by the attempt.
        with engine.begin() as connection:
            _set_tenant(connection, tenant["tenant_id"])
            state = connection.execute(
                text("SELECT state FROM slot_offers WHERE tenant_id=:t AND id=:o"),
                {"t": tenant["tenant_id"], "o": offer["offer_id"]}).scalar_one()
        assert state == "offered"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], "https://identity.example.invalid/wait-7b")


def test_a_claim_requires_only_its_bearer_token_and_rejects_client_ids(database_url):
    """AC-1: a bearer token identifies the waiting client without a public client id."""
    issuer = "https://identity.example.invalid/wait-8"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    tenant_id = tenant["tenant_id"]
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        start = _first_slot(client, slug, service, day)
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        offer = _offer(client, base, entry_id, start)
        with_client_id = client.post(f"/v1/public/salons/{slug}/offers/{offer['offer_id']}/claim",
                                     json={"owner_token": offer["owner_token"], "client_id": client_id,
                                           "idempotency_key": "c-" + uuid.uuid4().hex})
        assert with_client_id.status_code == 422, with_client_id.text
        forged = client.post(f"/v1/public/salons/{slug}/offers/{offer['offer_id']}/claim",
                             json={"owner_token": "z" * 32,
                                   "idempotency_key": "c-" + uuid.uuid4().hex})
        assert forged.status_code == 404, forged.text
        assert forged.json()["detail"]["code"] == "offer_not_found"

        claimed = _claim(client, slug, offer, client_id)
        assert claimed.status_code == 201, claimed.text
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_a_replayed_claim_returns_the_same_appointment(database_url):
    """AC-1: the claim's idempotency key replays the booking, never a second one."""
    issuer = "https://identity.example.invalid/wait-9"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    tenant_id = tenant["tenant_id"]
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        start = _first_slot(client, slug, service, day)
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        offer = _offer(client, base, entry_id, start)
        key = "c-" + uuid.uuid4().hex

        first = client.post(f"/v1/public/salons/{slug}/offers/{offer['offer_id']}/claim",
                            json={"owner_token": offer["owner_token"],
                                  "idempotency_key": key})
        assert first.status_code == 201, first.text
        replay = client.post(f"/v1/public/salons/{slug}/offers/{offer['offer_id']}/claim",
                             json={"owner_token": offer["owner_token"],
                                   "idempotency_key": key})
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        assert replay.json()["appointment_id"] == first.json()["appointment_id"]
        with engine.begin() as connection:
            _set_tenant(connection, tenant_id)
            appointments = connection.execute(
                text("SELECT count(*) FROM appointments WHERE tenant_id=:t"),
                {"t": tenant_id}).scalar_one()
        assert appointments == 1
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_withdrawing_an_entry_retracts_its_open_offers(database_url):
    """AC-1: withdrawing stops a live offer, and the retracted link cannot claim."""
    issuer = "https://identity.example.invalid/wait-10"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    tenant_id = tenant["tenant_id"]
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        start = _first_slot(client, slug, service, day)
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        offer = _offer(client, base, entry_id, start)

        withdrawn = client.post(f"{base}/waitlist/{entry_id}/withdraw")
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["state"] == "withdrawn"
        assert withdrawn.json()["offers_retracted"] == 1
        # A repeated withdraw is a harmless replay, not an error.
        again = client.post(f"{base}/waitlist/{entry_id}/withdraw")
        assert again.status_code == 200 and again.json()["replayed"] is True

        claimed = _claim(client, slug, offer, client_id)
        assert claimed.status_code == 409, claimed.text
        assert claimed.json()["detail"]["code"] == "offer_retracted"
        with engine.begin() as connection:
            _set_tenant(connection, tenant_id)
            appointments = connection.execute(
                text("SELECT count(*) FROM appointments WHERE tenant_id=:t"),
                {"t": tenant_id}).scalar_one()
        assert appointments == 0
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_claim_and_withdraw_serialize_on_the_waitlist_entry(database_url):
    """A racing claim and withdrawal have one terminal result and no deadlock/500."""
    issuer = "https://identity.example.invalid/wait-claim-withdraw"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    tenant_id = tenant["tenant_id"]
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        offer = _offer(client, base, entry_id, _first_slot(client, slug, service, day))
        barrier = threading.Barrier(2)

        def claim():
            local = _waitlist_client(database_url, owner)
            barrier.wait()
            return local.post(f"/v1/public/salons/{slug}/offers/{offer['offer_id']}/claim",
                              json={"owner_token": offer["owner_token"],
                                    "idempotency_key": "race-" + uuid.uuid4().hex})

        def withdraw():
            local = _waitlist_client(database_url, owner)
            barrier.wait()
            return local.post(f"{base}/waitlist/{entry_id}/withdraw")

        with ThreadPoolExecutor(max_workers=2) as pool:
            claim_future, withdraw_future = pool.submit(claim), pool.submit(withdraw)
            claimed, withdrawn = claim_future.result(timeout=30), withdraw_future.result(timeout=30)

        assert claimed.status_code in (201, 409), claimed.text
        assert withdrawn.status_code in (200, 409), withdrawn.text
        assert claimed.status_code != 500 and withdrawn.status_code != 500
        if claimed.status_code == 409:
            assert claimed.json()["detail"]["code"] in {
                "offer_retracted", "waitlist_entry_not_open"}
        with engine.begin() as connection:
            _set_tenant(connection, tenant_id)
            facts = connection.execute(text("""SELECT e.state AS entry_state,
                o.state AS offer_state,
                (SELECT count(*) FROM appointments a WHERE a.tenant_id=e.tenant_id
                  AND a.client_id=e.client_id AND a.starts_at=o.starts_at) AS bookings
                FROM waitlist_entries e JOIN slot_offers o
                  ON o.tenant_id=e.tenant_id AND o.entry_id=e.id
                WHERE e.tenant_id=:tenant AND e.id=:entry AND o.id=:offer"""),
                {"tenant": tenant_id, "entry": entry_id, "offer": offer["offer_id"]}).mappings().one()
        if facts["entry_state"] == "fulfilled":
            assert facts["offer_state"] == "claimed" and facts["bookings"] == 1
        else:
            assert facts["entry_state"] == "withdrawn"
            assert facts["offer_state"] == "retracted" and facts["bookings"] == 0
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_offering_the_same_key_twice_offers_once(database_url):
    """AC-1: the offer command's idempotency key replays the offer, not a second one."""
    issuer = "https://identity.example.invalid/wait-11"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    tenant_id = tenant["tenant_id"]
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        start = _first_slot(client, slug, service, day)
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        key = "o-" + uuid.uuid4().hex
        first = client.post(f"{base}/waitlist/{entry_id}/offers",
                            json={"starts_at": start, "idempotency_key": key})
        second = client.post(f"{base}/waitlist/{entry_id}/offers",
                             json={"starts_at": start, "idempotency_key": key})
        assert first.status_code == 201 and second.status_code == 201
        assert second.json()["offer_id"] == first.json()["offer_id"]
        # A live offer is not re-tokened: the client's delivered link stays valid.
        assert second.json()["replayed"] is True
        with engine.begin() as connection:
            _set_tenant(connection, tenant_id)
            offers = connection.execute(
                text("SELECT count(*) FROM slot_offers WHERE tenant_id=:t"),
                {"t": tenant_id}).scalar_one()
        assert offers == 1
        # The token that was delivered still claims the offer.
        claimed = _claim(client, slug, first.json(), client_id)
        assert claimed.status_code == 201, claimed.text
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_waitlist_entry_cannot_have_two_live_offers(database_url):
    """An entry's single claim link must not create two separate bookings."""
    issuer = "https://identity.example.invalid/wait-one-live"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        first_start = datetime.fromisoformat(_first_slot(client, slug, service, day))
        first = _offer(client, base, entry_id, first_start.isoformat())

        second = client.post(f"{base}/waitlist/{entry_id}/offers", json={
            "starts_at": (first_start + timedelta(hours=2)).isoformat(),
            "idempotency_key": "second-" + uuid.uuid4().hex,
            "expires_in_minutes": 60})
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["code"] == "offer_already_pending"
        assert first["state"] == "offered"
        with engine.begin() as connection:
            _set_tenant(connection, tenant["tenant_id"])
            offers = connection.execute(text("""SELECT count(*) FROM slot_offers
                WHERE tenant_id=:tenant AND entry_id=:entry AND state='offered'
                  AND expires_at > now()"""),
                {"tenant": tenant["tenant_id"], "entry": entry_id}).scalar_one()
        assert offers == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_waitlist_create_rejects_bad_input_and_other_tenants(database_url):
    """AC-2: an entry belongs to the salon that created it, with valid references."""
    issuer = "https://identity.example.invalid/wait-12"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    other_engine, other_owner, other_tenant = _salon(database_url, "https://identity.example.invalid/wait-12b")
    try:
        base, _slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        stranger = _waitlist_client(database_url, other_owner)
        assert stranger.get(f"{base}/waitlist").status_code == 404
        assert stranger.post(base + "/waitlist", json={
            "client_id": client_id, "service_id": service,
            "date_from": day.isoformat(), "date_to": day.isoformat()}).status_code == 404
        assert client.post(base + "/waitlist", json={
            "client_id": client_id, "service_id": service,
            "date_from": day.isoformat(), "date_to": day.isoformat()}).status_code == 201
        # A missing client, an inverted window and an unqualified staff member are all
        # refused, and nothing is written by a refused request.
        assert client.post(base + "/waitlist", json={
            "client_id": str(uuid.uuid4()), "service_id": service,
            "date_from": day.isoformat(), "date_to": day.isoformat()}).status_code == 404
        inverted = client.post(base + "/waitlist", json={
            "client_id": client_id, "service_id": service,
            "date_from": day.isoformat(), "date_to": (day - timedelta(days=1)).isoformat()})
        assert inverted.status_code == 422
        assert inverted.json()["detail"]["code"] == "date_range_inverted"
        unqualified = client.post(base + "/waitlist", json={
            "client_id": client_id, "service_id": service, "staff_id": str(uuid.uuid4()),
            "date_from": day.isoformat(), "date_to": day.isoformat()})
        assert unqualified.status_code in (404, 409)

        listed = client.get(f"{base}/waitlist", params={"state": "open"})
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["total"] == 1 and body["items"][0]["client_id"] == client_id
        assert body["items"][0]["date_from"] == day.isoformat()
        assert client.get(f"{base}/waitlist", params={"state": "withdrawn"}).json()["total"] == 0
        assert client.get(f"{base}/waitlist", params={"state": "nonsense"}).status_code == 422
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], "https://identity.example.invalid/wait-12b")


@pytest.mark.parametrize("retry_kind", ["wrong_token", "extra_client_id", "restricted"])
def test_claim_replay_checks_bearer_and_does_not_rebook(database_url, retry_kind):
    issuer = "https://identity.example.invalid/wait-replay-" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    client = _waitlist_client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, slug, service, staff, client_id, day = _waitlist_ready(client, database_url, tenant)
        entry_id = _create_entry(client, base, client_id, service, staff, day)
        offer = _offer(client, base, entry_id, _first_slot(client, slug, service, day))
        url = f"/v1/public/salons/{slug}/offers/{offer['offer_id']}/claim"
        payload = {"owner_token": offer["owner_token"],
                   "idempotency_key": "retry-" + uuid.uuid4().hex}
        first = client.post(url, json=payload)
        assert first.status_code == 201, first.text
        if retry_kind == "wrong_token":
            payload["owner_token"] = "invalid-owner-token-0000000000"
        elif retry_kind == "extra_client_id":
            payload["client_id"] = client_id
        elif retry_kind == "restricted":
            with engine.begin() as conn:
                _set_tenant(conn, tid)
                conn.execute(text("UPDATE tenant_subscriptions SET status='restricted' WHERE tenant_id=:t"), {"t": tid})
        replay = client.post(url, json=payload)
        if retry_kind == "restricted":
            assert replay.status_code == 200, replay.text
            assert replay.json()["appointment_id"] == first.json()["appointment_id"]
        elif retry_kind == "wrong_token":
            assert replay.status_code == 404, replay.text
            assert replay.json()["detail"]["code"] == "offer_not_found"
        else:
            assert replay.status_code == 422, replay.text
        with engine.connect() as conn:
            _set_tenant(conn, tid)
            assert conn.execute(text("SELECT count(*) FROM appointments WHERE tenant_id=:t"), {"t": tid}).scalar_one() == 1
    finally:
        _teardown(engine, [tid], issuer)
