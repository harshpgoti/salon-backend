"""Tests for offer redemption and attributed outcomes (TASK-040, FR-056).

AC-1: One appointment attribution and receipt linkage uses defined window.
AC-2: No causal lift claim or duplicate campaign revenue counts.
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.retention import run_rebooking_prompts
from modules.worker.communications import dispatch_once, dispatch_tenant_once
from test_booking_holds import _bookable, _first_slot
from test_clients import _salon, _client, _teardown
from test_rebooking_prompts import (
    _activate_rule, _book_complete, _client_with_email as _rebooking_client,
    _due_now, _grant as _grant_rebooking, _setup as _setup_rebooking,
)


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _email_client(client, base, name, email):
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
                     source, wording_version, status, contact_value_snapshot)
                    VALUES (:t, gen_random_uuid(), :c, :contact, :ch,
                            'marketing', 'client_self', 'v1', 'granted',
                            (SELECT normalized_value FROM client_contacts
                             WHERE tenant_id=:t AND id=:contact))"""),
            {"t": tid, "c": client_row["id"], "contact": contact["id"], "ch": channel})


def _live_campaign(client, engine, base, tid, day):
    os.environ.setdefault("WEB_BASE_URL", "http://127.0.0.1:3000")
    friend = _email_client(client, base, "Friend", "friend@example.com")
    _grant(engine, tid, friend)
    segment = client.post(f"{base}/segments", json={
        "name": "Everyone", "definition": {"inactive_days": 3650}}).json()["id"]
    drafted = client.post(f"{base}/campaigns", json={
        "name": "Spring", "segment_id": segment, "channel": "email",
        "offer_title": "Spring cut", "offer_terms": "20% off",
        "offer_code": "SPRING20", "template_body": "Hi! 20% off.",
        "budget_minor": 10000,
        "scheduled_at": (datetime.now(timezone.utc)
                         + timedelta(hours=1)).isoformat()})
    assert drafted.status_code == 201, drafted.text
    campaign_id = drafted.json()["id"]
    assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 200
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        conn.execute(text("""INSERT INTO tenant_entitlements
            (tenant_id, plan_code, status, max_locations, max_staff,
             included_sms_segments, included_emails, marketing_allowed)
            VALUES (:t, 'synthetic', 'active', 1, 5, 100, 100, true)
            ON CONFLICT (tenant_id) DO UPDATE SET marketing_allowed=true"""), {"t": tid})
        conn.execute(text("""INSERT INTO messaging_settings
            (tenant_id, quiet_start, quiet_end, emergency_stop)
            VALUES (:t, NULL, NULL, false)
            ON CONFLICT (tenant_id) DO UPDATE SET quiet_start=NULL, quiet_end=NULL,
                emergency_stop=false"""), {"t": tid})
    summary = dispatch_tenant_once(
        engine, tid, now=datetime.now(timezone.utc) + timedelta(hours=12))
    assert summary["dispatched"] == 1, json.dumps(summary, sort_keys=True)
    with engine.connect() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        token = conn.execute(
            text("""SELECT attribution_token FROM communications
                    WHERE tenant_id=:t AND purpose='marketing'"""),
            {"t": tid}).scalar_one()
    return campaign_id, friend, token


def _book_with_token(client, slot, day, client_id, token, key):
    base_b, slug, service, staff, _ = slot
    start = _first_slot(client, slug, service, day)
    hold = client.post(f"/v1/public/salons/{slug}/holds",
                       json={"service_id": service, "staff_id": staff,
                             "starts_at": start, "idempotency_key": f"atr-h-{key}"})
    assert hold.status_code == 201, hold.text
    confirmed = client.post(
        f"/v1/public/salons/{slug}/holds/{hold.json()['hold_id']}/confirm", json={
            "owner_token": hold.json()["owner_token"],
            "client_name": "Synthetic Attribution Booker", "email": "attribution@example.invalid",
            "policy_accepted": True, "policy_version": 1,
            "idempotency_key": f"atr-c-{key}", "attribution_token": token})
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()["appointment_id"]


def _appointment_client_id(engine, tenant_id, appointment_id):
    with engine.connect() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        return connection.execute(
            text("SELECT client_id::text FROM appointments WHERE tenant_id=:tenant AND id=:appointment"),
            {"tenant": tenant_id, "appointment": appointment_id}).scalar_one()


