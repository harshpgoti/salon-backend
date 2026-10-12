"""Tests for manual salon receipt ledger (FR-036).
Builds on TASK-025 and extends the receipt_entries table with full CRUD.
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
    """Helper to create a confirmed appointment."""
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


def test_post_receipt_creates_entry(database_url):
    """AC-1: posting a receipt creates an append-only entry."""
    issuer = "https://identity.example.invalid/tr-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)

    base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
    day = date.today() + timedelta(days=1)
    starts = f"{day.isoformat()}T14:00:00+00:00"
    appt_id, _ = _created(client, base, service, staff, client_id, starts, source="staff")

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


def test_list_receipts_returns_items(database_url):
    """AC-2: listing receipt entries returns the correct items."""
    issuer = "https://identity.example.invalid/tr-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)

    base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
    day = date.today() + timedelta(days=1)
    starts = f"{day.isoformat()}T14:00:00+00:00"
    appt_id, _ = _created(client, base, service, staff, client_id, starts, source="staff")

    # List receipt entries
    resp = client.get(
        f"{base}/appointments/{appt_id}/receipt-entries",
        params={"limit": 100, "offset": 0},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) >= 1
    assert any(i["id"] == appt_id for i in items)


def test_receipt_total_returns_recorded_amount(database_url):
    """AC-3: getting the receipt total returns the recorded amount."""
    issuer = "https://identity.example.invalid/tr-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)

    base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
    day = date.today() + timedelta(days=1)
    starts = f"{day.isoformat()}T14:00:00+00:00"
    appt_id, _ = _created(client, base, service, staff, client_id, starts, source="staff")

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