"""Tests for rebooking prompt suppression and attribution (TASK-037, FR-051/FR-052).

AC-1: Future suitable booking/new completion/opt-out/snooze suppress old cycle.
AC-2: Due prompt and one follow-up cap create traced attributed booking, not automatic appointment.
"""

import os
import sys
import uuid
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.retention import run_rebooking_prompts
from modules.worker.communications import dispatch_once, dispatch_tenant_once
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


def _client_with_phone(client, base, name="Prompted", phone="+14165550991"):
    created = client.post(f"{base}/clients", json={
        "name": name,
        "contacts": [{"kind": "phone", "value": phone, "is_primary": True}],
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
                     source, wording_version, status, contact_value_snapshot)
                    VALUES (:t, gen_random_uuid(), :c, :contact, :ch,
                            'marketing', 'client_self', 'v1', 'granted',
                            (SELECT normalized_value FROM client_contacts
                             WHERE tenant_id=:t AND id=:contact))"""),
            {"t": tid, "c": client_row["id"], "contact": contact["id"], "ch": channel})


def _book_complete(client, base, service, staff, client_id, day, hour, key):
    starts = (datetime.combine(day, datetime.min.time())
              + timedelta(hours=hour)).replace(tzinfo=ZoneInfo("America/Toronto"))
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


def _activate_rule(client, base, service, channel="email", send_time="10:00",
                   max_follow_ups=1):
    created = client.post(f"{base}/rebooking-rules", json={
        "service_id": service, "interval_days": 42, "channel": channel,
        "send_time_local": send_time, "max_follow_ups": max_follow_ups})
    assert created.status_code == 201, created.text
    rule_id = created.json()["id"]
    assert client.post(f"{base}/rebooking-rules/{rule_id}/activate").status_code == 200
    return rule_id


def _run(engine, tid, now=None):
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        return run_rebooking_prompts(connection=conn, tenant_id=tid, now=now)


def _due_now():
    # Use a stable local send hour after the 42-day cycle due date.
    day = date.today() + timedelta(days=43)
    return datetime.combine(day, datetime.min.time()).replace(
        hour=12, tzinfo=ZoneInfo("America/Toronto")).astimezone(timezone.utc)


class RecordingAdapter:
    def __init__(self):
        self.records = []

    def send(self, *, channel, recipient, template, sender=None, unsubscribe_url=None,
             idempotency_key=None, template_category=None, template_locale=None,
             message_body=None):
        self.records.append({"channel": channel, "recipient": recipient,
                             "template": template, "message_body": message_body})
        return {"status": "recorded", "id": f"retention-{len(self.records)}"}


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
                               source_type FROM communications
                        WHERE tenant_id=:t AND source_type='rebooking_cycle'"""),
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


def test_prompts_ac2_worker_schedules_and_dispatches_tracked_booking_link(database_url):
    """The tenant-discovering worker creates and dispatches due prompts itself."""
    issuer = "https://identity.example.invalid/prm-worker"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, service, staff, day = _setup(client, tenant)
        row = _client_with_email(client, base)
        _grant(engine, tid, row)
        _activate_rule(client, base, service)
        _book_complete(client, base, service, staff, row["id"], day, 10,
                       "prm-worker-complete")
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""INSERT INTO tenant_entitlements
                (tenant_id, plan_code, status, max_locations, max_staff,
                 included_sms_segments, included_emails, marketing_allowed)
                VALUES (:t, 'synthetic', 'active', 1, 5, 100, 100, true)
                ON CONFLICT (tenant_id) DO UPDATE SET marketing_allowed=true"""),
                {"t": tid})
        adapter = RecordingAdapter()
        summary = dispatch_once(engine, adapter=adapter, now=_due_now())
        assert summary["prompted"] == 1
        assert summary["dispatched"] == 1
        assert len(adapter.records) == 1
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            row = conn.execute(text("""SELECT attribution_token, reply_body
                FROM communications WHERE tenant_id=:t AND source_type='rebooking_cycle'"""),
                {"t": tid}).mappings().one()
        # Prompt scheduling used a future worker-clock fixture; record an accepted
        # send just inside the attribution window for this booking exercise.
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE communications SET dispatched_at=now()-interval '1 hour'
                WHERE tenant_id=:t AND source_type='rebooking_cycle'"""), {"t": tid})
        assert row["attribution_token"]
        assert f"?attribution_token={row['attribution_token']}" in row["reply_body"]
        assert adapter.records[0]["message_body"] == row["reply_body"]
        slug = _slug(database_url, tid, tenant["location_id"])
        start = _first_slot(client, slug, service, day)
        hold = client.post(f"/v1/public/salons/{slug}/holds", json={
            "service_id": service, "staff_id": staff, "starts_at": start,
            "idempotency_key": "prm-worker-hold-1"})
        assert hold.status_code == 201, hold.text
        confirmed = client.post(
            f"/v1/public/salons/{slug}/holds/{hold.json()['hold_id']}/confirm", json={
                "owner_token": hold.json()["owner_token"],
                "client_name": "Prompted", "email": "prompted@example.com",
                "policy_accepted": True, "policy_version": 1,
                "idempotency_key": "prm-worker-confirm-1",
                "attribution_token": row["attribution_token"]})
        assert confirmed.status_code == 200, confirmed.text
        assert _cycle(engine, tid)["status"] == "booked"
    finally:
        _teardown(engine, [tid], issuer)


