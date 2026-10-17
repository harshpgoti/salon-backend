"""Tests for scoped public booking management access (TASK-021, FR-033).

Builds on TASK-020's hold/confirm seam (reused from test_booking_holds) and proves the
management link: it returns the client's own booking, and a forged, cross-appointment or
expired token is refused. Marketing consent is optional and never recorded implicitly.
"""

import os
import uuid
from datetime import date, timedelta

import pytest
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy import text
from test_clients import create_app, _salon, _client, _teardown
from test_booking_holds import _bookable, _first_slot


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _confirm(client, slug, service, staff, client_id, day, marketing=None):
    """Place a hold on the first slot and confirm it; return (appointment_id, management_token)."""
    start = _first_slot(client, slug, service, day)
    hold = client.post(f"/v1/public/salons/{slug}/holds",
                       json={"service_id": service, "staff_id": staff, "starts_at": start,
                             "idempotency_key": "h-" + uuid.uuid4().hex})
    assert hold.status_code == 201, hold.text
    body = {"owner_token": hold.json()["owner_token"], "client_name": "Synthetic Booker",
            "email": "booker@example.invalid", "policy_accepted": True, "policy_version": 1,
            "idempotency_key": "c-" + uuid.uuid4().hex}
    if marketing is not None:
        body["marketing_opt_in"] = marketing
    confirmed = client.post(f"/v1/public/salons/{slug}/holds/{hold.json()['hold_id']}/confirm", json=body)
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()["appointment_id"], confirmed.json()["management_token"]


