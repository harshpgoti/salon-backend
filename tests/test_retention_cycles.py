"""Tests for versioned rebooking cycles (TASK-035, FR-049).

AC-1: Completed service interval/override yields deterministic due date.
AC-2: Cancelled/no-show and corrected/superseded completion create no stale cycle.
"""

import os
import sys
import uuid
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_clients import _salon, _client, _teardown


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _setup(client, tenant, interval_days=42):
    """Hours + service (with interval) + staff + client; return ids."""
    base = f"/v1/tenants/{tenant['tenant_id']}"
    loc = tenant["location_id"]
    day = date.today() + timedelta(days=1)
    client.put(f"{base}/locations/{loc}/hours", json={
        "intervals": [{"weekday": day.weekday(), "opens_at": "09:00", "closes_at": "17:00"}]})
    service = client.post(f"{base}/services", json={
        "name": "Cut", "duration_minutes": 60, "price_minor": 6000,
        "rebooking_interval_days": interval_days}).json()["id"]
    staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
    client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [loc]})
    client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
    client.post(f"{base}/staff/{staff}/shifts", json={
        "location_id": loc, "weekday": day.weekday(),
        "starts_at": "09:00", "ends_at": "17:00"})
    client_id = client.post(f"{base}/clients", json={"name": "Regular"}).json()["id"]
    return base, service, staff, client_id, day


def _book(client, base, service, staff, client_id, day, hour=10, key=None):
    starts = (datetime.combine(day, datetime.min.time())
              + timedelta(hours=hour)).replace(tzinfo=timezone.utc)
    created = client.post(f"{base}/appointments", json={
        "client_id": client_id, "staff_id": staff, "service_id": service,
        "starts_at": starts.isoformat(), "source": "staff",
        "idempotency_key": key or ("ret-" + uuid.uuid4().hex)})
    assert created.status_code == 201, created.text
    return created.json()["appointment_id"]


def _move(client, base, appt_id, to_status, expected_version, reason="test"):
    moved = client.post(f"{base}/appointments/{appt_id}/transitions", json={
        "to_status": to_status, "expected_version": expected_version,
        "idempotency_key": "ret-t-" + uuid.uuid4().hex, "reason": reason})
    assert moved.status_code == 200, moved.text
    return moved.json()


def _complete(client, base, appt_id):
    _move(client, base, appt_id, "checked_in", 1)
    _move(client, base, appt_id, "in_service", 2)
    return _move(client, base, appt_id, "completed", 3)


def _cycles(engine, tenant_id, status="due"):
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                     {"t": tenant_id})
        return conn.execute(
            text("""SELECT client_id::text AS client_id, service_id::text AS service_id,
                           interval_days, due_date, status FROM rebooking_cycles
                    WHERE tenant_id=:t AND status=:s ORDER BY due_date"""),
            {"t": tenant_id, "s": status}).mappings().all()


