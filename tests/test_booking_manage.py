"""Tests for scoped public booking management access (TASK-021, FR-033).

Builds on TASK-020's hold/confirm seam (reused from test_booking_holds) and proves the
management link: it returns the client's own booking, and a forged, cross-appointment or
expired token is refused. Marketing consent is optional and never recorded implicitly.
"""

import os
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from test_clients import _salon, _client, _teardown
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
    body = {"owner_token": hold.json()["owner_token"], "client_id": client_id,
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

        view = client.get(f"/v1/public/salons/{slug}/manage/{appointment_id}", params={"token": token})
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
                            params={"token": "x" * 40})
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
                           params={"token": second_token})
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
        expired = client.get(f"/v1/public/salons/{slug}/manage/{appointment_id}", params={"token": token})
        assert expired.status_code == 409
        assert expired.json()["detail"]["code"] == "management_link_expired"
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
        view = client.get(f"/v1/public/salons/{slug}/manage/{appointment_id}", params={"token": token})
        assert view.status_code == 200, view.text
        # No consent table exists for FR-040 yet, and none was created: the client is
        # marketing-ineligible. This guards against a future task silently opting in.
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            tables = connection.execute(
                text("""SELECT count(*) FROM information_schema.tables
                        WHERE table_schema='public' AND table_name IN
                        ('consents','communication_consents','client_consents')""")).scalar_one()
        assert tables == 0
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
            again = client.get(f"/v1/public/salons/{slug}/manage/{appointment_id}", params={"token": token})
            assert again.status_code == 200, again.text
            assert again.json()["service"] == "Cut"
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
                           params={"token": second_token})
        assert cross.status_code == 404
        assert cross.json()["detail"]["code"] == "appointment_not_found"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