def test_attribution_ac1_tracked_booking_attributes_in_window(database_url):
    """AC-1: a token booking inside fourteen days attributes once with receipt."""
    issuer = "https://identity.example.invalid/atr-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    from datetime import date
    day = date.today() + timedelta(days=2)
    try:
        campaign_id, friend, token = _live_campaign(client, engine, base, tid, day)
        slot = _bookable(client, database_url, tenant, day)
        appt_id = _book_with_token(client, slot, day,
                                   friend["id"], token, "001")
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            rows = conn.execute(
                text("""SELECT campaign_id::text AS campaign, client_id::text AS client,
                               appointment_id::text AS appt FROM offer_redemptions
                        WHERE tenant_id=:t"""),
                {"t": tid}).mappings().all()
        assert len(rows) == 1
        assert rows[0]["campaign"] == campaign_id and rows[0]["appt"] == appt_id
        # A recorded receipt for the attributed booking links to the outcome.
        receipt = client.post(f"{base}/appointments/{appt_id}/receipt-entries", json={
            "kind": "payment", "amount_minor": 6000, "method": "card",
            "idempotency_key": "atr-receipt-001"})
        assert receipt.status_code == 201, receipt.text
        report = client.get(f"{base}/campaigns/{campaign_id}/report").json()
        assert report["attributed_count"] == 1
        assert report["attributed_recorded_minor"] == 6000
        assert report["attributed_bookings"][0]["appointment_id"] == appt_id
    finally:
        _teardown(engine, [tid], issuer)


