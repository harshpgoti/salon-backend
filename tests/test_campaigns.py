"""Tests for budget-bounded offer campaigns (TASK-039, FR-054/FR-055/FR-057).

AC-1: Draft/test/approval locks template audience budget and scheduled version.
AC-2: Pause/cancel/caps stop future dispatch; send-time opt-out still skipped.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.campaigns import run_campaigns
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


def _phone_client(client, base, name, phone):
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
                     source, wording_version, status)
                    VALUES (:t, gen_random_uuid(), :c, :contact, :ch,
                            'marketing', 'client_self', 'v1', 'granted')"""),
            {"t": tid, "c": client_row["id"], "contact": contact["id"], "ch": channel})


def _segment(client, base, name="Everyone"):
    created = client.post(f"{base}/segments", json={
        "name": name, "definition": {"inactive_days": 3650}})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _draft(client, base, segment_id, channel="email", budget=10000,
           title="Spring cut", schedule_hours=1):
    return client.post(f"{base}/campaigns", json={
        "name": "Spring", "segment_id": segment_id, "channel": channel,
        "offer_title": title, "offer_terms": "20% off cuts",
        "offer_code": "SPRING20", "template_body": "Hi! 20% off cuts.",
        "budget_minor": budget,
        "scheduled_at": (datetime.now(timezone.utc)
                         + timedelta(hours=schedule_hours)).isoformat()})


def _run(engine, tid, hours=2):
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        return run_campaigns(connection=conn, tenant_id=tid,
                             now=datetime.now(timezone.utc) + timedelta(hours=hours))


def test_campaign_ac1_approval_locks_and_sends(database_url):
    """AC-1: approval freezes audience/budget/schedule; the run completes."""
    issuer = "https://identity.example.invalid/camp-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        friend = _email_client(client, base, "Friend", "friend@example.com")
        _grant(engine, tid, friend)
        _email_client(client, base, "Stranger", "stranger@example.com")
        segment_id = _segment(client, base)
        drafted = _draft(client, base, segment_id)
        assert drafted.status_code == 201, drafted.text
        campaign_id = drafted.json()["id"]
        approved = client.post(f"{base}/campaigns/{campaign_id}/approve")
        assert approved.status_code == 200, approved.text
        body = approved.json()
        assert body["status"] == "scheduled" and body["eligible"] == 1
        assert body["exclusions"]["no_consent"] == 1
        summary = _run(engine, tid)
        assert summary["sent"] == 1 and summary["campaigns_started"] == 1
        assert summary["completed"] == 1
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            comm = conn.execute(
                text("""SELECT purpose, channel, state, attribution_token,
                               source_type FROM communications WHERE tenant_id=:t"""),
                {"t": tid}).mappings().one()
            status = conn.execute(
                text("SELECT status FROM offer_campaigns WHERE tenant_id=:t"),
                {"t": tid}).scalar_one()
        assert comm["purpose"] == "marketing" and comm["channel"] == "email"
        assert comm["state"] == "recorded" and comm["source_type"] == "campaign"
        assert comm["attribution_token"]
        assert status == "completed"
    finally:
        _teardown(engine, [tid], issuer)


def test_campaign_ac2_cancel_stops_future_sends(database_url):
    """AC-2: cancelling drains nothing further; sent rows keep their state."""
    issuer = "https://identity.example.invalid/camp-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        for name, email in (("A", "a@example.com"), ("B", "b@example.com")):
            row = _email_client(client, base, name, email)
            _grant(engine, tid, row)
        segment_id = _segment(client, base)
        campaign_id = _draft(client, base, segment_id).json()["id"]
        assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 200
        assert client.post(f"{base}/campaigns/{campaign_id}/cancel").status_code == 200
        summary = _run(engine, tid)
        assert summary["sent"] == 0 and summary["campaigns_started"] == 0
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            states = conn.execute(
                text("SELECT DISTINCT state FROM campaign_sends WHERE tenant_id=:t"),
                {"t": tid}).scalars().all()
            status = conn.execute(
                text("SELECT status FROM offer_campaigns WHERE tenant_id=:t"),
                {"t": tid}).scalar_one()
        assert states == ["skipped_cancelled"] and status == "cancelled"
    finally:
        _teardown(engine, [tid], issuer)


