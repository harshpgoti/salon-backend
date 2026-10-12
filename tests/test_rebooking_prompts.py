"""Tests for rebooking prompt suppression and attribution (TASK-037, FR-051/FR-052).

AC-1: Future suitable booking/new completion/opt-out/snooze suppress old cycle.
AC-2: Due prompt and one follow-up cap create traced attributed booking, not automatic appointment.
"""

import os
import sys
import uuid
from datetime import date, timedelta, datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.retention import run_rebooking_prompts
from test_booking_holds import _bookable, _first_slot, _slug
from test_clients import _salon, _client, _teardown

@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _setup(client, tenant, interval_days=42):
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
    return base, service, staff, day


def _client_with_email(client, base, name="Prompted", email="prompted@example.com"):
    created = client.post(f"{base}/clients", json={
        "name": name,
        "contacts": [{"kind": "email", "value": email, "is_primary": True}],
    })
    assert created.status_code == 201, created.text
    return created.json()


def _grant(engine, tid, client_row, channel="email"):
    contact = next(c for c in client_row["contacts"])
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        conn.execute(
            text("""INSERT INTO consents
                    (tenant_id, id, client_id, contact_id, channel, purpose,
                     source, wording_version, status)
                    VALUES (:t, gen_random_uuid(), :c, :contact, :ch,
                            'marketing', 'client_self', 'v1', 'granted')"""),
            {"t": tid, "c": client_row["id"], "contact": contact["id"], "ch": channel})


def _book_complete(client, base, service, staff, client_id, day, hour, key):
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
            "idempotency_key": f"prm-t-{key}-{version}"})
        assert moved.status_code == 200, moved.text
        version += 1
    return appt


def _activate_rule(client, base, service, channel="email"):
    created = client.post(f"{base}/rebooking-rules", json={
        "service_id": service, "interval_days": 42, "channel": channel,
        "send_time_local": "10:00", "max_follow_ups": 1})
    assert created.status_code == 201, created.text
    rule_id = created.json()["id"]
    assert client.post(f"{base}/rebooking-rules/{rule_id}/activate").status_code == 200
    return rule_id


def _run(engine, tid, now=None):
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        return run_rebooking_prompts(connection=conn, tenant_id=tid, now=now)


def _due_now():
    # 42-day cycles from a completion today come due in 43 days.
    return datetime.now(timezone.utc) + timedelta(days=43)


def _cycle(engine, tid):
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        return conn.execute(
            text("""SELECT id::text AS id, status, booked_appointment_id::text AS appt,
                           attributed_communication_id::text AS comm
                    FROM rebooking_cycles WHERE tenant_id=:t"""),
            {"t": tid}).mappings().one()


def test_prompts_ac1_due_prompt_records_with_token(database_url):
    """AC-1: a due cycle prompts once with a tracked token, nothing automatic."""
    issuer = "https://identity.example.invalid/prm-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, service, staff, day = _setup(client, tenant)
        row = _client_with_email(client, base)
        _grant(engine, tid, row)
        _activate_rule(client, base, service)
        _book_complete(client, base, service, staff, row["id"], day, 10, "prm-key-1")
        summary = _run(engine, tid, now=_due_now())
        assert summary["prompted"] == 1
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            comm = conn.execute(
                text("""SELECT purpose, channel, state, attribution_token,
                               source_type FROM communications WHERE tenant_id=:t"""),
                {"t": tid}).mappings().one()
            prompts = conn.execute(
                text("SELECT kind FROM rebooking_prompts WHERE tenant_id=:t"),
                {"t": tid}).scalars().all()
            cycle = _cycle(engine, tid)
        assert comm["purpose"] == "marketing" and comm["channel"] == "email"
        assert comm["state"] == "recorded" and comm["source_type"] == "rebooking_cycle"
        assert comm["attribution_token"]
        assert prompts == ["due_prompt"]
        # The prompt is a message, not an appointment: the cycle stays due.
        assert cycle["status"] == "due" and cycle["appt"] is None
    finally:
        _teardown(engine, [tid], issuer)


