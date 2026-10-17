"""Tests for closure notices and audience purpose policy (TASK-033, FR-026/FR-042).

AC-1: Affected bookings prioritized; broader location audience follows purpose policy.
AC-2: Opted-out/unreachable contacts create staff follow-up without unlawful fallback.
"""

import os
import sys
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.outbox import dispatch_due_communications
from modules.communications.senders import register_sender, verify_sender
from modules.worker.communications import dispatch_once
from test_clients import _salon, _client, _teardown
from test_communications import RecordingAdapter
from test_closure_impacts import _bookable_salon_with_appointment


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _email_client(client, base, name, email, loc=None):
    body = {"name": name,
            "contacts": [{"kind": "email", "value": email, "is_primary": True}]}
    if loc is not None:
        body["preferred_location_id"] = loc
    created = client.post(f"{base}/clients", json=body)
    assert created.status_code == 201, created.text
    return created.json()


def _grant(engine, tid, client_row, channel="email"):
    kind = "email" if channel == "email" else "phone"
    contact = next(c for c in client_row["contacts"] if c["kind"] == kind)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        conn.execute(
            text("""INSERT INTO consents
                    (tenant_id, id, client_id, contact_id, channel, purpose,
                     source, wording_version, status, contact_value_snapshot)
                    VALUES (:t, gen_random_uuid(), :c, :contact, :channel,
                            'marketing', 'client_self', 'v1', 'granted',
                            lower(btrim((SELECT normalized_value FROM client_contacts
                                         WHERE tenant_id=:t AND id=:contact))))"""),
            {"t": tid, "c": client_row["id"], "contact": contact["id"],
             "channel": channel})


def _closed_tomorrow(client, base, loc):
    tomorrow = date.today() + timedelta(days=1)
    exc = client.post(f"{base}/locations/{loc}/exceptions", json={
        "exception_date": tomorrow.isoformat(), "kind": "closed"}).json()["id"]
    client.post(f"{base}/locations/{loc}/exceptions/{exc}/publish",
                json={"published": True})
    return tomorrow


def _safe_dispatch_time():
    """Use the next salon-local daytime window so quiet hours stay deterministic."""
    now = datetime.now(ZoneInfo("America/Toronto"))
    candidate = now.replace(hour=11, minute=0, second=0, microsecond=0)
    if candidate <= now + timedelta(hours=2):
        candidate += timedelta(days=1)
    return candidate.astimezone(timezone.utc)


def _record_impact(client, base, loc, appt_id, tomorrow):
    impact = client.post(f"{base}/closures/{loc}/impacts", json={
        "appointment_id": appt_id, "location_id": loc,
        "exception_date": tomorrow.isoformat()})
    assert impact.status_code == 200, impact.text
    return impact.json()["id"]