def test_retention_ac1_service_interval_sets_due_date(database_url):
    """AC-1: a 42-day service interval yields completion date + 42."""
    issuer = "https://identity.example.invalid/ret-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, service, staff, client_id, day = _setup(client, tenant, interval_days=42)
        appt_id = _book(client, base, service, staff, client_id, day)
        done = _complete(client, base, appt_id)
        assert done["visit_id"]
        rows = _cycles(engine, tenant["tenant_id"])
        assert len(rows) == 1
        assert rows[0]["interval_days"] == 42
        assert str(rows[0]["due_date"]) == (date.today() + timedelta(days=42)).isoformat()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_retention_ac1_client_override_wins(database_url):
    """AC-1: a per-client override beats the service default."""
    issuer = "https://identity.example.invalid/ret-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, service, staff, client_id, day = _setup(client, tenant, interval_days=42)
        saved = client.post(f"/v1/tenants/{tenant['tenant_id']}/rebooking-overrides", json={
            "client_id": client_id, "service_id": service, "interval_days": 14})
        assert saved.status_code == 201, saved.text
        appt_id = _book(client, base, service, staff, client_id, day)
        _complete(client, base, appt_id)
        rows = _cycles(engine, tenant["tenant_id"])
        assert len(rows) == 1
        assert rows[0]["interval_days"] == 14
        assert str(rows[0]["due_date"]) == (date.today() + timedelta(days=14)).isoformat()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_retention_ac1_no_interval_no_cycle(database_url):
    """AC-1: a service with no interval anywhere creates no cycle."""
    issuer = "https://identity.example.invalid/ret-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base = f"/v1/tenants/{tenant['tenant_id']}"
        loc = tenant["location_id"]
        day = date.today() + timedelta(days=1)
        client.put(f"{base}/locations/{loc}/hours", json={
            "intervals": [{"weekday": day.weekday(), "opens_at": "09:00", "closes_at": "17:00"}]})
        service = client.post(f"{base}/services", json={
            "name": "Consult", "duration_minutes": 30, "price_minor": 0}).json()["id"]
        staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
        client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [loc]})
        client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
        client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": loc, "weekday": day.weekday(),
            "starts_at": "09:00", "ends_at": "17:00"})
        client_id = client.post(f"{base}/clients", json={"name": "Regular"}).json()["id"]
        appt_id = _book(client, base, service, staff, client_id, day)
        _complete(client, base, appt_id)
        assert _cycles(engine, tenant["tenant_id"]) == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_retention_ac2_cancel_and_no_show_create_nothing(database_url):
    """AC-2: cancelled and no-show appointments leave no cycle."""
    issuer = "https://identity.example.invalid/ret-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, service, staff, client_id, day = _setup(client, tenant, interval_days=42)
        cancelled = _book(client, base, service, staff, client_id, day, hour=10)
        _move(client, base, cancelled, "cancelled", 1, reason="client request")
        skipped = _book(client, base, service, staff, client_id, day, hour=14)
        _move(client, base, skipped, "no_show", 1)
        assert _cycles(engine, tenant["tenant_id"]) == []
        assert _cycles(engine, tenant["tenant_id"], status="superseded") == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_retention_ac2_newer_completion_supersedes(database_url):
    """AC-2: a second completion replaces the older due row; one stays due."""
    issuer = "https://identity.example.invalid/ret-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, service, staff, client_id, day = _setup(client, tenant, interval_days=42)
        first = _book(client, base, service, staff, client_id, day, hour=10)
        _complete(client, base, first)
        second = _book(client, base, service, staff, client_id, day, hour=14)
        _complete(client, base, second)
        due = _cycles(engine, tenant["tenant_id"], status="due")
        old = _cycles(engine, tenant["tenant_id"], status="superseded")
        assert len(due) == 1 and len(old) == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_retention_ac1_rule_edit_keeps_history(database_url):
    """AC-1: changing the service interval never rewrites an existing cycle."""
    issuer = "https://identity.example.invalid/ret-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, service, staff, client_id, day = _setup(client, tenant, interval_days=42)
        first = _book(client, base, service, staff, client_id, day, hour=10)
        _complete(client, base, first)
        edited = client.patch(f"{base}/services/{service}", json={
            "rebooking_interval_days": 60, "expected_version": 1})
        assert edited.status_code == 200, edited.text
        assert edited.json()["version"] == 2
        second = _book(client, base, service, staff, client_id, day, hour=14)
        _complete(client, base, second)
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            intervals = conn.execute(
                text("""SELECT interval_days FROM rebooking_cycles
                        WHERE tenant_id=:t ORDER BY created_at"""),
                {"t": tenant["tenant_id"]}).scalars().all()
        assert intervals == [42, 60]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_retention_cycle_list_route(database_url):
    """AC-1: due cycles list over HTTP with client and service names."""
    issuer = "https://identity.example.invalid/ret-7"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, service, staff, client_id, day = _setup(client, tenant, interval_days=42)
        appt_id = _book(client, base, service, staff, client_id, day)
        _complete(client, base, appt_id)
        listed = client.get(f"/v1/tenants/{tenant['tenant_id']}/rebooking-cycles")
        assert listed.status_code == 200, listed.text
        items = listed.json()["items"]
        assert len(items) == 1
        assert items[0]["client_name"] == "Regular"
        assert items[0]["service_name"] == "Cut"
        assert items[0]["status"] == "due"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
