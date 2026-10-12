"""Tests for owner operational and receipt reports (TASK-042, FR-059/FR-062).

AC-1: Bookings/no-show/utilization/service/forecast/recorded receipts follow dictionary.
AC-2: Timezone denominators/corrections/refunds and exports reconcile.
"""

import os
import sys
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


def _setup(client, tenant):
    base = f"/v1/tenants/{tenant['tenant_id']}"
    loc = tenant["location_id"]
    day = date.today() + timedelta(days=1)
    client.put(f"{base}/locations/{loc}/hours", json={
        "intervals": [{"weekday": day.weekday(), "opens_at": "09:00", "closes_at": "17:00"}]})
    cut = client.post(f"{base}/services", json={
        "name": "Cut", "duration_minutes": 60, "price_minor": 6000}).json()["id"]
    tint = client.post(f"{base}/services", json={
        "name": "Tint", "duration_minutes": 30, "price_minor": 3000}).json()["id"]
    staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
    client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [loc]})
    client.put(f"{base}/staff/{staff}/qualifications",
               json={"service_ids": [cut, tint]})
    client.post(f"{base}/staff/{staff}/shifts", json={
        "location_id": loc, "weekday": day.weekday(),
        "starts_at": "09:00", "ends_at": "17:00"})
    return base, loc, cut, tint, staff, day


def _book(client, base, service, staff, client_id, day, hour, key):
    starts = (datetime.combine(day, datetime.min.time())
              + timedelta(hours=hour)).replace(tzinfo=timezone.utc)
    created = client.post(f"{base}/appointments", json={
        "client_id": client_id, "staff_id": staff, "service_id": service,
        "starts_at": starts.isoformat(), "source": "staff",
        "idempotency_key": key})
    assert created.status_code == 201, created.text
    return created.json()["appointment_id"]


def _move(client, base, appt_id, to_status, version, reason="report"):
    moved = client.post(f"{base}/appointments/{appt_id}/transitions", json={
        "to_status": to_status, "expected_version": version,
        "idempotency_key": f"rep-key-t{appt_id[:8]}-{version}", "reason": reason})
    assert moved.status_code == 200, moved.text


def _receipt(client, base, appt_id, kind, amount, key):
    posted = client.post(f"{base}/appointments/{appt_id}/receipt-entries", json={
        "kind": kind, "amount_minor": amount, "method": "card",
        "idempotency_key": key})
    assert posted.status_code == 201, posted.text


