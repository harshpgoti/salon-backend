"""Tests for offer redemption and attributed outcomes (TASK-040, FR-056).

AC-1: One appointment attribution and receipt linkage uses defined window.
AC-2: No causal lift claim or duplicate campaign revenue counts.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.campaigns import run_campaigns
from test_booking_holds import _bookable, _first_slot
from test_clients import _salon, _client, _teardown


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
                     source, wording_version, status)
                    VALUES (:t, gen_random_uuid(), :c, :contact, :ch,
                            'marketing', 'client_self', 'v1', 'granted')"""),
            {"t": tid, "c": client_row["id"], "contact": contact["id"], "ch": channel})


def _live_campaign(client, engine, base, tid, day):
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
        summary = run_campaigns(
            connection=conn, tenant_id=tid,
            now=datetime.now(timezone.utc) + timedelta(hours=2))
    assert summary["sent"] == 1, summary
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
            "owner_token": hold.json()["owner_token"], "client_id": client_id,
            "idempotency_key": f"atr-c-{key}", "attribution_token": token})
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()["appointment_id"]


def test_attribution_ac1_tracked_booking_attributes_in_window(database_url):
    """AC-1: a token booking inside fourteen days attributes once with receipt."""
    issuer = "https://identity.example.invalid/atr-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    from datetime import date
    day = date.today() + timedelta(days=1)
    try:
        campaign_id, friend, token = _live_campaign(client, engine, base, tid, day)
        slot = _bookable(client, database_url, tenant)
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
    """AC-1: stale tokens do not attribute; one appointment attributes once."""
    issuer = "https://identity.example.invalid/atr-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    from datetime import date
    day = date.today() + timedelta(days=1)
    try:
        campaign_id, friend, token = _live_campaign(client, engine, base, tid, day)
        slot = _bookable(client, database_url, tenant)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE communications SET created_at = now() - interval '20 days'
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
        # Fresh token, same appointment twice: the second claim changes nothing.
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE communications SET created_at = now()
                                 WHERE tenant_id=:t AND purpose='marketing'"""),
                         {"t": tid})
        appt2 = _book_with_token(client, slot, day,
                                 friend["id"], token, "003")
        again = client.post(f"{base}/redemptions", json={
            "campaign_id": campaign_id, "client_id": friend["id"],
            "appointment_id": appt2})
        assert again.status_code == 409
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            per_appt = conn.execute(
                text("""SELECT appointment_id::text AS appt, count(*) FROM offer_redemptions
                        WHERE tenant_id=:t GROUP BY appointment_id"""),
                {"t": tid}).mappings().all()
        assert all(r["count"] == 1 for r in per_appt)
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
    day = date.today() + timedelta(days=1)
    try:
        campaign_id, friend, _ = _live_campaign(client, engine, base, tid, day)
        slot = _bookable(client, database_url, tenant)
        stranger = _email_client(client, base, "Stranger", "stranger@example.com")
        appt_id = _book_with_token(client, slot, day,
                                   stranger["id"], None, "004")
        recorded = client.post(f"{base}/redemptions", json={
            "code": "SPRING20", "client_id": stranger["id"], "appointment_id": appt_id})
        assert recorded.status_code == 201, recorded.text
        assert client.post(f"{base}/redemptions", json={
            "code": "NOPE", "client_id": stranger["id"]}).status_code == 422
        assert client.post(f"{base}/redemptions", json={
            "client_id": stranger["id"]}).status_code == 422
        assert client.post(f"{base}/redemptions", json={
            "code": "SPRING20", "client_id": friend["id"],
            "appointment_id": appt_id}).status_code == 422
        again = client.post(f"{base}/redemptions", json={
            "code": "SPRING20", "client_id": stranger["id"],
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
    day = date.today() + timedelta(days=1)
    try:
        campaign_id, friend, token = _live_campaign(client, engine, base, tid, day)
        slot = _bookable(client, database_url, tenant)
        _book_with_token(client, slot, day, friend["id"], token, "005")
        report = client.get(f"{base}/campaigns/{campaign_id}/report")
        assert report.status_code == 200, report.text
        blob = json.dumps(report.json()).lower()
        assert report.json()["sends_by_state"] == {"sent": 1}
        assert report.json()["attributed_count"] == 1
        assert report.json()["currency"] == "CAD"
        for banned in ("lift", "roi", "open_rate", "open rate", "incremental"):
            assert banned not in blob
    finally:
        _teardown(engine, [tid], issuer)