def test_campaign_ac2_budget_and_frequency_caps(database_url):
    """AC-2: the approved budget and the cross-message frequency cap hold."""
    issuer = "https://identity.example.invalid/camp-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        row_a = _phone_client(client, base, "A", "+14165550101")
        _grant(engine, tid, row_a, channel="sms")
        row_b = _phone_client(client, base, "B", "+14165550202")
        _grant(engine, tid, row_b, channel="sms")
        segment_id = _segment(client, base, name="SmsCrowd")
        campaign_id = _draft(client, base, segment_id, channel="sms",
                             budget=1, title="Tiny").json()["id"]
        assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 200
        summary = _run(engine, tid)
        assert summary["sent"] == 1
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            states = sorted(conn.execute(
                text("SELECT state FROM campaign_sends WHERE tenant_id=:t"),
                {"t": tid}).scalars().all())
        assert states == ["sent", "skipped_budget"]
        # Frequency: A already heard four marketing sends in the window, and
        # holds an email grant, so the email crowd skips A but not for consent.
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(
                text("""INSERT INTO client_contacts
                        (tenant_id, id, client_id, kind, normalized_value,
                         display_value, is_primary, source)
                        VALUES (:t, gen_random_uuid(), :c, 'email',
                                'a@example.com', 'a@example.com', false, 'staff')"""),
                {"t": tid, "c": row_a["id"]})
        email_contact = None
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            email_contact = conn.execute(
                text("""SELECT id::text AS id FROM client_contacts
                        WHERE tenant_id=:t AND client_id=:c AND kind='email'"""),
                {"t": tid, "c": row_a["id"]}).scalar_one()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(
                text("""INSERT INTO consents
                        (tenant_id, id, client_id, contact_id, channel, purpose,
                         source, wording_version, status)
                        VALUES (:t, gen_random_uuid(), :c, :contact, 'email',
                                'marketing', 'client_self', 'v1', 'granted')"""),
                {"t": tid, "c": row_a["id"], "contact": email_contact})
            for n in range(4):
                conn.execute(
                    text("""INSERT INTO communications
                            (tenant_id, id, client_id, contact_value, channel, purpose,
                             source_type, source_id, expected_entity_version,
                             state, dedupe_key)
                            VALUES (:t, gen_random_uuid(), :c, 'a@example.com',
                                    'email', 'marketing', 'appointment',
                                    gen_random_uuid(), 1, 'dispatched', :dedupe)"""),
                    {"t": tid, "c": row_a["id"], "dedupe": f"freq-probe-{n}"})
        crowded = _draft(client, base, segment_id, channel="email",
                         budget=10000, title="Crowded")
        crowded_id = crowded.json()["id"]
        assert client.post(f"{base}/campaigns/{crowded_id}/approve").status_code == 200
        second = _run(engine, tid)
        assert second["sent"] == 0
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            freq = conn.execute(
                text("""SELECT state FROM campaign_sends
                        WHERE tenant_id=:t AND campaign_id=:c"""),
                {"t": tid, "c": crowded_id}).scalars().all()
        assert freq == ["skipped_frequency"]
    finally:
        _teardown(engine, [tid], issuer)


def test_campaign_ac1_test_send_and_revalidation(database_url):
    """AC-1/AC-2: tests ride the real gates; revoked consent skips at run time."""
    issuer = "https://identity.example.invalid/camp-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        friend = _email_client(client, base, "Friend", "friend@example.com")
        _grant(engine, tid, friend)
        segment_id = _segment(client, base)
        campaign_id = _draft(client, base, segment_id).json()["id"]
        tested = client.post(f"{base}/campaigns/{campaign_id}/test",
                             json={"client_id": friend["id"]})
        assert tested.status_code == 200, tested.text
        assert tested.json()["attribution_token"]
        stranger = _email_client(client, base, "Stranger", "stranger@example.com")
        refused = client.post(f"{base}/campaigns/{campaign_id}/test",
                              json={"client_id": stranger["id"]})
        assert refused.status_code == 422
        assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 200
        contact_id = next(c["id"] for c in friend["contacts"])
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(
                text("""INSERT INTO consents
                        (tenant_id, id, client_id, contact_id, channel, purpose,
                         source, wording_version, status)
                        VALUES (:t, gen_random_uuid(), :c, :contact, 'email',
                                'marketing', 'client_self', 'v1', 'revoked')"""),
                {"t": tid, "c": friend["id"], "contact": contact_id})
        summary = _run(engine, tid)
        assert summary["sent"] == 0
    finally:
        _teardown(engine, [tid], issuer)


def test_campaign_ac1_permissions_and_locks(database_url):
    """AC-1: drafting vs sending powers differ; approvals lock mechanics."""
    issuer = "https://identity.example.invalid/camp-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        segment_id = _segment(client, base)
        assert _draft(client, base, "00000000-0000-0000-0000-000000000000").status_code == 422
        assert _draft(client, base, segment_id, channel="pigeon").status_code == 422
        unscheduled = client.post(f"{base}/campaigns", json={
            "name": "S", "segment_id": segment_id, "channel": "email",
            "offer_title": "T", "template_body": "B", "budget_minor": 10})
        assert unscheduled.status_code == 201, unscheduled.text
        campaign_id = unscheduled.json()["id"]
        assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 422
        assert client.patch(f"{base}/campaigns/{campaign_id}", json={
            "expected_version": 1, "budget_minor": 5}).status_code == 200
        assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 422
        scheduled = client.patch(f"{base}/campaigns/{campaign_id}", json={
            "expected_version": 2,
            "scheduled_at": (datetime.now(timezone.utc)
                             + timedelta(hours=1)).isoformat()})
        assert scheduled.status_code == 200, scheduled.text
        assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 200
        locked = client.patch(f"{base}/campaigns/{campaign_id}", json={
            "expected_version": 3, "budget_minor": 50000})
        assert locked.status_code == 409
        from test_clients import _identity
        outsider = _client(database_url, _identity())
        assert outsider.post(f"{base}/campaigns", json={
            "name": "S", "segment_id": segment_id, "channel": "email",
            "offer_title": "T", "template_body": "B",
            "budget_minor": 10}).status_code == 404
    finally:
        _teardown(engine, [tid], issuer)
