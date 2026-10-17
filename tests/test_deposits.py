"""Tests for validated, provider-hosted customer deposits (TASK-066, FR-038).

The provider is synthetic and in-app: success/fail/dispute is a request field. These tests
prove the load-bearing invariants:

  - AC-1: a paid deposit lands in the salon's own deposit ledger (append-only, signed minor
    units) and NEVER in the company SaaS ledger (company_finance_ledger's sources); refunds
    are compensating entries, never edits; a released deposit cannot be refunded (named 409).
  - AC-2: an expired pay window / held-money window is swept (failed booking recovery) - the
    deposit is released, its still-confirmed booking is cancelled, the reservation is released
    (the slot books again) and transitions are recorded; a dispute path exists; a failed
    payment stays retryable.
  - Idempotency: one create key yields one intent and one pay token yields one hold entry.

The orchestrator wires register_deposit_routes into the app factory, so the tests build a
client the same way test_clients._client does and add the deposit routes themselves - they
never touch identity/main.py.
"""

import json
import os
import uuid
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from test_clients import _salon, _client, _teardown
from test_booking_holds import _bookable, _first_slot
from test_booking_manage import _confirm

from modules.identity.main import (
    create_app,
    require_identity,
    _membership_scope,
    _require_permission,
    _insert_audit,
)
from modules.booking.deposits import register_deposit_routes


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _salon_client(database_url: str, owner) -> TestClient:
    """A client with every wired route PLUS the deposit routes, acting as `owner`.

    Mirrors test_clients._client, then registers the deposit routes the orchestrator wires
    in main.py, so the deposit endpoints exist without editing main.py.
    """
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    register_deposit_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )
    app.dependency_overrides[require_identity] = lambda: owner
    return TestClient(app)


def _token_from_url(pay_url: str) -> str:
    assert "token=" in pay_url, pay_url
    return pay_url.split("token=", 1)[1]


def _deposit_hold_window_past(engine, tenant_id, deposit_id):
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        connection.execute(
            text("UPDATE deposits SET hold_expires_at = now() - interval '1 minute' "
                 "WHERE tenant_id=:t AND id=:id"),
            {"t": tenant_id, "id": deposit_id})