def test_prompts_worker_rotates_past_tenants_that_cannot_receive_marketing(database_url):
    """A consent-ineligible tenant cannot occupy the discovery page forever."""
    issuer = "https://identity.example.invalid/prm-fair-poll"
    engine, owner_a, tenant_a = _salon(database_url, issuer)
    client_a = _client(database_url, owner_a)
    tid_a = tenant_a["tenant_id"]
    engine_b = None
    tenant_b = None
    tid_b = None
    try:
        base_a, service_a, staff_a, day_a = _setup(client_a, tenant_a)
        row_a = _client_with_email(client_a, base_a, name="No Consent")
        _activate_rule(client_a, base_a, service_a)
        _book_complete(client_a, base_a, service_a, staff_a, row_a["id"], day_a,
                       10, "prm-fair-ineligible")

        engine_b, owner_b, tenant_b = _salon(database_url, issuer)
        client_b = _client(database_url, owner_b)
        tid_b = tenant_b["tenant_id"]
        base_b, service_b, staff_b, day_b = _setup(client_b, tenant_b)
        row_b = _client_with_email(client_b, base_b, name="Eligible")
        _grant(engine_b, tid_b, row_b)
        _activate_rule(client_b, base_b, service_b)
        _book_complete(client_b, base_b, service_b, staff_b, row_b["id"], day_b,
                       10, "prm-fair-eligible")

        start = _due_now()
        with engine.connect() as conn:
            due_ids = set(conn.execute(text(
                "SELECT tenant_id::text FROM due_rebooking_tenants(:now, 500)"),
                {"now": start}).scalars())
        assert {tid_a, tid_b} <= due_ids

        seen = set()
        # Simulate consecutive five-second worker polls. Ineligible tenants stay
        # due, so rotation must eventually expose the later eligible tenant.
        for poll in range(len(due_ids) * 2):
            moment = start + timedelta(seconds=5 * poll)
            with engine.connect() as conn:
                selected = conn.execute(text(
                    "SELECT tenant_id::text FROM due_rebooking_tenants(:now, 1)"),
                    {"now": moment}).scalar_one_or_none()
            if selected == tid_a:
                seen.add(tid_a)
                result = dispatch_tenant_once(engine, tid_a, now=moment)
                assert result["consent_skip"] >= 1
            elif selected == tid_b and tid_b not in seen:
                seen.add(tid_b)
                result = dispatch_tenant_once(engine_b, tid_b, now=moment)
                assert result["prompted"] == 1
            if {tid_a, tid_b} <= seen:
                break
        assert {tid_a, tid_b} <= seen
    finally:
        _teardown(engine, [tid_a], issuer)
        if engine_b is not None and tid_b is not None:
            _teardown(engine_b, [tid_b], issuer)


def test_prompts_ac1_future_booking_after_queue_stales_before_send(database_url):
    """A suitable booking created after enqueue blocks the queued prompt."""
    issuer = "https://identity.example.invalid/prm-future-stale"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, service, staff, day = _setup(client, tenant)
        row = _client_with_email(client, base)
        _grant(engine, tid, row)
        _activate_rule(client, base, service)
        _book_complete(client, base, service, staff, row["id"], day, 10,
                       "prm-future-stale-complete")
        moment = _due_now()
        assert _run(engine, tid, now=moment)["prompted"] == 1
        future_day = day + timedelta(days=49)
        future_start = (datetime.combine(future_day, datetime.min.time())
                        + timedelta(hours=10)).replace(tzinfo=ZoneInfo("America/Toronto"))
        future = client.post(f"{base}/appointments", json={
            "client_id": row["id"], "staff_id": staff, "service_id": service,
            "starts_at": future_start.isoformat(), "source": "staff",
            "idempotency_key": "prm-future-stale-booking"})
        assert future.status_code == 201, future.text
        adapter = RecordingAdapter()
        result = dispatch_tenant_once(engine, tid, adapter=adapter, now=moment)
        assert result["skipped_stale"] >= 1
        assert not any("attribution_token=" in (record["message_body"] or "")
                       for record in adapter.records)
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            state = conn.execute(text("""SELECT state FROM communications
                WHERE tenant_id=:t AND source_type='rebooking_cycle'"""),
                {"t": tid}).scalar_one()
        assert state == "skipped_stale"
    finally:
        _teardown(engine, [tid], issuer)


