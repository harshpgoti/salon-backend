"""Tests for deterministic rebooking rule previews (TASK-036, FR-050).

AC-1: Validated service/interval/channel/time/cap rules show eligible/cost preview.
AC-2: Arbitrary script or invalid segment/tenant refs rejected.
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
    return base, service, staff, day


def _client_with(client, base, name, contact=None):
    body = {"name": name}
    if contact:
        body["contacts"] = [{"kind": contact[0], "value": contact[1], "is_primary": True}]
    created = client.post(f"{base}/clients", json=body)
    assert created.status_code == 201, created.text
    return created.json()


def _book_complete(client, base, service, staff, client_id, day, hour, key):
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
            "idempotency_key": f"rule-t-{key}-{version}"})
        assert moved.status_code == 200, moved.text
        version += 1


def _grant(client, engine, tenant_id, client_row, channel):
    contact = next(c for c in client_row["contacts"])
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                     {"t": tenant_id})
        conn.execute(
            text("""INSERT INTO consents
                    (tenant_id, id, client_id, contact_id, channel, purpose,
                     source, wording_version, status)
                    VALUES (:t, gen_random_uuid(), :c, :contact, :ch,
                            'marketing', 'client_self', 'v1', 'granted')"""),
            {"t": tenant_id, "c": client_row["id"], "contact": contact["id"],
             "ch": channel})


def _rule(client, base, service, **overrides):
    body = {"service_id": service, "interval_days": 42, "channel": "sms",
            "send_time_local": "10:00", "max_follow_ups": 1}
    body.update(overrides)
    return client.post(f"{base}/rebooking-rules", json=body)


def test_rule_ac1_preview_counts_and_cost(database_url):
    """AC-1: eligible/suppressed/unreachable counts and cost are deterministic."""
    issuer = "https://identity.example.invalid/rule-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, service, staff, day = _setup(client, tenant)
        # Eligible: due + consented + reachable.
        willing = _client_with(client, base, "Willing", ("phone", "+14165550101"))
        _book_complete(client, base, service, staff, willing["id"], day, 10, "rule-a-1")
        _grant(client, engine, tenant["tenant_id"], willing, "sms")
        # Suppressed: due but never consented.
        quiet = _client_with(client, base, "Quiet", ("phone", "+14165550202"))
        _book_complete(client, base, service, staff, quiet["id"], day, 12, "rule-a-2")
        # Unreachable: due with no contact at all.
        ghost = _client_with(client, base, "Ghost")
        _book_complete(client, base, service, staff, ghost["id"], day, 14, "rule-a-3")
        created = _rule(client, base, service)
        assert created.status_code == 201, created.text
        rule_id = created.json()["id"]
        first = client.post(f"{base}/rebooking-rules/{rule_id}/preview").json()
        assert (first["due_total"], first["eligible"], first["suppressed"],
                first["unreachable"]) == (3, 1, 1, 1)
        # One eligible client x (prompt + 1 follow-up) x 1c SMS segment.
        assert first["estimated_sends"] == 2
        assert first["estimated_cost_minor"] == 2
        assert first["currency"] == "CAD"
        # Same data, same preview: deterministic.
        second = client.post(f"{base}/rebooking-rules/{rule_id}/preview").json()
        assert second == first
        listed = client.get(f"{base}/rebooking-rules").json()["items"]
        assert len(listed) == 1 and listed[0]["service_name"] == "Cut"
        activated = client.post(f"{base}/rebooking-rules/{rule_id}/activate")
        assert activated.status_code == 200 and activated.json()["is_active"] is True
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_rule_ac2_invalid_refs_and_scripts_rejected(database_url):
    """AC-2: bad service/segment/channel/caps/scripts never become rules."""
    issuer = "https://identity.example.invalid/rule-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, service, staff, day = _setup(client, tenant)
        assert _rule(client, base, service, channel="pigeon").status_code == 422
        assert _rule(client, base, service, interval_days=0).status_code == 422
        assert _rule(client, base, service, max_follow_ups=5).status_code == 422
        assert _rule(client, base, service,
                     send_time_local="25:00").status_code == 422
        # Audience segments do not exist yet: any reference is invalid.
        assert _rule(client, base, service,
                     segment_id=str(uuid.uuid4())).status_code == 422
        # A service from another salon (or nowhere) is rejected.
        assert _rule(client, base, str(uuid.uuid4())).status_code == 422
        # An arbitrary script field is rejected, never stored or executed.
        smuggled = client.post(f"{base}/rebooking-rules", json={
            "service_id": service, "interval_days": 42, "channel": "sms",
            "send_time_local": "10:00", "max_follow_ups": 1,
            "script": "DROP TABLE clients;"})
        assert smuggled.status_code == 422
        # Versioned updates conflict honestly.
        created = _rule(client, base, service)
        rule_id = created.json()["id"]
        conflict = client.patch(f"{base}/rebooking-rules/{rule_id}", json={
            "expected_version": 999, "interval_days": 30})
        assert conflict.status_code == 409
        updated = client.patch(f"{base}/rebooking-rules/{rule_id}", json={
            "expected_version": 1, "interval_days": 30})
        assert updated.status_code == 200 and updated.json()["version"] == 2
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
