"""Tests for campaign and message cost performance (TASK-044, FR-061/FR-062).

AC-1: Accepted/delivered/failure/opt-outs/attribution/cost distinct and reconciled.
AC-2: Unknown delivery/stale projection labelled; no email-open ROI claim.
"""

import json
import os
import sys
import uuid
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.callbacks import ingest_callback, sign_callback_body
from test_clients import _salon, _client, _teardown

SECRET = "test-mkt-secret-0123456789abcdef01"


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
        "name": "Cut", "duration_minutes": 60, "price_minor": 6000}).json()["id"]
    staff = client.post(f"{base}/staff", json={"display_name": "Ava"}).json()["id"]
    client.put(f"{base}/staff/{staff}/locations", json={"location_ids": [loc]})
    client.put(f"{base}/staff/{staff}/qualifications", json={"service_ids": [service]})
    return base, loc, service, staff, day


def _email_client(client, base, name, email):
    created = client.post(f"{base}/clients", json={
        "name": name,
        "contacts": [{"kind": "email", "value": email, "is_primary": True}],
    })
    assert created.status_code == 201, created.text
    return created.json()


def _grant(engine, tid, client_row, status="granted"):
    contact = next(c for c in client_row["contacts"])
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        conn.execute(
            text("""INSERT INTO consents
                    (tenant_id, id, client_id, contact_id, channel, purpose,
                     source, wording_version, status, contact_value_snapshot, revoked_at)
                    VALUES (:t, gen_random_uuid(), :c, :contact, 'email',
                            'marketing', 'client_self', 'v1', :status,
                            (SELECT normalized_value FROM client_contacts
                             WHERE tenant_id=:t AND id=:contact),
                            CASE WHEN :status='revoked' THEN now() ELSE NULL END)"""),
            {"t": tid, "c": client_row["id"], "contact": contact["id"],
             "status": status})


