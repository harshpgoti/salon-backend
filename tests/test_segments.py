"""Tests for consenting-client audience segments (TASK-038, FR-053).

AC-1: History/service/tags/location/spend filters produce scoped counts/exclusions.
AC-2: Consent/contact duplicates and invalid AST do not expand audience.
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
from modules.communications.consent import record_suppression


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


def _client_with(client, base, name, phone=None):
    body = {"name": name}
    if phone:
        kind, value = phone if isinstance(phone, tuple) else ("phone", phone)
        body["contacts"] = [{"kind": kind, "value": value, "is_primary": True}]
    created = client.post(f"{base}/clients", json=body)
    assert created.status_code == 201, created.text
    return created.json()


def _complete(client, base, service, staff, client_id, day, hour, key):
    starts = (datetime.combine(day, datetime.min.time())
              + timedelta(hours=hour)).replace(tzinfo=timezone.utc)
    created = client.post(f"{base}/appointments", json={
        "client_id": client_id, "staff_id": staff, "service_id": service,
        "starts_at": starts.isoformat(), "source": "staff",
        "idempotency_key": key})
    assert created.status_code == 201, created.text
    appt = created.json()["appointment_id"]
    version = 1
    for to_status in ("checked_in", "in_service", "completed"):
        moved = client.post(f"{base}/appointments/{appt}/transitions", json={
            "to_status": to_status, "expected_version": version,
            "idempotency_key": f"seg-t-{key}-{version}"})
        assert moved.status_code == 200, moved.text
        version += 1
    return appt


def _grant(engine, tenant_id, client_row, channel="sms"):
    kind = "email" if channel == "email" else "phone"
    contact = next(c for c in client_row["contacts"] if c["kind"] == kind)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                     {"t": tenant_id})
        conn.execute(
            text("""INSERT INTO consents
                    (tenant_id, id, client_id, contact_id, channel, purpose,
                     source, wording_version, status, contact_value_snapshot)
                    VALUES (:t, gen_random_uuid(), :c, :contact, :ch,
                            'marketing', 'client_self', 'v1', 'granted',
                            lower(btrim((SELECT normalized_value FROM client_contacts
                                         WHERE tenant_id=:t AND id=:contact))))"""),
            {"t": tenant_id, "c": client_row["id"], "contact": contact["id"],
             "ch": channel})


def _receipt(client, base, appt_id, amount):
    posted = client.post(f"{base}/appointments/{appt_id}/receipt-entries", json={
        "kind": "payment", "amount_minor": amount, "method": "card",
        "idempotency_key": "seg-r-" + uuid.uuid4().hex})
    assert posted.status_code == 201, posted.text


def _tag(client, base, client_id, tag):
    resp = client.post(f"{base}/clients/{client_id}/tags", json={"tag": tag})
    assert resp.status_code == 201, resp.text


def test_segment_ac1_filters_and_exclusions(database_url):
    """AC-1: tag/spend/service/inactivity filters scope counts honestly."""
    issuer = "https://identity.example.invalid/seg-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, service, staff, day = _setup(client, tenant)
        tid = tenant["tenant_id"]

        def grant(row):
            _grant(engine, tid, row, "sms")

        vip = _client_with(client, base, "VIP", "+14165550101")
        vip_appt = _complete(client, base, service, staff, vip["id"], day, 14, "seg-key-vip")
        _receipt(client, base, vip_appt, 10000)
        _tag(client, base, vip["id"], "vip")
        grant(vip)

        quiet = _client_with(client, base, "Quiet", "+14165550202")
        quiet_appt = _complete(client, base, service, staff, quiet["id"], day, 15, "seg-key-quiet")
        _receipt(client, base, quiet_appt, 10000)

        # Household shares one number: two clients, one reachable number.
        for name, hour, key in (("Sharer1", 16, "seg-key-s1"), ("Sharer2", 17, "seg-key-s2")):
            sharer = _client_with(client, base, name, "+14165550303")
            _complete(client, base, service, staff, sharer["id"], day, hour, key)
            grant(sharer)

        ghost = _client_with(client, base, "Ghost")

        mixed = client.post(f"{base}/clients", json={"name": "Mixed contact", "contacts": [
            {"kind": "email", "value": "segment-mixed@example.invalid", "is_primary": True},
            {"kind": "phone", "value": "+14165550505", "is_primary": False},
        ]}).json()
        _complete(client, base, service, staff, mixed["id"], day, 13, "seg-key-mixed")
        _grant(engine, tid, mixed, "sms")
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            record_suppression(conn, tenant_id=tid, client_id=mixed["id"],
                               contact_value="+14165550505", channel="sms",
                               reason="stop", source="client_self")
        email_only = _client_with(client, base, "Email only", ("email", "segment-email@example.invalid"))
        _complete(client, base, service, staff, email_only["id"], day, 18, "seg-key-email")

        vips = client.post(f"{base}/segments", json={
            "name": "VIPs",
            "definition": {"and": [{"tag": "vip"}, {"min_spend_minor": 5000}]}})
        assert vips.status_code == 201, vips.text
        preview = client.post(
            f"{base}/segments/{vips.json()['id']}/preview", json={"channel": "sms"}).json()
        assert preview["candidates"] == 1
        assert preview["eligible_count"] == 1
        assert preview["exclusions"] == {"no_consent": 0, "suppressed": 0, "unreachable": 0}

        cutters = client.post(f"{base}/segments", json={
            "name": "Cutters", "definition": {"service": service}})
        assert cutters.status_code == 201, cutters.text
        seen = client.post(
            f"{base}/segments/{cutters.json()['id']}/preview", json={"channel": "sms"}).json()
        # Ghost never completed: not a candidate. Quiet never consented.
        assert seen["candidates"] == 6
        assert seen["eligible_count"] == 3
        assert seen["exclusions"]["no_consent"] == 1
        assert seen["exclusions"]["suppressed"] == 1
        assert seen["exclusions"]["unreachable"] == 1
        assert seen["reachable_numbers"] == 2
        assert seen["shared_numbers"] == 1

        located = client.post(f"{base}/clients", json={"name": "Located", "preferred_location_id": tenant["location_id"],
            "contacts": [{"kind": "phone", "value": "+14165550606", "is_primary": True}]}).json()
        _grant(engine, tid, located)
        location_segment = client.post(f"{base}/segments", json={
            "name": "Located clients", "definition": {"location": tenant["location_id"]}})
        assert location_segment.status_code == 201, location_segment.text
        by_location = client.post(
            f"{base}/segments/{location_segment.json()['id']}/preview", json={"channel": "sms"}).json()
        assert (by_location["candidates"], by_location["eligible_count"]) == (1, 1)

        lapsed = client.post(f"{base}/segments", json={
            "name": "Lapsed", "definition": {"inactive_days": 30}})
        assert lapsed.status_code == 201, lapsed.text
        old = client.post(
            f"{base}/segments/{lapsed.json()['id']}/preview", json={"channel": "sms"}).json()
        # Inactive includes clients with no visit history; the located client is reachable.
        assert old["candidates"] == 2
        assert old["exclusions"]["unreachable"] == 1
        assert old["eligible_count"] == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_segment_ac2_invalid_ast_never_expands(database_url):
    """AC-2: unknown operators, bad refs and runaway trees are rejected."""
    issuer = "https://identity.example.invalid/seg-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, service, staff, day = _setup(client, tenant)

        def attempt(definition):
            return client.post(f"{base}/segments",
                               json={"name": "Bad", "definition": definition}).status_code

        assert attempt({"blast": True}) == 422
        assert attempt({"tag": ["vip"]}) == 422
        assert attempt({"service": "not-a-uuid"}) == 422
        assert attempt({"and": []}) == 422
        assert attempt({"min_spend_minor": -5}) == 422
        assert attempt({"min_spend_minor": True}) == 422
        assert attempt({"max_spend_minor": True}) == 422
        assert attempt({"inactive_days": True}) == 422
        deep = {"tag": "x"}
        for _ in range(7):
            deep = {"not": deep}
        assert attempt(deep) == 422
        # Nothing was stored by any rejection.
        assert client.get(f"{base}/segments").json()["items"] == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_segment_ac1_tenant_isolation(database_url):
    """AC-1: one salon's audience never includes another salon's clients."""
    issuer_a = "https://identity.example.invalid/seg-3a"
    issuer_b = "https://identity.example.invalid/seg-3b"
    engine_a, owner_a, tenant_a = _salon(database_url, issuer_a)
    engine_b, owner_b, tenant_b = _salon(database_url, issuer_b)
    client_a = _client(database_url, owner_a)
    client_b = _client(database_url, owner_b)
    try:
        base_a, service_a, staff_a, day_a = _setup(client_a, tenant_a)
        base_b, service_b, staff_b, day_b = _setup(client_b, tenant_b)
        other = _client_with(client_b, base_b, "Other VIP", "+14165550404")
        other_appt = _complete(client_b, base_b, service_b, staff_b, other["id"],
                               day_b, 14, "seg-key-other")
        _receipt(client_b, base_b, other_appt, 50000)
        _tag(client_b, base_b, other["id"], "vip")
        seg = client_a.post(f"{base_a}/segments", json={
            "name": "VIPs",
            "definition": {"and": [{"tag": "vip"}, {"min_spend_minor": 5000}]}})
        assert seg.status_code == 201, seg.text
        preview = client_a.post(
            f"{base_a}/segments/{seg.json()['id']}/preview", json={"channel": "sms"}).json()
        assert preview["candidates"] == 0
        assert preview["eligible_count"] == 0
    finally:
        _teardown(engine_a, [tenant_a["tenant_id"]], issuer_a)
        _teardown(engine_b, [tenant_b["tenant_id"]], issuer_b)