def test_attribution_ac1_window_and_duplicates(database_url):
    """AC-1: attribution uses accepted-send time and a bounded seven-day window."""
    issuer = "https://identity.example.invalid/atr-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    from datetime import date
    day = date.today() + timedelta(days=2)
    try:
        campaign_id, friend, token = _live_campaign(client, engine, base, tid, day)
        slot = _bookable(client, database_url, tenant, day)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE communications SET dispatched_at = now() - interval '7 days 1 second'
                                 WHERE tenant_id=:t AND purpose='marketing'"""),
                         {"t": tid})
        appt_id = _book_with_token(client, slot, day,
                                   friend["id"], token, "002")
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            count = conn.execute(
                text("SELECT count(*) FROM offer_redemptions WHERE tenant_id=:t"),
                {"t": tid}).scalar_one()
        assert count == 0
        # A recent creation timestamp cannot rescue an expired dispatch token.
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE communications SET dispatched_at = now() - interval '6 days 23 hours'
                                 WHERE tenant_id=:t AND purpose='marketing'"""),
                         {"t": tid})
        appt2 = _book_with_token(client, slot, day,
                                 friend["id"], token, "003")
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE communications SET dispatched_at = now() + interval '1 day'
                                 WHERE tenant_id=:t AND purpose='marketing'"""),
                         {"t": tid})
        appt3 = _book_with_token(client, slot, day,
                                 friend["id"], token, "004")
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            count = conn.execute(
                text("SELECT count(*) FROM offer_redemptions WHERE tenant_id=:t"),
                {"t": tid}).scalar_one()
        assert count == 1
        # Manual redemption cannot claim the already-attributed appointment twice.
        booked_client_id = _appointment_client_id(engine, tid, appt2)
        again = client.post(f"{base}/redemptions", json={
            "campaign_id": campaign_id, "client_id": booked_client_id,
            "appointment_id": appt2})
        assert again.status_code == 409
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            per_appt = conn.execute(
                text("""SELECT appointment_id::text AS appt, count(*) FROM offer_redemptions
                        WHERE tenant_id=:t GROUP BY appointment_id"""),
                {"t": tid}).mappings().all()
        assert all(r["count"] == 1 for r in per_appt)
        assert appt3 != appt2
    finally:
        _teardown(engine, [tid], issuer)


def test_attribution_ac1_manual_code_redemption(database_url):
    """AC-1: staff record code redemptions; unknown codes and mismatches fail."""
    issuer = "https://identity.example.invalid/atr-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    from datetime import date
    day = date.today() + timedelta(days=2)
    try:
        campaign_id, friend, _ = _live_campaign(client, engine, base, tid, day)
        slot = _bookable(client, database_url, tenant, day)
        stranger = _email_client(client, base, "Stranger", "stranger@example.com")
        appt_id = _book_with_token(client, slot, day,
                                   stranger["id"], None, "004")
        booked_client_id = _appointment_client_id(engine, tid, appt_id)
        recorded = client.post(f"{base}/redemptions", json={
            "code": "SPRING20", "client_id": booked_client_id, "appointment_id": appt_id})
        assert recorded.status_code == 201, recorded.text
        assert client.post(f"{base}/redemptions", json={
            "code": "NOPE", "client_id": booked_client_id}).status_code == 422
        assert client.post(f"{base}/redemptions", json={
            "client_id": booked_client_id}).status_code == 422
        assert client.post(f"{base}/redemptions", json={
            "code": "SPRING20", "client_id": friend["id"],
            "appointment_id": appt_id}).status_code == 422
        again = client.post(f"{base}/redemptions", json={
            "code": "SPRING20", "client_id": booked_client_id,
            "appointment_id": appt_id})
        assert again.status_code == 409
    finally:
        _teardown(engine, [tid], issuer)


def test_attribution_ac2_report_describes_without_lift(database_url):
    """AC-2: delivery, unsubscribes, outcomes and costs — no lift, no ROI."""
    issuer = "https://identity.example.invalid/atr-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    from datetime import date
    day = date.today() + timedelta(days=2)
    try:
        campaign_id, friend, token = _live_campaign(client, engine, base, tid, day)
        slot = _bookable(client, database_url, tenant, day)
        _book_with_token(client, slot, day, friend["id"], token, "005")
        unrelated = _email_client(client, base, "Unrelated", "unrelated@example.com")
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""INSERT INTO suppressions
                (tenant_id, id, client_id, contact_value, channel, reason, source)
                VALUES (:t, gen_random_uuid(), :client, :contact, 'email',
                        'unsubscribe', 'client_self')"""),
                {"t": tid, "client": unrelated["id"],
                 "contact": "unrelated@example.com"})
        report = client.get(f"{base}/campaigns/{campaign_id}/report")
        assert report.status_code == 200, report.text
        blob = json.dumps(report.json()).lower()
        assert report.json()["sends_by_state"] == {"sent": 1}
        assert report.json()["attributed_count"] == 1
        assert report.json()["unsubscribes_since_approval"] == 0
        assert report.json()["currency"] == "CAD"
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""INSERT INTO suppressions
                (tenant_id, id, client_id, contact_value, channel, reason, source)
                VALUES (:t, gen_random_uuid(), :client, :contact, 'email',
                        'unsubscribe', 'client_self')"""),
                {"t": tid, "client": friend["id"],
                 "contact": "friend@example.com"})
        report_after_opt_out = client.get(f"{base}/campaigns/{campaign_id}/report")
        assert report_after_opt_out.json()["unsubscribes_since_approval"] == 1
        for banned in ("lift", "roi", "open_rate", "open rate", "incremental"):
            assert banned not in blob
    finally:
        _teardown(engine, [tid], issuer)


def test_one_appointment_cannot_be_attributed_to_offer_and_rebooking(database_url):
    """AC-2: a tracked return booking is not counted again as a campaign redemption."""
    issuer = "https://identity.example.invalid/atr-cross-source"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    base, service, staff, day = _setup_rebooking(client, tenant)
    try:
        os.environ.setdefault("WEB_BASE_URL", "http://127.0.0.1:3000")
        returning = _rebooking_client(client, base, "Returning", "returning@example.com")
        _grant_rebooking(engine, tid, returning)
        _activate_rule(client, base, service)
        _book_complete(client, base, service, staff, returning["id"], day, 10,
                       "atr-cross-complete")
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""INSERT INTO tenant_entitlements
                (tenant_id, plan_code, status, max_locations, max_staff,
                 included_sms_segments, included_emails, marketing_allowed)
                VALUES (:t, 'synthetic', 'active', 1, 5, 100, 100, true)
                ON CONFLICT (tenant_id) DO UPDATE SET marketing_allowed=true"""), {"t": tid})
            prompt_summary = run_rebooking_prompts(conn, tenant_id=tid, now=_due_now())
            assert prompt_summary["prompted"] == 1, {
                key: value for key, value in prompt_summary.items() if value
            }
        dispatch_tenant_once(engine, tid, now=_due_now())
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            token = conn.execute(text("""SELECT attribution_token FROM communications
                WHERE tenant_id=:t AND source_type='rebooking_cycle'"""), {"t": tid}).scalar_one()

        booking_day = day + timedelta(days=7)
        starts = datetime.combine(booking_day, datetime.min.time()).replace(
            hour=12, tzinfo=ZoneInfo("America/Toronto"))
        booked = client.post(f"{base}/appointments", json={
            "client_id": returning["id"], "staff_id": staff, "service_id": service,
            "starts_at": starts.isoformat(), "source": "staff",
            "idempotency_key": "atr-cross-booking-001", "attribution_token": token})
        assert booked.status_code == 201, booked.text
        appointment_id = booked.json()["appointment_id"]
        segment = client.post(f"{base}/segments", json={
            "name": "Offer audience", "definition": {"inactive_days": 3650}})
        campaign = client.post(f"{base}/campaigns", json={
            "name": "Offer", "segment_id": segment.json()["id"], "channel": "email",
            "offer_title": "Return offer", "offer_code": "RETURN10",
            "template_body": "Return offer", "budget_minor": 1000,
            "scheduled_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()})
        assert campaign.status_code == 201, campaign.text
        duplicate = client.post(f"{base}/redemptions", json={
            "campaign_id": campaign.json()["id"], "client_id": returning["id"],
            "appointment_id": appointment_id})
        assert duplicate.status_code == 409, duplicate.text
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            attribution = conn.execute(text("""SELECT kind FROM appointment_attributions
                WHERE tenant_id=:t AND appointment_id=:a"""),
                {"t": tid, "a": appointment_id}).scalar_one()
        assert attribution == "rebooking"
    finally:
        _teardown(engine, [tid], issuer)