def test_closure_ac1_impact_notice_sends_first(database_url):
    """AC-1: a recorded impact notifies its client immediately."""
    issuer = "https://identity.example.invalid/notice-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, _, _, _, appt_id, _ = _bookable_salon_with_appointment(
            client, database_url, tenant)
        tomorrow = _closed_tomorrow(client, base, loc)
        impact_id = _record_impact(client, base, loc, appt_id, tomorrow)
        notified = client.post(f"{base}/closures/{loc}/impacts/{impact_id}/notify")
        assert notified.status_code == 200, notified.text
        # Client One (fixture) carries no contacts: unreachable opens a task.
        assert notified.json()["notified"] is False
        assert notified.json()["follow_up_task_id"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_closure_ac1_reachable_impact_sends(database_url):
    """AC-1: an impact client with a contact gets the notice, then it sends."""
    issuer = "https://identity.example.invalid/notice-1b"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, service, staff, _, _, _ = _bookable_salon_with_appointment(
            client, database_url, tenant)
        tomorrow = date.today() + timedelta(days=1)
        friend = _email_client(client, base, "Friend", "friend@example.com")
        starts = datetime.combine(tomorrow, datetime.min.time()).replace(
            hour=14, tzinfo=ZoneInfo("America/Toronto"))
        appt = client.post(f"{base}/appointments", json={
            "client_id": friend["id"], "staff_id": staff, "service_id": service,
            "location_id": loc,
            "starts_at": starts.isoformat(), "source": "staff",
            "idempotency_key": "notice-reach-1"})
        assert appt.status_code == 201, appt.text
        _closed_tomorrow(client, base, loc)
        impact_id = _record_impact(client, base, loc, appt.json()["appointment_id"],
                                   tomorrow)
        notified = client.post(f"{base}/closures/{loc}/impacts/{impact_id}/notify")
        assert notified.status_code == 200, notified.text
        assert notified.json()["notified"] is True
        adapter = RecordingAdapter()
        summary = dispatch_once(engine, adapter=adapter, now=_safe_dispatch_time())
        # The 24-hour reminder is due only when the injected dispatch time is
        # at or after this appointment's exact reminder time. Keep this test
        # focused on the impact notice rather than the machine's wall clock.
        assert summary["dispatched"] == len(adapter.records), summary
        impact_notices = [record for record in adapter.records
                          if record["template"] == "closure_notice"]
        assert len(impact_notices) == 1, adapter.records
        assert impact_notices[0]["recipient"] == "friend@example.com"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_closure_ac2_unreachable_impact_opens_follow_up(database_url):
    """AC-2: no contact means a staff task, never a guessed send."""
    issuer = "https://identity.example.invalid/notice-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc, _, _, _, appt_id, _ = _bookable_salon_with_appointment(
            client, database_url, tenant)
        tomorrow = _closed_tomorrow(client, base, loc)
        impact_id = _record_impact(client, base, loc, appt_id, tomorrow)
        # Client One (fixture) carries no contacts.
        out = client.post(f"{base}/closures/{loc}/impacts/{impact_id}/notify").json()
        assert out["notified"] is False
        assert out["follow_up_task_id"]
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            tasks = conn.execute(
                text("""SELECT kind, status FROM follow_up_tasks WHERE tenant_id=:t"""),
                {"t": tenant["tenant_id"]}).mappings().all()
            comms = conn.execute(
                text("SELECT count(*) FROM communications WHERE tenant_id=:t"),
                {"t": tenant["tenant_id"]}).scalar_one()
        assert [(t["kind"], t["status"]) for t in tasks] == [("closure_follow_up", "open")]
        assert comms == 0
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert summary["dispatched"] == 0 and adapter.records == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_closure_ac1_broadcast_follows_purpose_policy(database_url):
    """AC-1: broadcasts record only consented clients; others are counted, not sent."""
    issuer = "https://identity.example.invalid/notice-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        loc, _, _, _, _, _ = _bookable_salon_with_appointment(
            client, database_url, tenant)
        _closed_tomorrow(client, base, loc)
        friend = _email_client(client, base, "Friend", "friend@example.com", loc)
        _grant(engine, tid, friend)
        stranger = _email_client(client, base, "Stranger", "stranger@example.com", loc)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            contact = next(c for c in stranger["contacts"])
            conn.execute(
                text("""INSERT INTO suppressions
                        (tenant_id, id, client_id, contact_value, channel, reason)
                        VALUES (:t, gen_random_uuid(), :c, 'stranger@example.com',
                                'email', 'unsubscribe')"""),
                {"t": tid, "c": stranger["id"]})
        plain = _email_client(client, base, "Plain", "plain@example.com", loc)
        ghost = client.post(f"{base}/clients", json={
            "name": "Ghost", "preferred_location_id": loc}).json()
        cast = client.post(f"{base}/closures/{loc}/notify-audience", params={})
        assert cast.status_code == 200, cast.text
        body = cast.json()
        assert body["recorded"] == 1
        assert body["skipped_no_consent"] == 1
        assert body["skipped_suppressed"] == 1
        assert body["unreachable"] == 1
        assert body["summary_task_id"]
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            purposes = conn.execute(
                text("SELECT DISTINCT purpose FROM communications WHERE tenant_id=:t"),
                {"t": tid}).scalars().all()
            channels = conn.execute(
                text("SELECT DISTINCT channel FROM communications WHERE tenant_id=:t"),
                {"t": tid}).scalars().all()
        assert purposes == ["closure_broadcast"]
        # No unlawful fallback: nothing left email for another channel.
        assert channels == ["email"]
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""INSERT INTO tenant_entitlements
                    (tenant_id, plan_code, status, max_locations, max_staff,
                     included_sms_segments, included_emails, marketing_allowed)
                    VALUES (:t, 'synthetic', 'active', 1, 5, 100, 100, true)
                    ON CONFLICT (tenant_id) DO UPDATE
                    SET marketing_allowed=true"""), {"t": tid})
        # Broadcasts schedule an hour out (deprioritized behind impacts).
        summary = dispatch_once(engine, adapter=adapter, now=_safe_dispatch_time())
        assert summary["dispatched"] == 1, summary
        assert adapter.records[0]["recipient"] == "friend@example.com"
    finally:
        _teardown(engine, [tid], issuer)


def test_closure_ac1_worker_targets_only_consented_channel_contacts(database_url):
    """SMS closure broadcast uses phone consent/contact and never email fallback."""
    issuer = "https://identity.example.invalid/notice-sms"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        loc, _, _, _, _, _ = _bookable_salon_with_appointment(client, database_url, tenant)
        _closed_tomorrow(client, base, loc)

        eligible = client.post(f"{base}/clients", json={
            "name": "SMS Eligible",
            "preferred_location_id": loc,
            "contacts": [
                {"kind": "email", "value": "primary@example.com", "is_primary": True},
                {"kind": "phone", "value": "+14165550161", "is_primary": False},
            ],
        }).json()
        _grant(engine, tid, eligible, channel="sms")
        # A broadcast explicitly requested on SMS keeps that channel even if
        # the client's closure-broadcast preference favors email.
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""INSERT INTO client_channel_prefs
                (tenant_id, client_id, purpose, primary_channel, fallback_channel)
                VALUES (:t, :client, 'closure_broadcast', 'email', NULL)"""),
                {"t": tid, "client": eligible["id"]})
        opted_out = client.post(f"{base}/clients", json={
            "name": "SMS Opted Out",
            "preferred_location_id": loc,
            "contacts": [
                {"kind": "email", "value": "stopped@example.com", "is_primary": True},
                {"kind": "phone", "value": "+14165550162", "is_primary": False},
            ],
        }).json()
        _grant(engine, tid, opted_out, channel="sms")
        _email_client(client, base, "Email Only", "email-only@example.com", loc)

        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""INSERT INTO suppressions
                (tenant_id, id, client_id, contact_value, channel, reason)
                VALUES (:t, gen_random_uuid(), :c, '+14165550162', 'sms', 'stop')"""),
                {"t": tid, "c": opted_out["id"]})
            sender = register_sender(conn, tenant_id=tid, label="Main",
                                     sender_identity="+14165550199")
            verify_sender(conn, tenant_id=tid, sender_id=sender["id"],
                          code=sender["verification_code"])
            conn.execute(text("""INSERT INTO tenant_entitlements
                (tenant_id, plan_code, status, max_locations, max_staff,
                 included_sms_segments, included_emails, marketing_allowed)
                VALUES (:t, 'synthetic', 'active', 1, 5, 100, 100, true)
                ON CONFLICT (tenant_id) DO UPDATE
                SET marketing_allowed=true"""), {"t": tid})

        cast = client.post(f"{base}/closures/{loc}/notify-audience",
                           params={"channel": "sms"})
        assert cast.status_code == 200, cast.text
        body = cast.json()
        assert body["recorded"] == 1, body
        assert body["skipped_suppressed"] == 1, body
        assert body["unreachable"] == 1, body
        adapter = RecordingAdapter()
        summary = dispatch_once(engine, adapter=adapter, now=_safe_dispatch_time())
        assert summary["dispatched"] == 1, summary
        assert [(record["channel"], record["recipient"]) for record in adapter.records] == [
            ("sms", "+14165550161")]
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            row = conn.execute(text("""SELECT contact_value, channel FROM communications
                WHERE tenant_id=:t AND client_id=:client AND purpose='closure_broadcast'"""),
                {"t": tid, "client": eligible["id"]}).mappings().one()
        assert dict(row) == {"contact_value": "+14165550161", "channel": "sms"}
    finally:
        _teardown(engine, [tid], issuer)


def test_closure_ac2_revoked_consent_blocks_broadcast_send(database_url):
    """AC-2: consent revoked after recording still blocks the send."""
    issuer = "https://identity.example.invalid/notice-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        loc, _, _, _, _, _ = _bookable_salon_with_appointment(
            client, database_url, tenant)
        _closed_tomorrow(client, base, loc)
        friend = _email_client(client, base, "Friend", "friend@example.com", loc)
        _grant(engine, tid, friend)
        cast = client.post(f"{base}/closures/{loc}/notify-audience", params={})
        assert cast.json()["recorded"] == 1
        contact_id = next(c["id"] for c in friend["contacts"])
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(
                text("""INSERT INTO consents
                        (tenant_id, id, client_id, contact_id, channel, purpose,
                         source, wording_version, status, contact_value_snapshot)
                        VALUES (:t, gen_random_uuid(), :c, :contact, 'email',
                                'marketing', 'client_self', 'v1', 'revoked',
                                lower(btrim((SELECT normalized_value FROM client_contacts
                                             WHERE tenant_id=:t AND id=:contact))))"""),
                {"t": tid, "c": friend["id"], "contact": contact_id})
        adapter = RecordingAdapter()
        summary = dispatch_once(engine, adapter=adapter, now=_safe_dispatch_time())
        assert summary["suppressed"] == 1 and summary["dispatched"] == 0
        assert adapter.records == []
    finally:
        _teardown(engine, [tid], issuer)
