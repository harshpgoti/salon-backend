"""Operational report source-fact reconciliation (TASK-042, FR-059/FR-062)."""

import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_calendar import _book, _next_weekday_slot
from test_booking_holds import _bookable
from test_clients import _client, _salon, _teardown


@pytest.fixture
def database_url():
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def test_operational_report_reconciles_dates_capacity_and_receipts(database_url):
    issuer = "https://identity.example.invalid/operational-report/" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        first = _next_weekday_slot()
        second = first + timedelta(days=7)
        report_start = min(date.today(), first - timedelta(days=1))
        base, _slug, service, staff, client_id = _bookable(
            client, database_url, tenant, first)
        hours = client.put(f"{base}/locations/{tenant['location_id']}/hours", json={
            "intervals": [{"weekday": weekday, "opens_at": "09:00",
                           "closes_at": "23:59" if weekday == first.weekday() else "17:00"}
                          for weekday in range(7)]})
        assert hours.status_code == 200, hours.text
        first_appt = _book(client, base, service, staff, client_id,
                           f"{first.isoformat()}T14:00:00+00:00")
        second_appt = _book(client, base, service, staff, client_id,
                            f"{second.isoformat()}T14:00:00+00:00")
        assert first_appt.status_code == 201, first_appt.text
        assert second_appt.status_code == 201, second_appt.text
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            conn.execute(text("""UPDATE staff_shifts SET breaks='[{\"start\":\"12:00\",\"end\":\"12:30\"}]'::jsonb,
                effective_from=:first,effective_to=:last
                WHERE tenant_id=:t AND staff_id=:staff"""),
                         {"first": first.isoformat(), "last": second.isoformat(),
                          "t": tenant["tenant_id"], "staff": staff})

        for appointment_id, kind, amount in (
            (first_appt.json()["appointment_id"], "payment", 10000),
            (first_appt.json()["appointment_id"], "refund", -2500),
            (second_appt.json()["appointment_id"], "adjustment", -500),
        ):
            recorded = client.post(f"{base}/appointments/{appointment_id}/receipt-entries", json={
                "kind": kind, "amount_minor": amount, "idempotency_key": "report-" + uuid.uuid4().hex})
            assert recorded.status_code == 201, recorded.text

        # An approval request is not a booked appointment and must not inflate forecast.
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            conn.execute(text("UPDATE appointments SET status='pending_approval' WHERE tenant_id=:t AND id=:a"),
                         {"t": tenant["tenant_id"], "a": second_appt.json()["appointment_id"]})

        report = client.get(f"{base}/reports/operational", params={
            "date_from": report_start.isoformat(),
            "date_to": (second + timedelta(days=1)).isoformat(),
            "location_id": tenant["location_id"], "staff_id": staff, "service_id": service})
        assert report.status_code == 200, report.text
        assert report.json()["forecast_booked_value_minor"] == 6000
        assert len(report.json()["service_mix"]) == 1
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            conn.execute(text("UPDATE appointments SET status='confirmed' WHERE tenant_id=:t AND id=:a"),
                         {"t": tenant["tenant_id"], "a": second_appt.json()["appointment_id"]})
        report = client.get(f"{base}/reports/operational", params={
            "date_from": report_start.isoformat(),
            "date_to": (second + timedelta(days=1)).isoformat(),
            "location_id": tenant["location_id"], "staff_id": staff, "service_id": service})
        assert report.status_code == 200, report.text
        body = report.json()
        assert body["bookings"]["total"] == 2
        assert body["service_mix"] == [{"service_id": service, "service": "Cut",
                                         "bookings": 2, "booked_minor": 12000}]
        assert body["staff_utilization"] == [{
            "staff_id": staff, "staff_name": "Ava", "booked_minutes": 120, "scheduled_minutes": 900,
            "utilization": 0.133}]
        assert body["recorded_receipts"] == {
            "by_kind": {"payment": {"entries": 1, "minor": 10000},
                        "refund": {"entries": 1, "minor": -2500},
                        "adjustment": {"entries": 1, "minor": -500}},
            "gross_minor": 10000, "refunds_minor": 2500,
            "adjustments_minor": -500, "net_minor": 7000}
        assert body["timezone"] == "appointment local_timezone snapshot"
        assert body["location_timezone"] == "America/Toronto"
        assert body["definitions"]["utilization"]
        assert body["generated_at"] and body["filters"]["location_id"] == tenant["location_id"]

        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            conn.execute(text("UPDATE appointments SET status=CASE WHEN id=:a THEN 'no_show' ELSE 'cancelled' END WHERE tenant_id=:t AND id IN (:a,:b)"),
                         {"t": tenant["tenant_id"], "a": first_appt.json()["appointment_id"],
                          "b": second_appt.json()["appointment_id"]})
        closed_report = client.get(f"{base}/reports/operational", params={
            "date_from": report_start.isoformat(),
            "date_to": (second + timedelta(days=1)).isoformat(),
            "location_id": tenant["location_id"]})
        assert closed_report.status_code == 200, closed_report.text
        assert closed_report.json()["rates"] == {
            "completion_rate": {"numerator": 0, "denominator": 2, "value": 0.0},
            "cancellation_rate": {"numerator": 1, "denominator": 2, "value": 0.5},
            "no_show_rate": {"numerator": 1, "denominator": 1, "value": 1.0}}
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            conn.execute(text("UPDATE appointments SET status='confirmed' WHERE tenant_id=:t AND id IN (:a,:b)"),
                         {"t": tenant["tenant_id"], "a": first_appt.json()["appointment_id"],
                          "b": second_appt.json()["appointment_id"]})

        # Available capacity loses approved leave and a published full-day closure.
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            conn.execute(text("""INSERT INTO staff_leave
                (tenant_id,staff_id,starts_on,ends_on,approved)
                VALUES (:t,:staff,:day,:day,true)"""),
                         {"t": tenant["tenant_id"], "staff": staff, "day": first.isoformat()})
            conn.execute(text("""INSERT INTO location_exceptions
                (tenant_id,location_id,exception_date,kind,published_at)
                VALUES (:t,:location,:day,'closed',now())"""),
                         {"t": tenant["tenant_id"], "location": tenant["location_id"],
                          "day": second.isoformat()})
        revised = client.get(f"{base}/reports/operational", params={
            "date_from": report_start.isoformat(),
            "date_to": (second + timedelta(days=1)).isoformat(),
            "location_id": tenant["location_id"], "staff_id": staff, "service_id": service})
        assert revised.status_code == 200, revised.text
        assert revised.json()["staff_utilization"][0]["scheduled_minutes"] == 0
        assert revised.json()["staff_utilization"][0]["utilization"] is None
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_operational_report_uses_appointment_timezone_snapshot(database_url):
    issuer = "https://identity.example.invalid/report-local-day/" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        day = _next_weekday_slot()
        base, _slug, service, staff, client_id = _bookable(
            client, database_url, tenant, day)
        hours = client.put(f"{base}/locations/{tenant['location_id']}/hours", json={
            "intervals": [{"weekday": weekday, "opens_at": "09:00",
                           "closes_at": "23:59" if weekday == day.weekday() else "17:00"}
                          for weekday in range(7)]})
        assert hours.status_code == 200, hours.text
        extra_shift = client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": tenant["location_id"], "weekday": day.weekday(),
            "starts_at": "17:00", "ends_at": "23:59"})
        assert extra_shift.status_code == 201, extra_shift.text
        # 00:30 next UTC date is still 20:30 on the previous Toronto date.
        created = _book(client, base, service, staff, client_id,
                        f"{(day + timedelta(days=1)).isoformat()}T00:30:00+00:00")
        assert created.status_code == 201, created.text
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            conn.execute(text("UPDATE locations SET timezone='Pacific/Kiritimati' WHERE tenant_id=:t AND id=:l"),
                         {"t": tenant["tenant_id"], "l": tenant["location_id"]})
        response = client.get(f"{base}/reports/operational", params={
            "date_from": day.isoformat(), "date_to": day.isoformat(),
            "location_id": tenant["location_id"]})
        assert response.status_code == 200, response.text
        assert response.json()["bookings"]["total"] == 1
        assert response.json()["timezone"] == "appointment local_timezone snapshot"
        assert response.json()["location_timezone"] == "Pacific/Kiritimati"
        assert "saved local_timezone snapshot" in response.json()["definitions"]["basis"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_operational_report_manager_is_confined_to_assigned_locations(database_url):
    from modules.identity.main import AuthenticatedIdentity

    issuer = "https://identity.example.invalid/report-scope/" + uuid.uuid4().hex
    engine, owner, tenant = _salon(database_url, issuer)
    identity = AuthenticatedIdentity(issuer, uuid.uuid4().hex)
    try:
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            user_id = conn.execute(text("""INSERT INTO identity_users (issuer,subject)
                VALUES (:issuer,:subject) RETURNING id"""),
                {"issuer": identity.issuer, "subject": identity.subject}).scalar_one()
            conn.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:t,:u,'manager')"),
                         {"t": tenant["tenant_id"], "u": str(user_id)})
        manager = _client(database_url, identity)
        base = f"/v1/tenants/{tenant['tenant_id']}"
        params = {"date_from": date.today().isoformat(), "date_to": date.today().isoformat()}
        empty = manager.get(f"{base}/reports/operational", params=params)
        assert empty.status_code == 200, empty.text
        assert empty.json()["bookings"]["total"] == 0
        denied = manager.get(f"{base}/reports/operational", params={
            **params, "location_id": tenant["location_id"]})
        assert denied.status_code == 403
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