def test_marketing_ac1_sections_reconcile(database_url):
    """AC-1: delivery, callbacks, opt-outs, campaigns, automation, costs agree."""
    issuer = "https://identity.example.invalid/mkt-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        base, loc, service, staff, day = _setup(client, tenant)
        shift = client.post(f"{base}/staff/{staff}/shifts", json={
            "location_id": loc, "weekday": day.weekday(),
            "starts_at": "09:00", "ends_at": "17:00"})
        assert shift.status_code in (200, 201), shift.text
        friend = _email_client(client, base, "Friend", "friend@example.com")
        failed_recipient = _email_client(client, base, "Failed", "failed@example.com")
        _grant(engine, tid, friend)
        starts_at = (datetime.combine(day, datetime.min.time())
                     + timedelta(hours=14)).replace(
                         tzinfo=ZoneInfo("America/Toronto"))
        booking = client.post(f"{base}/appointments", json={
            "client_id": friend["id"], "staff_id": staff, "service_id": service,
            "starts_at": starts_at.isoformat(), "source": "staff",
            "idempotency_key": "mkt-attributed-booking-1"})
        assert booking.status_code == 201, booking.text
        segment = client.post(f"{base}/segments", json={
            "name": "Everyone", "definition": {"inactive_days": 3650}}).json()["id"]
        drafted = client.post(f"{base}/campaigns", json={
            "name": "Spring", "segment_id": segment, "channel": "email",
            "offer_title": "T", "template_body": "B", "budget_minor": 10000,
            "scheduled_at": (datetime.now(timezone.utc)
                             + timedelta(hours=1)).isoformat()})
        campaign_id = drafted.json()["id"]
        assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 200
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE offer_campaigns SET created_at=now()-interval '10 days'
                WHERE tenant_id=:t AND id=:campaign"""),
                {"t": tid, "campaign": campaign_id})
            conn.execute(text("""UPDATE communications SET state='suppressed'
                WHERE tenant_id=:t AND source_type='appointment' AND state='recorded'"""),
                {"t": tid})
            conn.execute(
                text("""INSERT INTO provider_webhook_secrets (tenant_id, channel, secret)
                        VALUES (:t, 'email', :s) ON CONFLICT DO NOTHING"""),
                {"t": tid, "s": SECRET})
            comm_id = str(uuid.uuid4())
            conn.execute(text("""INSERT INTO communications
                (tenant_id, id, client_id, contact_value, channel, purpose, source_type,
                 source_id, expected_entity_version, state, dispatched_at, dedupe_key)
                VALUES (:t, :comm, :client, 'friend@example.com', 'email', 'marketing',
                        'campaign', :campaign, 1, 'dispatched', now(), 'mkt-dispatched-1')"""),
                {"t": tid, "comm": comm_id, "client": friend["id"],
                 "campaign": campaign_id})
            failed_id = str(uuid.uuid4())
            conn.execute(text("""INSERT INTO communications
                (tenant_id, id, client_id, contact_value, channel, purpose, source_type,
                 source_id, expected_entity_version, state, dispatched_at, dedupe_key)
                VALUES (:t, :comm, :client, 'failed@example.com', 'email', 'marketing',
                        'campaign', :campaign, 1, 'dispatched', now(), 'mkt-failed-1')"""),
                {"t": tid, "comm": failed_id, "client": failed_recipient["id"],
                 "campaign": campaign_id})
            conn.execute(text("""UPDATE campaign_sends SET communication_id=:comm, state='sent'
                WHERE tenant_id=:t AND campaign_id=:campaign AND client_id=:client"""),
                {"t": tid, "comm": comm_id, "campaign": campaign_id,
                 "client": friend["id"]})
            conn.execute(text("""INSERT INTO usage_reservations
                (tenant_id, communication_id, estimated_minor, actual_minor, state)
                VALUES (:t, :comm, 0, 245, 'reconciled')"""),
                {"t": tid, "comm": comm_id})
            conn.execute(text("""INSERT INTO message_costs
                (tenant_id, communication_id, channel, cost_minor)
                VALUES (:t, :comm, 'email', 245)"""),
                {"t": tid, "comm": comm_id})
            body = b"{}"
            ingest_callback(conn, tenant_id=tid, channel="email", raw_body=body,
                            signature=sign_callback_body(SECRET, body),
                            event="accepted", provider_event_id="mkt-evt-accepted",
                            communication_id=comm_id)
            ingest_callback(conn, tenant_id=tid, channel="email", raw_body=body,
                            signature=sign_callback_body(SECRET, body),
                            event="delivered", provider_event_id="mkt-evt-1",
                            communication_id=comm_id)
            ingest_callback(conn, tenant_id=tid, channel="email", raw_body=body,
                            signature=sign_callback_body(SECRET, body),
                            event="delivered", provider_event_id="mkt-evt-duplicate",
                            communication_id=comm_id)
            ingest_callback(conn, tenant_id=tid, channel="email", raw_body=body,
                            signature=sign_callback_body(SECRET, body),
                            event="failed", provider_event_id="mkt-evt-failed",
                            communication_id=failed_id)
            conn.execute(text("""INSERT INTO provider_usage
                (tenant_id, provider_ref, communication_id, segments, cost_minor, state)
                VALUES (:t, 'mkt-usage-1', :comm, 1, 245, 'matched')"""),
                {"t": tid, "comm": comm_id})
            # Write the consent event before taking the suppression lock: the
            # helper opens a separate connection, which cannot re-enter a
            # transaction-scoped advisory lock held by this connection.
            _grant(engine, tid, friend, status="revoked")
            from modules.communications.consent import record_suppression
            record_suppression(conn, tenant_id=tid, client_id=friend["id"],
                               contact_value="friend@example.com", channel="email",
                               reason="unsubscribe")
            conn.execute(
                text("""INSERT INTO offer_redemptions
                        (tenant_id, id, campaign_id, client_id, communication_id, appointment_id)
                        VALUES (:t, gen_random_uuid(), :c, :client, :comm, :appointment)"""),
                {"t": tid, "c": campaign_id, "client": friend["id"],
                 "comm": comm_id, "appointment": booking.json()["appointment_id"]})
        params = {"date_from": date.today().isoformat(),
                  "date_to": (date.today() + timedelta(days=1)).isoformat()}
        report = client.get(f"/v1/tenants/{tid}/reports/marketing", params=params)
        assert report.status_code == 200, report.text
        body = report.json()
        delivered = [r for r in body["delivery_by_channel_state"]
                     if r["channel"] == "email" and r["state"] == "delivered"]
        assert delivered and delivered[0]["count"] == 1
        failed = [r for r in body["delivery_by_channel_state"]
                  if r["channel"] == "email" and r["state"] == "failed"]
        assert failed and failed[0]["count"] == 1
        assert body["provider_events"] == {"accepted": 1, "delivered": 1, "failed": 1}
        assert body["unknown_delivery"] == 0
        assert body["unsubscribes_by_reason"] == {"unsubscribe": 1}
        assert body["consents_revoked"] == 1
        campaign = next(c for c in body["campaigns"] if c["id"] == campaign_id)
        assert campaign["sent"] == 1 and campaign["attributed"] == 1
        assert campaign["provider_actual_cost_minor"] == 245
        assert campaign["estimated_cost_minor"] == 0
        assert body["automation"]["prompt_attributed_bookings"] == 0
        assert body["message_costs"]["by_channel"][0]["channel"] == "email"
        assert body["message_costs"]["by_channel"][0]["provider_actual_minor"] == 245
        assert body["message_costs"]["by_channel"][0]["estimated_minor"] == 0
        assert body["freshness"]["source"] == "canonical PostgreSQL tables"
        assert body["freshness"]["stale"] is False
        assert body["audiences"][0]["eligible"] == 0
    finally:
        _teardown(engine, [tid], issuer)


def test_marketing_ac2_unknown_labelled_and_scoped(database_url):
    """AC-2: evidence-free sends stay unknown; tenants never cross-read."""
    issuer = "https://identity.example.invalid/mkt-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        base, loc, service, staff, day = _setup(client, tenant)
        friend = _email_client(client, base, "Friend", "friend@example.com")
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(
                text("""INSERT INTO communications
                        (tenant_id, id, client_id, contact_value, channel, purpose,
                         source_type, source_id, expected_entity_version,
                         state, dispatched_at, dedupe_key)
                        VALUES (:t, gen_random_uuid(), :c, 'friend@example.com',
                                'email', 'booking_confirmation', 'appointment',
                                gen_random_uuid(), 1, 'dispatched', now(), :dedupe)"""),
                {"t": tid, "c": friend["id"], "dedupe": "mkt-unknown-001"})
            unknown_id = conn.execute(
                text("SELECT id::text FROM communications WHERE tenant_id=:t AND dedupe_key=:key"),
                {"t": tid, "key": "mkt-unknown-001"}).scalar_one()
            conn.execute(text("""INSERT INTO usage_reservations
                (tenant_id, communication_id, estimated_minor, state)
                VALUES (:t, :comm, 12, 'reserved')"""),
                {"t": tid, "comm": unknown_id})
        params = {"date_from": (date.today() - timedelta(days=1)).isoformat(),
                  "date_to": (date.today() + timedelta(days=1)).isoformat()}
        body = client.get(f"/v1/tenants/{tid}/reports/marketing",
                          params=params).json()
        assert body["unknown_delivery"] == 1
        assert body["message_costs"]["by_channel"][0]["pending_estimate_minor"] == 12
        assert body["freshness"]["stale"] is False
        blob = json.dumps(body).lower()
        for banned in ('"lift":', '"roi":', "open_rate", "incremental"):
            assert banned not in blob
        issuer_b = "https://identity.example.invalid/mkt-2b"
        engine_b, owner_b, tenant_b = _salon(database_url, issuer_b)
        try:
            other = client.get(
                f"/v1/tenants/{tenant_b['tenant_id']}/reports/marketing",
                params=params)
            assert other.status_code == 404
        finally:
            _teardown(engine_b, [tenant_b["tenant_id"]], issuer_b)
    finally:
        _teardown(engine, [tid], issuer)