def test_confirm_issues_a_management_link_that_returns_the_booking(database_url):
    """AC-1: the confirmed receipt carries a scoped token that reads back the booking."""
    issuer = "https://identity.example.invalid/mgmt-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        appointment_id, token = _confirm(client, slug, service, staff, client_id, day)
        assert token and len(token) >= 16

        view = client.get(f"/v1/public/salons/{slug}/manage/{appointment_id}",
                          headers={"Authorization": f"Bearer {token}"})
        assert view.status_code == 200, view.text
        body = view.json()
        # Only this booking's own safe fields are exposed (FR-028).
        assert body["service"] == "Cut" and body["staff"] == "Ava"
        assert body["timezone"] == "America/Toronto" and body["status"] == "confirmed"
        assert "client" not in body and "phone" not in body and "email" not in body
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_forged_management_token_is_refused(database_url):
    """AC-2: a token that is not the issued one reads as not-found, not a data leak."""
    issuer = "https://identity.example.invalid/mgmt-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        appointment_id, _token = _confirm(client, slug, service, staff, client_id, day)
        forged = client.get(f"/v1/public/salons/{slug}/manage/{appointment_id}",
                            headers={"Authorization": f"Bearer {'x' * 40}"})
        assert forged.status_code == 404
        assert forged.json()["detail"]["code"] == "appointment_not_found"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_token_for_another_appointment_is_refused(database_url):
    """AC-2: a token is scoped to one appointment; it cannot read a different one."""
    issuer = "https://identity.example.invalid/mgmt-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        first_id, _first_token = _confirm(client, slug, service, staff, client_id, day)
        second_id, second_token = _confirm(client, slug, service, staff, client_id, day)
        assert first_id != second_id
        cross = client.get(f"/v1/public/salons/{slug}/manage/{first_id}",
                           headers={"Authorization": f"Bearer {second_token}"})
        assert cross.status_code == 404
        assert cross.json()["detail"]["code"] == "appointment_not_found"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_expired_management_link_is_refused(database_url):
    """AC-2: the link expires at the appointment's end; replaying it afterwards is 409."""
    issuer = "https://identity.example.invalid/mgmt-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        appointment_id, token = _confirm(client, slug, service, staff, client_id, day)
        # Wind the link's expiry into the past directly; the issued token stays the same,
        # so only the expiry branch can change the answer.
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            connection.execute(
                text("UPDATE appointments SET management_token_expires_at = now() - interval '1 minute' "
                     "WHERE tenant_id=:t AND id=:a"),
                {"t": tenant["tenant_id"], "a": appointment_id})
        expired = client.get(f"/v1/public/salons/{slug}/manage/{appointment_id}",
                             headers={"Authorization": f"Bearer {token}"})
        assert expired.status_code == 409
        assert expired.json()["detail"]["code"] == "management_link_expired"
        mutation = client.post(f"/v1/public/salons/{slug}/manage/{appointment_id}/cancel",
            headers={"Authorization": f"Bearer {token}"},
            json={"expected_version": 1, "idempotency_key": "expired-" + uuid.uuid4().hex})
        assert mutation.status_code == 409
        assert mutation.json()["detail"]["code"] == "management_link_expired"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_booking_succeeds_without_marketing_consent_and_records_none(database_url):
    """AC-1/QA-15: marketing is optional; booking works without it and stores no consent."""
    issuer = "https://identity.example.invalid/mgmt-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        # Omit marketing entirely (the ordinary case).
        appointment_id, token = _confirm(client, slug, service, staff, client_id, day)
        view = client.get(f"/v1/public/salons/{slug}/manage/{appointment_id}",
                          headers={"Authorization": f"Bearer {token}"})
        assert view.status_code == 200, view.text
        # Booking stores no consent: the client stays marketing-ineligible unless
        # they explicitly opt in. (Written before TASK-028 this asserted the
        # consent table did not exist yet; now that it does, the guard is zero
        # consent *rows* for this client.) This guards against silently opting in.
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            rows = connection.execute(
                text("""SELECT count(*) FROM consents
                        WHERE tenant_id=:t AND client_id=(SELECT client_id FROM appointments
                            WHERE tenant_id=:t AND id=:appointment)"""),
                {"t": tenant["tenant_id"], "appointment": appointment_id}).scalar_one()
        assert rows == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_management_link_survives_a_repeated_read(database_url):
    """AC-2: reading the link repeatedly is harmless (idempotent GET)."""
    issuer = "https://identity.example.invalid/mgmt-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        appointment_id, token = _confirm(client, slug, service, staff, client_id, day)
        for _ in range(3):
            again = client.get(f"/v1/public/salons/{slug}/manage/{appointment_id}",
                               headers={"Authorization": f"Bearer {token}"})
            assert again.status_code == 200, again.text
            assert again.json()["service"] == "Cut"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_public_management_cancels_without_a_staff_session(database_url):
    issuer = "https://identity.example.invalid/public-cancel-" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    owner_client = _client(database_url, owner)
    public_client = TestClient(create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True)))
    try:
        _base, slug, service, staff, client_id = _bookable(owner_client, database_url, tenant)
        appointment_id, token = _confirm(owner_client, slug, service, staff, client_id,
                                          date.today() + timedelta(days=1))
        path = f"/v1/public/salons/{slug}/manage/{appointment_id}"
        view = public_client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert view.status_code == 200, view.text
        assert view.json()["version"] == 1
        cancel_key = "public-cancel-" + uuid.uuid4().hex
        cancelled = public_client.post(path + "/cancel", headers={"Authorization": f"Bearer {token}"},
            json={"expected_version": 1, "idempotency_key": cancel_key})
        assert cancelled.status_code == 200, cancelled.text
        assert cancelled.headers["cache-control"] == "no-store"
        assert cancelled.headers["referrer-policy"] == "no-referrer"
        assert cancelled.json()["status"] == "cancelled"
        replay = public_client.post(path + "/cancel", headers={"Authorization": f"Bearer {token}"},
            json={"expected_version": 1, "idempotency_key": cancel_key})
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert public_client.post(path + "/cancel", headers={"Authorization": f"Bearer {token}"},
            json={"expected_version": 2, "idempotency_key": "public-cancel-" + uuid.uuid4().hex}).status_code == 200
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_public_management_is_scoped_and_brand_changes_do_not_invalidate_booking_policy(database_url):
    issuer = "https://identity.example.invalid/public-policy-" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    owner_client = _client(database_url, owner)
    public_client = TestClient(create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True)))
    try:
        _base, slug, service, staff, client_id = _bookable(owner_client, database_url, tenant)
        first_id, first_token = _confirm(owner_client, slug, service, staff, client_id,
                                         date.today() + timedelta(days=1))
        second_id, second_token = _confirm(owner_client, slug, service, staff, client_id,
                                           date.today() + timedelta(days=1))
        first_path = f"/v1/public/salons/{slug}/manage/{first_id}"
        forbidden = public_client.post(first_path + "/cancel", headers={"Authorization": f"Bearer {second_token}"},
            json={"expected_version": 1, "idempotency_key": "cross-" + uuid.uuid4().hex})
        assert forbidden.status_code == 404
        assert public_client.get(first_path, headers={"Authorization": f"Bearer {first_token}"}).json()["status"] == "confirmed"

        changed = owner_client.patch(f"/v1/tenants/{tenant['tenant_id']}/settings",
                                     json={"brand_name": "Synthetic updated salon"})
        assert changed.status_code == 200, changed.text
        view = public_client.get(first_path, headers={"Authorization": f"Bearer {first_token}"})
        assert view.status_code == 200 and view.json()["can_manage"] is True
        stale = public_client.post(first_path + "/cancel", headers={"Authorization": f"Bearer {first_token}"},
            json={"expected_version": 9, "idempotency_key": "stale-" + uuid.uuid4().hex})
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "appointment_version_conflict"

        # The second confirmed booking provides an occupied target; the failed move
        # must leave the first appointment at its original slot.
        second_view = public_client.get(f"/v1/public/salons/{slug}/manage/{second_id}",
                                       headers={"Authorization": f"Bearer {second_token}"})
        conflict = public_client.post(first_path + "/reschedule", headers={"Authorization": f"Bearer {first_token}"},
            json={"starts_at": second_view.json()["starts_at"], "expected_version": 1,
                  "idempotency_key": "conflict-" + uuid.uuid4().hex})
        assert conflict.status_code == 409
        assert conflict.json()["detail"]["code"] == "slot_unavailable"
        after = public_client.get(first_path, headers={"Authorization": f"Bearer {first_token}"})
        assert after.json()["starts_at"] == view.json()["starts_at"]
        assert after.json()["version"] == 1

        policy_change = owner_client.patch(f"/v1/tenants/{tenant['tenant_id']}/settings",
            json={"cancellation_policy": "Please call the salon for changes."})
        assert policy_change.status_code == 200, policy_change.text
        refreshed = public_client.get(first_path, headers={"Authorization": f"Bearer {first_token}"})
        assert refreshed.status_code == 200
        assert refreshed.json()["can_manage"] is False
        assert refreshed.json()["manage_blocked_reason"] == "booking_policy_changed"
        blocked = public_client.post(first_path + "/cancel", headers={"Authorization": f"Bearer {first_token}"},
            json={"expected_version": 1, "idempotency_key": "policy-" + uuid.uuid4().hex})
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "booking_policy_changed"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_public_management_reschedules_with_the_saved_slot_rules(database_url):
    issuer = "https://identity.example.invalid/public-reschedule-" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    owner_client = _client(database_url, owner)
    public_client = TestClient(create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True)))
    try:
        _base, slug, service, staff, client_id = _bookable(owner_client, database_url, tenant)
        appointment_id, token = _confirm(owner_client, slug, service, staff, client_id,
                                          date.today() + timedelta(days=1))
        path = f"/v1/public/salons/{slug}/manage/{appointment_id}"
        view = public_client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert view.status_code == 200, view.text
        original = datetime.fromisoformat(view.json()["starts_at"])
        moved_start = (original + timedelta(hours=2)).isoformat()
        moved = public_client.post(path + "/reschedule", headers={"Authorization": f"Bearer {token}"},
            json={"starts_at": moved_start, "expected_version": view.json()["version"],
                  "idempotency_key": "public-resched-" + uuid.uuid4().hex})
        assert moved.status_code == 200, moved.text
        after = public_client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert after.json()["starts_at"].startswith(moved_start[:16])
        assert after.json()["version"] == view.json()["version"] + 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)

    """AC-2: a token is scoped to one appointment; it cannot read a different one."""
    issuer = "https://identity.example.invalid/mgmt-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        first_id, _first_token = _confirm(client, slug, service, staff, client_id, day)
        second_id, second_token = _confirm(client, slug, service, staff, client_id, day)
        assert first_id != second_id
        cross = client.get(f"/v1/public/salons/{slug}/manage/{first_id}",
                           headers={"Authorization": f"Bearer {second_token}"})
        assert cross.status_code == 404
        assert cross.json()["detail"]["code"] == "appointment_not_found"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