def test_prompts_ac1_suppressions_leave_cycle_due(database_url):
    """AC-1: future booking, opt-out, suspension and bad contacts only skip."""
    issuer = "https://identity.example.invalid/prm-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, service, staff, day = _setup(client, tenant)
        row = _client_with_email(client, base)
        _grant(engine, tid, row)
        _activate_rule(client, base, service)
        _book_complete(client, base, service, staff, row["id"], day, 10, "prm-key-2")
        # A suitable future booking suppresses prompting (cycle stays due).
        # Dated past the worker's run time so it is still future there.
        future_day = date.today() + timedelta(days=60)
        starts = (datetime.combine(future_day, datetime.min.time())
                  + timedelta(hours=10)).replace(tzinfo=timezone.utc)
        booked = client.post(f"{base}/appointments", json={
            "client_id": row["id"], "staff_id": staff, "service_id": service,
            "starts_at": starts.isoformat(), "source": "staff",
            "idempotency_key": "prm-future-1"})
        assert booked.status_code == 201, booked.text
        assert _run(engine, tid, now=_due_now())["future_booking_skip"] == 1
        assert _cycle(engine, tid)["status"] == "due"
        # Opt-out suppresses too.
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("DELETE FROM appointments WHERE tenant_id=:t AND id=:a"),
                         {"t": tid, "a": booked.json()["appointment_id"]})
        contact_id = next(c["id"] for c in row["contacts"])
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(
                text("""INSERT INTO consents
                        (tenant_id, id, client_id, contact_id, channel, purpose,
                         source, wording_version, status)
                        VALUES (:t, gen_random_uuid(), :c, :contact, 'email',
                                'marketing', 'client_self', 'v1', 'revoked')"""),
                {"t": tid, "c": row["id"], "contact": contact_id})
        assert _run(engine, tid, now=_due_now())["consent_skip"] == 1
    finally:
        _teardown(engine, [tid], issuer)


def test_prompts_ac1_salon_and_contact_gates(database_url):
    """AC-1: suspended salons and contact-less clients produce no prompts."""
    issuer = "https://identity.example.invalid/prm-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, service, staff, day = _setup(client, tenant)
        lonely = client.post(f"{base}/clients", json={"name": "Lonely"}).json()
        _book_complete(client, base, service, staff, lonely["id"], day, 10, "prm-key-3")
        _activate_rule(client, base, service)
        assert _run(engine, tid, now=_due_now())["invalid_contact_skip"] == 1
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("UPDATE tenant_subscriptions SET status='suspended' WHERE tenant_id=:t"),
                         {"t": tid})
        assert _run(engine, tid, now=_due_now())["salon_skip"] == 1
    finally:
        _teardown(engine, [tid], issuer)