def test_prompts_ac1_snooze_after_queue_stales_before_send(database_url):
    """A staff snooze after queueing prevents the old prompt from sending."""
    issuer = "https://identity.example.invalid/prm-snooze-stale"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, service, staff, day = _setup(client, tenant)
        row = _client_with_email(client, base)
        _grant(engine, tid, row)
        _activate_rule(client, base, service)
        _book_complete(client, base, service, staff, row["id"], day, 10,
                       "prm-snooze-stale-complete")
        moment = _due_now()
        assert _run(engine, tid, now=moment)["prompted"] == 1
        cycle_id = _cycle(engine, tid)["id"]
        until = moment.astimezone(ZoneInfo("America/Toronto")).date() + timedelta(days=30)
        snooze = client.post(f"{base}/rebooking-cycles/{cycle_id}/snooze", json={
            "until": until.isoformat()})
        assert snooze.status_code == 200, snooze.text
        adapter = RecordingAdapter()
        result = dispatch_tenant_once(engine, tid, adapter=adapter, now=moment)
        assert result["skipped_stale"] >= 1
        assert not any("attribution_token=" in (record["message_body"] or "")
                       for record in adapter.records)
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            state = conn.execute(text("""SELECT state FROM communications
                WHERE tenant_id=:t AND source_type='rebooking_cycle'"""),
                {"t": tid}).scalar_one()
        assert state == "skipped_stale"
    finally:
        _teardown(engine, [tid], issuer)


def test_prompts_ac1_configured_local_send_time(database_url):
    """An active rule waits for its configured salon-local send time."""
    issuer = "https://identity.example.invalid/prm-local-time"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, service, staff, day = _setup(client, tenant)
        row = _client_with_email(client, base)
        _grant(engine, tid, row)
        _activate_rule(client, base, service, send_time="13:00")
        _book_complete(client, base, service, staff, row["id"], day, 10,
                       "prm-local-time-complete")
        early = _run(engine, tid, now=_due_now())
        assert early["send_time_skip"] == 1 and early["prompted"] == 0
        assert _run(engine, tid, now=_due_now() + timedelta(hours=1))["prompted"] == 1
    finally:
        _teardown(engine, [tid], issuer)


def test_prompts_ac2_zero_follow_up_limit_is_enforced(database_url):
    """Rule-level zero follow-ups permits the due prompt only."""
    issuer = "https://identity.example.invalid/prm-zero-follow-up"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, service, staff, day = _setup(client, tenant)
        row = _client_with_email(client, base)
        _grant(engine, tid, row)
        _activate_rule(client, base, service, max_follow_ups=0)
        _book_complete(client, base, service, staff, row["id"], day, 10,
                       "prm-zero-follow-up-complete")
        moment = _due_now()
        assert _run(engine, tid, now=moment)["prompted"] == 1
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("UPDATE rebooking_prompts SET sent_at=:at WHERE tenant_id=:t"),
                         {"t": tid, "at": moment - timedelta(days=8)})
        assert _run(engine, tid, now=moment)["capped_skip"] == 1
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            assert conn.execute(text("SELECT count(*) FROM rebooking_prompts WHERE tenant_id=:t"),
                                {"t": tid}).scalar_one() == 1
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
        future_day = date.today() + timedelta(days=50)
        starts = (datetime.combine(future_day, datetime.min.time())
                  + timedelta(hours=10)).replace(tzinfo=ZoneInfo("America/Toronto"))
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
                         source, wording_version, status, contact_value_snapshot)
                        VALUES (:t, gen_random_uuid(), :c, :contact, 'email',
                                'marketing', 'client_self', 'v1', 'revoked',
                                (SELECT normalized_value FROM client_contacts
                                 WHERE tenant_id=:t AND id=:contact))"""),
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
        dispatch_tenant_once(engine, tid, now=moment)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE communications SET dispatched_at=now()-interval '1 hour'
                WHERE tenant_id=:t AND purpose='marketing'"""), {"t": tid})
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
                "owner_token": hold.json()["owner_token"],
                "client_name": "Prompted", "email": "prompted@example.com",
                "policy_accepted": True, "policy_version": 1,
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
                "owner_token": hold2.json()["owner_token"],
                "client_name": "Synthetic Organic Booker", "email": "organic@example.invalid",
                "policy_accepted": True, "policy_version": 1,
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
        row = _client_with_phone(client, base)
        _grant(engine, tid, row, channel="whatsapp")
        _activate_rule(client, base, service, channel="whatsapp")
        _book_complete(client, base, service, staff, row["id"], day, 10, "prm-key-6")
        assert _run(engine, tid, now=_due_now())["channel_skip"] == 1
    finally:
        _teardown(engine, [tid], issuer)
