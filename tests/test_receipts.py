"""Tests for manual salon receipt ledger (TASK-025, FR-036).

AC-1: Manual payment/refund entries are idempotent CAD minor-unit facts.
AC-2: Recorded receipts are labelled separately from booked value and SaaS revenue.
"""

import os
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text

from test_clients import _salon, _client, _teardown
from test_booking_holds import _bookable


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _created(client, base, service, staff, client_id, starts_at, source="staff"):
    """Helper to create a confirmed appointment; returns the response."""
    return client.post(
        f"{base}/appointments",
        json={
            "client_id": client_id,
            "staff_id": staff,
            "service_id": service,
            "starts_at": starts_at,
            "source": source,
            "idempotency_key": "a-" + uuid.uuid4().hex,
        }
    )


def _transition(client, base, appt_id, to_status, expected_version=None, reason=None):
    """Helper to create an appointment transition."""
    body = {
        "to_status": to_status,
        "expected_version": expected_version,
        "idempotency_key": "t-" + uuid.uuid4().hex,
        "reason": reason,
    }
    return client.post(f"{base}/appointments/{appt_id}/transitions", json=body)


def _appointment(client, base, service, staff, client_id):
    day = date.today() + timedelta(days=1)
    starts = f"{day.isoformat()}T14:00:00+00:00"
    created = _created(client, base, service, staff, client_id, starts, source="staff")
    assert created.status_code == 201, created.text
    return created.json()["appointment_id"], day


def test_post_receipt_creates_entry(database_url):
    """AC-1: posting a receipt creates an append-only entry."""
    issuer = "https://identity.example.invalid/receipt-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        appt_id, day = _appointment(client, base, service, staff, client_id)

        # Verify the appointment was created
        listing = client.get(f"{base}/appointments", params={
            "date_from": day.isoformat(), "date_to": day.isoformat()
        })
        assert listing.status_code == 200
        item = next(i for i in listing.json()["items"] if i["id"] == appt_id)
        assert item["status"] == "confirmed"

        # Create a receipt entry
        receipt_id = client.post(
            f"{base}/appointments/{appt_id}/receipt-entries",
            json={
                "kind": "payment",
                "amount_minor": 500,
                "method": "cash",
                "idempotency_key": "r-" + uuid.uuid4().hex,
            }
        ).json()["entry_id"]

        # Verify the receipt was recorded
        total = client.get(
            f"{base}/appointments/{appt_id}/receipt-total",
            params={"tenant_id": tenant["tenant_id"]},
        ).json()["recorded_total_minor"]
        assert total == 500
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_list_receipts_returns_items(database_url):
    """AC-2: listing receipt entries returns the correct items."""
    issuer = "https://identity.example.invalid/receipt-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        appt_id, _ = _appointment(client, base, service, staff, client_id)

        for amount, method in ((500, "cash"), (250, "card")):
            created = client.post(
                f"{base}/appointments/{appt_id}/receipt-entries",
                json={"kind": "payment", "amount_minor": amount,
                      "method": method, "idempotency_key": "r-" + uuid.uuid4().hex})
            assert created.status_code == 201, created.text

        # List receipt entries
        resp = client.get(
            f"{base}/appointments/{appt_id}/receipt-entries",
            params={"limit": 100, "offset": 0},
        )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 2
        assert sorted(i["amount_minor"] for i in items) == [250, 500]
        assert {i["method"] for i in items} == {"cash", "card"}
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_receipt_total_returns_recorded_amount(database_url):
    """AC-1: getting the receipt total returns the recorded amount."""
    issuer = "https://identity.example.invalid/receipt-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        appt_id, _ = _appointment(client, base, service, staff, client_id)

        # Create a receipt
        receipt_id = client.post(
            f"{base}/appointments/{appt_id}/receipt-entries",
            json={
                "kind": "payment",
                "amount_minor": 750,
                "method": "card",
                "idempotency_key": "c-" + uuid.uuid4().hex,
            }
        ).json()["entry_id"]

        # Get the total
        total = client.get(
            f"{base}/appointments/{appt_id}/receipt-total",
            params={"tenant_id": tenant["tenant_id"]},
        ).json()["recorded_total_minor"]
        assert total == 750
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
