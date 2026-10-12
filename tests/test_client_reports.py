"""Tests for client cohorts, retention and data quality (TASK-043, FR-060/FR-062).

AC-1: First/last visits return intervals and due/reachable cohorts defined.
AC-2: Immature windows/deletion/import provenance and scopes handled.
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
    service = client.post(f"{base}/services", json={
        "name": "Cut", "duration_minutes": 60, "price_minor": 6000,
        "rebooking_interval_days": 42}).json()["id"]
    staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
    client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [loc]})
    client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
    client.post(f"{base}/staff/{staff}/shifts", json={
        "location_id": loc, "weekday": day.weekday(),
        "starts_at": "09:00", "ends_at": "17:00"})
    return base, loc, service, staff, day


def _client_with_email(client, base, name, email, loc=None):
    body = {"name": name,
            "contacts": [{"kind": "email", "value": email, "is_primary": True}]}
    if loc is not None:
        body["preferred_location_id"] = loc
    created = client.post(f"{base}/clients", json=body)
    assert created.status_code == 201, created.text
    return created.json()


def _complete(client, base, service, staff, client_id, day, hour, key):
    starts = (datetime.combine(day, datetime.min.time())
              + timedelta(hours=hour)).replace(tzinfo=timezone.utc)
    appt = client.post(f"{base}/appointments", json={
        "client_id": client_id, "staff_id": staff, "service_id": service,
        "starts_at": starts.isoformat(), "source": "staff",
        "idempotency_key": key}).json()["appointment_id"]
    version = 1
    for to_status in ("checked_in", "in_service", "completed"):
        moved = client.post(f"{base}/appointments/{appt}/transitions", json={
            "to_status": to_status, "expected_version": version,
            "idempotency_key": f"coh-t-{key}-{version}"})
        assert moved.status_code == 200, moved.text
        version += 1
    return appt


def _grant(engine, tid, client_row):
    contact = next(c for c in client_row["contacts"])
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        conn.execute(
            text("""INSERT INTO consents
                    (tenant_id, id, client_id, contact_id, channel, purpose,
                     source, wording_version, status)
                    VALUES (:t, gen_random_uuid(), :c, :contact, 'email',
                            'marketing', 'client_self', 'v1', 'granted')"""),
            {"t": tid, "c": client_row["id"], "contact": contact["id"]})


def _backdate_visits(engine, tid, client_id, days_ago_list):
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        rows = conn.execute(
            text("""SELECT id::text AS id FROM visits
                    WHERE tenant_id=:t AND client_id=:c ORDER BY completed_at"""),
            {"t": tid, "c": client_id}).scalars().all()
        assert len(rows) == len(days_ago_list)
        for visit_id, days in zip(rows, days_ago_list):
            conn.execute(
                text("""UPDATE visits SET completed_at = now() - (:d || ' days')::interval
                        WHERE tenant_id=:t AND id=:v"""),
                {"t": tid, "v": visit_id, "d": str(days)})


def test_clients_ac1_cohorts_facts_and_segments(database_url):
    """AC-1: cohorts, return gaps, facts, due and reachable cohorts defined."""
    issuer = "https://identity.example.invalid/coh-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        base, loc, service, staff, day = _setup(client, tenant)
        repeat = _client_with_email(client, base, "Repeat", "repeat@example.com")
        _grant(engine, tid, repeat)
        _complete(client, base, service, staff, repeat["id"], day, 10, "coh-r-0001")
        _complete(client, base, service, staff, repeat["id"], day, 14, "coh-r-0002")
        _backdate_visits(engine, tid, repeat["id"], [100, 40])
        new = _client_with_email(client, base, "New", "new@example.com")
        _complete(client, base, service, staff, new["id"], day, 12, "coh-n-0001")
        _backdate_visits(engine, tid, new["id"], [10])
        quiet = client.post(f"{base}/clients", json={"name": "Quiet"}).json()
        # A next appointment for the repeater.
        future_day = day + timedelta(days=20)
        starts = (datetime.combine(future_day, datetime.min.time())
                  + timedelta(hours=10)).replace(tzinfo=timezone.utc)
        nxt = client.post(f"{base}/appointments", json={
            "client_id": repeat["id"], "staff_id": staff, "service_id": service,
            "starts_at": starts.isoformat(), "source": "staff",
            "idempotency_key": "coh-future-001"})
        assert nxt.status_code == 201, nxt.text
        report = client.get(f"/v1/tenants/{tid}/reports/clients")
        assert report.status_code == 200, report.text
        body = report.json()
        cohorts = {c["cohort"]: c for c in body["cohorts"]}
        old_key = (date.today() - timedelta(days=100)).strftime("%Y-%m")
        new_key = (date.today() - timedelta(days=10)).strftime("%Y-%m")
        assert cohorts[old_key]["clients"] == 1
        assert cohorts[old_key]["repeaters"] == 1
        assert cohorts[old_key]["avg_return_days"] == 60.0
        assert cohorts[old_key]["immature"] is False
        assert cohorts["never"]["clients"] == 1
        facts = {f["name"]: f for f in body["clients"]}
        assert facts["Repeat"]["last_service"] == "Cut"
        assert facts["Repeat"]["next_appointment"]
        assert facts["Repeat"]["reachable"] is True
        assert facts["New"]["reachable"] is False
        assert facts["New"]["next_appointment"] is None
        assert body["segments"]["due_cycles"] == 2
        assert body["segments"]["inactive_30d"] == 2
        assert body["segments"]["inactive_90d"] == 1
        assert body["consent_reachability"]["reachable_clients"] == 1
    finally:
        _teardown(engine, [tid], issuer)


def test_clients_ac2_scopes_deletion_and_quality(database_url):
    """AC-2: location scope, deletions, duplicates and import errors handled."""
    issuer = "https://identity.example.invalid/coh-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        base, loc, service, staff, day = _setup(client, tenant)
        first = _client_with_email(client, base, "First", "shared@example.com", loc)
        _complete(client, base, service, staff, first["id"], day, 10, "coh-d-0001")
        twin = _client_with_email(client, base, "Twin", "shared@example.com", loc)
        contact_id = next(c["id"] for c in first["contacts"])
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(
                text("""INSERT INTO client_duplicate_candidates
                        (tenant_id, client_id, contact_id, other_client_id, match_basis,
                         matched_value)
                        VALUES (:t, :a, :contact, :b, 'email', 'shared@example.com')"""),
                {"t": tid, "a": first["id"], "contact": contact_id, "b": twin["id"]})
        gone = client.post(f"{base}/clients", json={"name": "Gone"}).json()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("UPDATE clients SET deleted_at=now() WHERE tenant_id=:t AND id=:c"),
                         {"t": tid, "c": gone["id"]})
        scoped = client.get(f"/v1/tenants/{tid}/reports/clients",
                            params={"location_id": loc}).json()
        assert scoped["filters"]["location_id"] == loc
        # Two open candidates: Twin's creation auto-detected the shared email,
        # plus the manual row above. Both are genuinely open.
        assert scoped["data_quality"]["open_duplicate_candidates"] == 2
        assert scoped["data_quality"]["import_error_rows"] == 0
        assert scoped["data_quality"]["unverified_primaries"] == 2
        assert scoped["data_quality"]["clients_without_contacts"] == 0
        names = [f["name"] for f in scoped["clients"]]
        assert "Gone" not in names
        assert scoped["data_quality"]["clients_by_source"] == {"staff": 2}
        from test_clients import _identity
        outsider = _client(database_url, _identity())
        assert outsider.get(f"/v1/tenants/{tid}/reports/clients").status_code == 404
    finally:
        _teardown(engine, [tid], issuer)