def test_operational_ac1_dictionary_figures_reconcile(database_url):
    """AC-1: every section follows its dictionary definition."""
    issuer = "https://identity.example.invalid/rep-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, loc, cut, tint, staff, day = _setup(client, tenant)
        regular = client.post(f"{base}/clients", json={"name": "Regular"}).json()["id"]
        newcomer = client.post(f"{base}/clients", json={"name": "Newcomer"}).json()["id"]
        # Pre-range history makes Regular a repeat client without entering counts.
        _book(client, base, cut, staff, regular, day - timedelta(days=7), 10, "rep-key-0")
        first = _book(client, base, cut, staff, regular, day, 10, "rep-key-1")
        _move(client, base, first, "checked_in", 1)
        _move(client, base, first, "in_service", 2)
        _move(client, base, first, "completed", 3)
        second = _book(client, base, tint, staff, regular, day, 14, "rep-key-2")
        dropped = _book(client, base, cut, staff, newcomer, day, 11, "rep-key-3")
        _move(client, base, dropped, "cancelled", 1, reason="client request")
        skipped = _book(client, base, cut, staff, newcomer, day, 15, "rep-key-4")
        _move(client, base, skipped, "no_show", 1)
        _receipt(client, base, first, "payment", 6000, "rep-key-r1")
        _receipt(client, base, first, "refund", 1000, "rep-key-r2")
        _receipt(client, base, first, "adjustment", 500, "rep-key-r3")
        report = client.get(
            f"/v1/tenants/{tid}/reports/operational",
            params={"date_from": date.today().isoformat(), "date_to": day.isoformat()})
        assert report.status_code == 200, report.text
        body = report.json()
        assert body["timezone"] == "America/Toronto"
        assert body["generated_at"]
        assert body["bookings"]["by_state"] == {"completed": 1, "confirmed": 1,
                                                "cancelled": 1, "no_show": 1}
        assert body["bookings"]["total"] == 4
        assert body["rates"] == {"completion_rate": 0.25, "cancellation_rate": 0.25,
                                 "no_show_rate": 0.25}
        mix = {m["service"]: (m["bookings"], m["booked_minor"]) for m in body["service_mix"]}
        assert mix == {"Cut": (1, 6000), "Tint": (1, 3000)}
        assert body["clients"] == {"repeat_clients": 1, "new_clients": 1}
        # One future confirmed booking (Tint 3000) forecasts booked value.
        assert body["forecast_booked_value_minor"] == 3000
        receipts = body["recorded_receipts"]
        assert receipts["gross_minor"] == 6000
        assert receipts["refunds_minor"] == 1000
        assert receipts["adjustments_minor"] == 500
        assert receipts["net_minor"] == 5500
        assert receipts["by_kind"]["payment"]["entries"] == 1
        assert len(body["staff_utilization"]) == 1
        util = body["staff_utilization"][0]
        assert util["booked_minutes"] == 90 and util["scheduled_minutes"] == 480
        assert util["utilization"] == 0.188
        assert set(body["definitions"]) >= {"booking_counts", "completion_rate",
                                            "service_mix", "recorded_receipts", "basis"}
    finally:
        _teardown(engine, [tid], issuer)


def test_operational_ac2_filters_scope_and_empty(database_url):
    """AC-2: filters isolate; strangers bounce; empty ranges stay well-formed."""
    issuer = "https://identity.example.invalid/rep-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, loc, cut, tint, staff, day = _setup(client, tenant)
        regular = client.post(f"{base}/clients", json={"name": "Regular"}).json()["id"]
        _book(client, base, cut, staff, regular, day, 10, "rep-key-9")
        params = {"date_from": day.isoformat(), "date_to": day.isoformat()}
        full = client.get(f"/v1/tenants/{tid}/reports/operational",
                          params=params).json()
        assert full["bookings"]["total"] == 1
        scoped = client.get(f"/v1/tenants/{tid}/reports/operational",
                            params={**params, "location_id": loc}).json()
        assert scoped["bookings"]["total"] == 1
        assert scoped["filters"]["location_id"] == loc
        by_staff = client.get(f"/v1/tenants/{tid}/reports/operational",
                              params={**params, "staff_id": staff}).json()
        assert by_staff["bookings"]["total"] == 1
        by_service = client.get(f"/v1/tenants/{tid}/reports/operational",
                                params={**params, "service_id": tint}).json()
        assert by_service["bookings"]["total"] == 0
        bad = client.get(f"/v1/tenants/{tid}/reports/operational",
                         params={"date_from": day.isoformat(),
                                 "date_to": (day - timedelta(days=1)).isoformat()})
        assert bad.status_code == 422
        from test_clients import _identity
        outsider = _client(database_url, _identity())
        assert outsider.get(f"/v1/tenants/{tid}/reports/operational",
                            params=params).status_code == 404
        empty = client.get(f"/v1/tenants/{tid}/reports/operational", params={
            "date_from": (day + timedelta(days=30)).isoformat(),
            "date_to": (day + timedelta(days=31)).isoformat()}).json()
        assert empty["bookings"] == {"by_state": {}, "total": 0}
        assert empty["rates"] == {"completion_rate": None, "cancellation_rate": None,
                                  "no_show_rate": None}
        assert empty["recorded_receipts"]["net_minor"] == 0
        assert empty["generated_at"]
    finally:
        _teardown(engine, [tid], issuer)