def test_paid_deposit_lands_in_salon_ledger_not_company_ledger(database_url):
    """AC-1: a paid deposit writes a 'hold' entry in the salon ledger and zero SaaS rows."""
    issuer = "https://identity.example.invalid/dep-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _salon_client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        appointment_id, _ = _confirm(client, slug, service, staff, client_id, day)

        created = client.post(f"{base}/appointments/{appointment_id}/deposits",
                              json={"amount_minor": 5000, "idempotency_key": "d-" + uuid.uuid4().hex})
        assert created.status_code == 201, created.text
        deposit_id = created.json()["deposit_id"]
        pay_url = created.json()["pay_url"]
        assert f"/v1/public/salons/{slug}/deposits/{deposit_id}/pay?token=" in pay_url

        paid = client.post(f"/v1/public/salons/{slug}/deposits/{deposit_id}/pay",
                           json={"pay_token": _token_from_url(pay_url), "simulation": "success"})
        assert paid.status_code == 200, paid.text
        assert paid.json()["state"] == "held"

        # The salon's own deposit ledger reflects the hold, and the deposit carries one entry.
        ledger = client.get(f"{base}/deposits")
        assert ledger.status_code == 200, ledger.text
        assert ledger.json()["balance_minor"] == 5000
        detail = client.get(f"{base}/deposits/{deposit_id}")
        assert detail.status_code == 200, detail.text
        entries = detail.json()["entries"]
        assert [e["kind"] for e in entries] == ["hold"]
        assert entries[0]["amount_minor"] == 5000

        # The company SaaS ledger contains NO rows for this tenant: deposit money never mixes
        # into subscription revenue. company_finance_ledger reads billing_events /
        # finance_adjustments, which deposits never touch.
        with engine.begin() as connection:
            company = connection.execute(
                text("SELECT company_finance_ledger(now() - interval '365 days')")).scalar_one()
        if isinstance(company, str):
            company = json.loads(company)
        company_rows = [r for r in (company or [])
                        if str(r.get("tenant_id")) == tenant["tenant_id"]]
        assert company_rows == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_refund_held_deposit_compensates_and_released_deposit_is_409(database_url):
    """AC-1: a refund is a compensating entry (never an edit); a released deposit 409s."""
    issuer = "https://identity.example.invalid/dep-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _salon_client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        appointment_id, _ = _confirm(client, slug, service, staff, client_id, day)
        created = client.post(f"{base}/appointments/{appointment_id}/deposits",
                              json={"amount_minor": 5000, "idempotency_key": "d-" + uuid.uuid4().hex}).json()
        deposit_id = created["deposit_id"]
        client.post(f"/v1/public/salons/{slug}/deposits/{deposit_id}/pay",
                    json={"pay_token": _token_from_url(created["pay_url"]), "simulation": "success"})

        refund = client.post(f"{base}/deposits/{deposit_id}/refund",
                             json={"idempotency_key": "r-" + uuid.uuid4().hex})
        assert refund.status_code == 200, refund.text
        assert refund.json()["state"] == "refunded"
        detail = client.get(f"{base}/deposits/{deposit_id}").json()
        hold = next(e for e in detail["entries"] if e["kind"] == "hold")
        refund_entry = next(e for e in detail["entries"] if e["kind"] == "refund")
        # Compensating: opposite sign, a distinct row, and the original hold is unchanged.
        assert hold["amount_minor"] == 5000 and refund_entry["amount_minor"] == -5000
        assert hold["id"] != refund_entry["id"]
        assert detail["ledger_balance_minor"] == 0

        # A released (never-paid) deposit has nothing to give back: a named 409.
        created2 = client.post(f"{base}/appointments/{appointment_id}/deposits",
                               json={"amount_minor": 3000, "idempotency_key": "d-" + uuid.uuid4().hex}).json()
        dep2 = created2["deposit_id"]
        client.post(f"/v1/public/salons/{slug}/deposits/{dep2}/pay",
                    json={"pay_token": _token_from_url(created2["pay_url"]), "simulation": "fail"})
        bad = client.post(f"{base}/deposits/{dep2}/refund",
                          json={"idempotency_key": "r-" + uuid.uuid4().hex})
        assert bad.status_code == 409, bad.text
        assert bad.json()["detail"]["code"] == "deposit_not_refundable"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_partial_refunds_never_exceed_the_held_balance(database_url):
    """The signed refund ledger leaves exactly the unrefunded amount available."""
    issuer = "https://identity.example.invalid/dep-partial-refund"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _salon_client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        appointment_id, _ = _confirm(client, slug, service, staff, client_id, day)
        created = client.post(f"{base}/appointments/{appointment_id}/deposits",
                              json={"amount_minor": 5000,
                                    "idempotency_key": "d-" + uuid.uuid4().hex}).json()
        deposit_id = created["deposit_id"]
        paid = client.post(f"/v1/public/salons/{slug}/deposits/{deposit_id}/pay",
                           json={"pay_token": _token_from_url(created["pay_url"]),
                                 "simulation": "success"})
        assert paid.status_code == 200, paid.text

        partial = client.post(f"{base}/deposits/{deposit_id}/refund",
                              json={"amount_minor": 2000,
                                    "idempotency_key": "r-" + uuid.uuid4().hex})
        assert partial.status_code == 200, partial.text
        assert partial.json()["refunded_minor"] == 2000
        assert partial.json()["state"] == "held"

        final = client.post(f"{base}/deposits/{deposit_id}/refund",
                            json={"idempotency_key": "r-" + uuid.uuid4().hex})
        assert final.status_code == 200, final.text
        assert final.json()["refunded_minor"] == 3000
        assert final.json()["state"] == "refunded"
        detail = client.get(f"{base}/deposits/{deposit_id}")
        assert detail.status_code == 200, detail.text
        entries = detail.json()["entries"]
        assert sorted(e["amount_minor"] for e in entries if e["kind"] == "refund") == [-3000, -2000]
        assert detail.json()["ledger_balance_minor"] == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_expired_hold_sweep_releases_deposit_and_cancels_the_booking(database_url):
    """AC-2: an unpaid deposit past its window is swept; the booking is cancelled, the slot freed."""
    issuer = "https://identity.example.invalid/dep-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _salon_client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        appointment_id, _ = _confirm(client, slug, service, staff, client_id, day)
        created = client.post(f"{base}/appointments/{appointment_id}/deposits",
                              json={"amount_minor": 4000, "idempotency_key": "d-" + uuid.uuid4().hex,
                                    "hold_minutes": 60}).json()
        deposit_id = created["deposit_id"]

        # Unpaid: wind the pay window into the past, then recover via the sweep.
        _deposit_hold_window_past(engine, tenant["tenant_id"], deposit_id)
        swept = client.post(f"{base}/deposits/sweep")
        assert swept.status_code == 200, swept.text
        assert swept.json()["released"] == 1

        assert client.get(f"{base}/deposits/{deposit_id}").json()["state"] == "released"
        appt = client.get(f"{base}/appointments", params={
            "date_from": day.isoformat(), "date_to": day.isoformat()}).json()
        assert next(i for i in appt["items"] if i["id"] == appointment_id)["status"] == "cancelled"

        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            active = connection.execute(
                text("SELECT count(*) FROM reservations WHERE tenant_id=:t AND appointment_id=:a AND state='active'"),
                {"t": tenant["tenant_id"], "a": appointment_id}).scalar_one()
            cancelled = connection.execute(
                text("SELECT count(*) FROM appointment_transitions "
                     "WHERE tenant_id=:t AND appointment_id=:a AND to_status='cancelled'"),
                {"t": tenant["tenant_id"], "a": appointment_id}).scalar_one()
            # A never-paid release records no money entry - there was no hold to compensate.
            money = connection.execute(
                text("SELECT count(*) FROM deposit_ledger_entries WHERE tenant_id=:t AND deposit_id=:d"),
                {"t": tenant["tenant_id"], "d": deposit_id}).scalar_one()
        assert active == 0, "reservation should be released so the slot books again"
        assert cancelled >= 1
        assert money == 0
        slots = client.get(f"/v1/public/salons/{slug}/availability", params={
            "service_id": service, "date_from": day.isoformat(), "date_to": day.isoformat()}).json()["slots"]
        assert slots, "the freed slot should be offered again"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_disputed_payment_records_state_and_ledger_entry(database_url):
    """AC-2: paying with simulation=dispute yields state 'disputed' and a dispute entry."""
    issuer = "https://identity.example.invalid/dep-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _salon_client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        appointment_id, _ = _confirm(client, slug, service, staff, client_id, day)
        created = client.post(f"{base}/appointments/{appointment_id}/deposits",
                              json={"amount_minor": 4500, "idempotency_key": "d-" + uuid.uuid4().hex}).json()
        deposit_id = created["deposit_id"]

        disputed = client.post(f"/v1/public/salons/{slug}/deposits/{deposit_id}/pay",
                               json={"pay_token": _token_from_url(created["pay_url"]),
                                     "simulation": "dispute"})
        assert disputed.status_code == 200, disputed.text
        assert disputed.json()["state"] == "disputed"
        detail = client.get(f"{base}/deposits/{deposit_id}").json()
        assert detail["state"] == "disputed"
        assert "dispute" in [e["kind"] for e in detail["entries"]]
        # A capture then the contesting dispute net out of clean float.
        assert detail["ledger_balance_minor"] == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_create_and_pay_are_idempotent(database_url):
    """One create key yields one intent; one pay token yields a single hold entry."""
    issuer = "https://identity.example.invalid/dep-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _salon_client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        appointment_id, _ = _confirm(client, slug, service, staff, client_id, day)

        key = "d-" + uuid.uuid4().hex
        first = client.post(f"{base}/appointments/{appointment_id}/deposits",
                            json={"amount_minor": 5000, "idempotency_key": key})
        assert first.status_code == 201 and first.json()["replayed"] is False
        replay = client.post(f"{base}/appointments/{appointment_id}/deposits",
                             json={"amount_minor": 5000, "idempotency_key": key})
        assert replay.status_code == 201 and replay.json()["replayed"] is True
        deposit_id = first.json()["deposit_id"]
        assert replay.json()["deposit_id"] == deposit_id
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            count = connection.execute(
                text("SELECT count(*) FROM deposits WHERE tenant_id=:t AND idempotency_key=:k"),
                {"t": tenant["tenant_id"], "k": key}).scalar_one()
        assert count == 1

        # Paying twice with the replay's current token replays: one hold entry only.
        token = _token_from_url(replay.json()["pay_url"])
        paid = client.post(f"/v1/public/salons/{slug}/deposits/{deposit_id}/pay",
                           json={"pay_token": token, "simulation": "success"})
        assert paid.status_code == 200 and paid.json()["replayed"] is False
        paid2 = client.post(f"/v1/public/salons/{slug}/deposits/{deposit_id}/pay",
                            json={"pay_token": token, "simulation": "success"})
        assert paid2.status_code == 200 and paid2.json()["replayed"] is True
        detail = client.get(f"{base}/deposits/{deposit_id}").json()
        assert [e["kind"] for e in detail["entries"]].count("hold") == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_failed_payment_is_released_and_stays_retryable(database_url):
    """AC-2: a failed payment releases the deposit without a money entry, then pays on retry."""
    issuer = "https://identity.example.invalid/dep-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _salon_client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        appointment_id, _ = _confirm(client, slug, service, staff, client_id, day)
        created = client.post(f"{base}/appointments/{appointment_id}/deposits",
                              json={"amount_minor": 5000, "idempotency_key": "d-" + uuid.uuid4().hex}).json()
        deposit_id = created["deposit_id"]
        token = _token_from_url(created["pay_url"])

        failed = client.post(f"/v1/public/salons/{slug}/deposits/{deposit_id}/pay",
                             json={"pay_token": token, "simulation": "fail"})
        assert failed.status_code == 200, failed.text
        assert failed.json()["state"] == "released" and failed.json()["retryable"] is True
        # No money moved on a failed payment.
        assert client.get(f"{base}/deposits/{deposit_id}").json()["entries"] == []

        # Retry with the same token now succeeds, holding the deposit.
        retried = client.post(f"/v1/public/salons/{slug}/deposits/{deposit_id}/pay",
                              json={"pay_token": token, "simulation": "success"})
        assert retried.status_code == 200, retried.text
        assert retried.json()["state"] == "held"
        detail = client.get(f"{base}/deposits/{deposit_id}").json()
        assert [e["kind"] for e in detail["entries"]] == ["hold"]
        assert detail["ledger_balance_minor"] == 5000
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_held_deposit_expiry_writes_a_compensating_release_and_cancels(database_url):
    """AC-1/AC-2: a held deposit past its money window releases with a compensating entry."""
    issuer = "https://identity.example.invalid/dep-7"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _salon_client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        appointment_id, _ = _confirm(client, slug, service, staff, client_id, day)
        created = client.post(f"{base}/appointments/{appointment_id}/deposits",
                              json={"amount_minor": 5000, "idempotency_key": "d-" + uuid.uuid4().hex}).json()
        dep = created["deposit_id"]
        client.post(f"/v1/public/salons/{slug}/deposits/{dep}/pay",
                    json={"pay_token": _token_from_url(created["pay_url"]), "simulation": "success"})

        # Wind the held-money window into the past and recover via the sweep.
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            connection.execute(
                text("UPDATE deposits SET held_expires_at = now() - interval '1 minute' "
                     "WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": dep})
        swept = client.post(f"{base}/deposits/sweep")
        assert swept.status_code == 200 and swept.json()["released"] == 1

        detail = client.get(f"{base}/deposits/{dep}").json()
        assert detail["state"] == "released"
        assert [e["kind"] for e in detail["entries"]] == ["hold", "release"]
        assert detail["ledger_balance_minor"] == 0
        appt = client.get(f"{base}/appointments", params={
            "date_from": day.isoformat(), "date_to": day.isoformat()}).json()
        assert next(i for i in appt["items"] if i["id"] == appointment_id)["status"] == "cancelled"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