def test_prompts_ac1_snooze_and_dismissal(database_url):
    """AC-1: snooze defers to its date; dismissal ends prompting."""
    issuer = "https://identity.example.invalid/prm-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, service, staff, day = _setup(client, tenant)
        row = _client_with_email(client, base)
        _grant(engine, tid, row)
        _activate_rule(client, base, service)
        _book_complete(client, base, service, staff, row["id"], day, 10, "prm-key-4")
        cycle_id = _cycle(engine, tid)["id"]
        snoozed = client.post(f"{base}/rebooking-cycles/{cycle_id}/snooze", json={
            "until": (date.today() + timedelta(days=60)).isoformat()})
        assert snoozed.status_code == 200, snoozed.text
        assert _run(engine, tid, now=_due_now())["snoozed_skip"] == 1
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE rebooking_cycles SET snoozed_until=:d
                                 WHERE tenant_id=:t AND id=:c"""),
                         {"t": tid, "c": cycle_id,
                          "d": (date.today() - timedelta(days=1)).isoformat()})
        assert _run(engine, tid, now=_due_now())["prompted"] == 1
        dismissed = client.post(f"{base}/rebooking-cycles/{cycle_id}/dismiss")
        assert dismissed.status_code == 200, dismissed.text
        out = _run(engine, tid, now=_due_now())
        assert out["prompted"] == 0 and _cycle(engine, tid)["status"] == "dismissed"
    finally:
        _teardown(engine, [tid], issuer)


def test_prompts_ac2_cooldown_cap_and_attribution(database_url):
    """AC-2: one prompt plus one 7-day follow-up, then silence; bookings attribute."""
    issuer = "https://identity.example.invalid/prm-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, service, staff, day = _setup(client, tenant)
        row = _client_with_email(client, base)
        _grant(engine, tid, row)
        _activate_rule(client, base, service)
        _book_complete(client, base, service, staff, row["id"], day, 10, "prm-key-5")
        moment = _due_now()
        assert _run(engine, tid, now=moment)["prompted"] == 1
        # The first prompt went out an hour ago in worker time: cooldown holds.
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE rebooking_prompts SET sent_at = :at
                                 WHERE tenant_id=:t"""),
                         {"t": tid,
                          "at": (moment - timedelta(hours=1)).isoformat()})
        assert _run(engine, tid, now=moment)["cooldown_skip"] == 1
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE rebooking_prompts SET sent_at = :at
                                 WHERE tenant_id=:t"""),
                         {"t": tid,
                          "at": (moment - timedelta(days=8)).isoformat()})
        second = _run(engine, tid, now=moment)
        assert second["prompted"] == 1 and second["follow_ups"] == 1
        assert _run(engine, tid, now=moment)["capped_skip"] == 1
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            token = conn.execute(
                text("""SELECT attribution_token FROM communications
                        WHERE tenant_id=:t AND purpose='marketing'
                        ORDER BY created_at LIMIT 1"""),
                {"t": tid}).scalar_one()
        # A tracked booking through the public flow closes the cycle.
        slug = _slug(database_url, tid, tenant["location_id"])
        start = _first_slot(client, slug, service, day)
        hold = client.post(f"/v1/public/salons/{slug}/holds",
                           json={"service_id": service, "staff_id": staff,
                                 "starts_at": start, "idempotency_key": "prm-hold-0001"})
        assert hold.status_code == 201, hold.text
        confirmed = client.post(
            f"/v1/public/salons/{slug}/holds/{hold.json()['hold_id']}/confirm", json={
                "owner_token": hold.json()["owner_token"], "client_id": row["id"],
                "idempotency_key": "prm-confirm-0001", "attribution_token": token})
        assert confirmed.status_code == 200, confirmed.text
        cycle = _cycle(engine, tid)
        assert cycle["status"] == "booked"
        assert cycle["appt"] == confirmed.json()["appointment_id"]
        assert cycle["comm"]
        # A bogus token breaks nothing and attributes nothing.
        start2 = _first_slot(client, slug, service, day)
        hold2 = client.post(f"/v1/public/salons/{slug}/holds",
                            json={"service_id": service, "staff_id": staff,
                                  "starts_at": start2, "idempotency_key": "prm-hold-0002"})
        assert hold2.status_code == 201, hold2.text
        organic = client.post(
            f"/v1/public/salons/{slug}/holds/{hold2.json()['hold_id']}/confirm", json={
                "owner_token": hold2.json()["owner_token"], "client_id": row["id"],
                "idempotency_key": "prm-confirm-0002",
                "attribution_token": "bogus-token"})
        assert organic.status_code == 200, organic.text
        assert _cycle(engine, tid)["status"] == "booked"
    finally:
        _teardown(engine, [tid], issuer)


def test_prompts_ac2_whatsapp_waits_for_templates(database_url):
    """AC-2: WhatsApp rules skip loudly until campaign templates land."""
    issuer = "https://identity.example.invalid/prm-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, service, staff, day = _setup(client, tenant)
        row = _client_with_email(client, base)
        _grant(engine, tid, row, channel="whatsapp")
        _activate_rule(client, base, service, channel="whatsapp")
        _book_complete(client, base, service, staff, row["id"], day, 10, "prm-key-6")
        assert _run(engine, tid, now=_due_now())["channel_skip"] == 1
    finally:
        _teardown(engine, [tid], issuer)
