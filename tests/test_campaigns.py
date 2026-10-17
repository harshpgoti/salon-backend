"""Tests for budget-bounded offer campaigns (TASK-039, FR-054/FR-055/FR-057).

AC-1: Draft/test/approval locks template audience budget and scheduled version.
AC-2: Pause/cancel/caps stop future dispatch; send-time opt-out still skipped.
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.campaigns import run_campaigns
from modules.budgets import CHANNEL_RATES
from modules import segments
from modules.communications.senders import register_sender, verify_sender
from modules.communications.whatsapp import (
    approve_sender, approve_template, create_template, register_wa_sender,
    submit_sender, submit_template,
)
from modules.worker.communications import dispatch_once, dispatch_tenant_once
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
                     source, wording_version, status, contact_value_snapshot)
                    VALUES (:t, gen_random_uuid(), :c, :contact, :ch,
                            'marketing', 'client_self', 'v1', 'granted',
                            (SELECT normalized_value FROM client_contacts
                             WHERE tenant_id=:t AND id=:contact))"""),
            {"t": tid, "c": client_row["id"], "contact": contact["id"], "ch": channel})


def _segment(client, base, name="Everyone"):
    created = client.post(f"{base}/segments", json={
        "name": name, "definition": {"inactive_days": 3650}})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _draft(client, base, segment_id, channel="email", budget=10000,
           title="Spring cut", schedule_hours=1, whatsapp_template_id=None):
    body = {
        "name": "Spring", "segment_id": segment_id, "channel": channel,
        "offer_title": title, "offer_terms": "20% off cuts",
        "offer_code": "SPRING20", "template_body": "Hi! 20% off cuts.",
        "budget_minor": budget,
        "scheduled_at": (datetime.now(timezone.utc)
                         + timedelta(hours=schedule_hours)).isoformat()}
    if whatsapp_template_id:
        body["whatsapp_template_id"] = whatsapp_template_id
    return client.post(f"{base}/campaigns", json=body)


class RecordingCampaignAdapter:
    def __init__(self):
        self.records = []

    def send(self, **kwargs):
        self.records.append(kwargs)
        return {"status": "recorded", "id": f"campaign-{len(self.records)}"}


def test_campaign_approval_serializes_concurrent_draft_edit(database_url, monkeypatch):
    """AC-1: audience and approved campaign terms come from one draft version."""
    issuer = "https://identity.example.invalid/camp-approval-race"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    updater = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        friend = _email_client(client, base, "Friend", "approval-race@example.com")
        _grant(engine, tid, friend)
        _allow_marketing(engine, tid)
        original_segment = _segment(client, base, "Original audience")
        restricted = client.post(f"{base}/segments", json={
            "name": "Changed audience", "definition": {"min_spend_minor": 1}})
        assert restricted.status_code == 201, restricted.text
        campaign_id = _draft(client, base, original_segment, budget=10000).json()["id"]

        resolving = Event()
        continue_approval = Event()
        real_resolve = segments.resolve_segment

        def pause_during_resolution(*args, **kwargs):
            resolving.set()
            assert continue_approval.wait(timeout=10)
            return real_resolve(*args, **kwargs)

        monkeypatch.setattr(segments, "resolve_segment", pause_during_resolution)
        with ThreadPoolExecutor(max_workers=2) as pool:
            approval = pool.submit(client.post, f"{base}/campaigns/{campaign_id}/approve")
            assert resolving.wait(timeout=10), "approval did not resolve its audience"
            edit = pool.submit(updater.patch, f"{base}/campaigns/{campaign_id}", json={
                "expected_version": 1, "segment_id": restricted.json()["id"],
                "budget_minor": 20000})
            try:
                assert edit.result(timeout=0.2).status_code == 409
            except TimeoutError:
                pass
            finally:
                continue_approval.set()
            approved = approval.result(timeout=10)
            edit_response = edit.result(timeout=10)

        assert approved.status_code == 200, approved.text
        assert approved.json()["eligible"] == 1
        assert edit_response.status_code == 409, edit_response.text
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            snapshot = conn.execute(text("""SELECT snapshot_budget_minor,
                    audience_snapshot FROM offer_campaigns
                    WHERE tenant_id=:t AND id=:campaign"""),
                {"t": tid, "campaign": campaign_id}).mappings().one()
        assert snapshot["snapshot_budget_minor"] == 10000
        assert snapshot["audience_snapshot"] == [friend["id"]]
    finally:
        _teardown(engine, [tid], issuer)


def _allow_marketing(engine, tid):
    os.environ.setdefault("WEB_BASE_URL", "http://127.0.0.1:3000")
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        conn.execute(text("""INSERT INTO tenant_entitlements
            (tenant_id, plan_code, status, max_locations, max_staff,
             included_sms_segments, included_emails, marketing_allowed)
            VALUES (:t, 'synthetic', 'active', 1, 5, 100, 100, true)
            ON CONFLICT (tenant_id) DO UPDATE SET marketing_allowed=true"""), {"t": tid})
        # These cases verify campaign approval and attribution, not quiet-hour
        # scheduling. Make dispatch deterministic regardless of the local clock.
        conn.execute(text("""INSERT INTO messaging_settings
            (tenant_id, quiet_start, quiet_end, emergency_stop)
            VALUES (:t, NULL, NULL, false)
            ON CONFLICT (tenant_id) DO UPDATE SET quiet_start=NULL, quiet_end=NULL,
                emergency_stop=false"""), {"t": tid})


def _run(engine, tid, hours=2, limit=200):
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        return run_campaigns(connection=conn, tenant_id=tid,
                             now=datetime.now(timezone.utc) + timedelta(hours=hours),
                             limit=limit)


def test_campaign_ac1_approval_locks_and_worker_sends_snapshot(database_url):
    """AC-1: approval freezes audience/template/budget/schedule for the worker."""
    issuer = "https://identity.example.invalid/camp-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        friend = _email_client(client, base, "Friend", "friend@example.com")
        _grant(engine, tid, friend)
        _allow_marketing(engine, tid)
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
        adapter = RecordingCampaignAdapter()
        moment = datetime.now(timezone.utc) + timedelta(hours=12)
        summary = dispatch_tenant_once(engine, tid, adapter=adapter, now=moment)
        assert summary["campaign_queued"] == 1 and summary["campaigns_started"] == 1
        assert summary["dispatched"] == 1, summary
        assert summary["campaigns_completed"] == 1, summary
        assert len(adapter.records) == 1
        assert adapter.records[0]["template"] == "campaign_offer"
        assert adapter.records[0]["message_body"] == "Hi! 20% off cuts."
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            comm = conn.execute(
                text("""SELECT purpose, channel, state, attribution_token,
                               source_type, template_subject_snapshot,
                               template_body_snapshot FROM communications WHERE tenant_id=:t"""),
                {"t": tid}).mappings().one()
            status = conn.execute(
                text("SELECT status FROM offer_campaigns WHERE tenant_id=:t"),
                {"t": tid}).scalar_one()
        assert comm["purpose"] == "marketing" and comm["channel"] == "email"
        assert comm["state"] == "dispatched" and comm["source_type"] == "campaign"
        assert comm["attribution_token"]
        assert comm["template_subject_snapshot"] is None
        assert comm["template_body_snapshot"] == "Hi! 20% off cuts."
        assert status == "completed"
    finally:
        _teardown(engine, [tid], issuer)


def test_campaign_worker_resumes_after_a_bounded_batch(database_url):
    """AC-1/AC-2: a sending campaign drains its remaining queue on later polls."""
    issuer = "https://identity.example.invalid/camp-resume"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        for name, email in (("First", "first@example.com"),
                            ("Second", "second@example.com")):
            row = _email_client(client, base, name, email)
            _grant(engine, tid, row)
        segment_id = _segment(client, base)
        campaign_id = _draft(client, base, segment_id).json()["id"]
        assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 200

        first = _run(engine, tid, limit=1)
        assert first["sent"] == 1
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            state = conn.execute(text(
                "SELECT status FROM offer_campaigns WHERE tenant_id=:t"),
                {"t": tid}).scalar_one()
            queued = conn.execute(text("""SELECT count(*) FROM campaign_sends
                WHERE tenant_id=:t AND campaign_id=:c AND state='queued'"""),
                {"t": tid, "c": campaign_id}).scalar_one()
        assert state == "sending" and queued == 1

        second = _run(engine, tid, limit=1)
        assert second["sent"] == 1
        assert second["completed"] == 1
    finally:
        _teardown(engine, [tid], issuer)


def test_campaign_budget_counts_prior_dispatch_once_across_batches(database_url):
    """AC-2: a prior dispatched send is not charged twice in the next batch."""
    issuer = "https://identity.example.invalid/camp-budget-pages"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        for name, phone in (("First", "+14165550601"),
                            ("Second", "+14165550602")):
            row = _phone_client(client, base, name, phone)
            _grant(engine, tid, row, channel="sms")
        _allow_marketing(engine, tid)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            sender = register_sender(conn, tenant_id=tid, label="Synthetic",
                                     sender_identity="+14165550699")
            verify_sender(conn, tenant_id=tid, sender_id=sender["id"],
                          code=sender["verification_code"])
        budget = 2 * CHANNEL_RATES["sms"]
        campaign_id = _draft(client, base, _segment(client, base), channel="sms",
                             budget=budget).json()["id"]
        assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 200

        adapter = RecordingCampaignAdapter()
        moment = datetime.now(timezone.utc) + timedelta(hours=12)
        first = dispatch_tenant_once(engine, tid, adapter=adapter, now=moment, limit=1)
        assert first["dispatched"] == 1
        second = dispatch_tenant_once(engine, tid, adapter=adapter, now=moment, limit=1)
        assert second["dispatched"] == 1, second
        assert len(adapter.records) == 2
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            status = conn.execute(text(
                "SELECT status FROM offer_campaigns WHERE tenant_id=:t"),
                {"t": tid}).scalar_one()
            sends = conn.execute(text("SELECT state FROM campaign_sends WHERE tenant_id=:t"),
                                 {"t": tid}).scalars().all()
        assert status == "completed" and sends == ["sent", "sent"]
    finally:
        _teardown(engine, [tid], issuer)


def test_campaign_pause_defers_recorded_message_until_resume(database_url):
    """AC-2: pause blocks outbox dispatch; resume releases the same snapshot."""
    issuer = "https://identity.example.invalid/camp-pause"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        friend = _email_client(client, base, "Friend", "pause@example.com")
        _grant(engine, tid, friend)
        _allow_marketing(engine, tid)
        campaign_id = _draft(client, base, _segment(client, base)).json()["id"]
        assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 200
        assert _run(engine, tid)["sent"] == 1
        paused = client.post(f"{base}/campaigns/{campaign_id}/pause")
        assert paused.status_code == 200 and paused.json()["status"] == "paused"

        adapter = RecordingCampaignAdapter()
        moment = datetime.now(timezone.utc) + timedelta(hours=12)
        first = dispatch_tenant_once(engine, tid, adapter=adapter, now=moment)
        assert first["deferred"] == 1 and adapter.records == []
        resumed = client.post(f"{base}/campaigns/{campaign_id}/resume")
        assert resumed.status_code == 200 and resumed.json()["status"] == "completed"

        second = dispatch_tenant_once(engine, tid, adapter=adapter, now=moment)
        assert second["dispatched"] == 1
        assert adapter.records[0]["message_body"] == "Hi! 20% off cuts."
    finally:
        _teardown(engine, [tid], issuer)


def test_campaign_cancel_after_enqueue_skips_recorded_messages(database_url):
    """AC-2: cancellation after queueing blocks messages already in the outbox."""
    issuer = "https://identity.example.invalid/camp-cancel-enqueued"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        friend = _email_client(client, base, "Friend", "cancel@example.com")
        _grant(engine, tid, friend)
        _allow_marketing(engine, tid)
        campaign_id = _draft(client, base, _segment(client, base)).json()["id"]
        assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 200
        assert _run(engine, tid)["sent"] == 1
        cancelled = client.post(f"{base}/campaigns/{campaign_id}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"

        adapter = RecordingCampaignAdapter()
        result = dispatch_tenant_once(
            engine, tid, adapter=adapter,
            now=datetime.now(timezone.utc) + timedelta(hours=12))
        assert result["dispatched"] == 0 and adapter.records == []
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            state = conn.execute(text("SELECT state FROM communications WHERE tenant_id=:t"),
                                 {"t": tid}).scalar_one()
            send_state = conn.execute(text("SELECT state FROM campaign_sends WHERE tenant_id=:t"),
                                      {"t": tid}).scalar_one()
        assert state == "skipped_stale" and send_state == "skipped_cancelled"
    finally:
        _teardown(engine, [tid], issuer)


def test_campaign_opt_out_after_queue_is_checked_at_dispatch(database_url):
    """AC-2: an opt-out after enqueue still suppresses the campaign send."""
    issuer = "https://identity.example.invalid/camp-opt-out"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        friend = _email_client(client, base, "Friend", "optout@example.com")
        _grant(engine, tid, friend)
        _allow_marketing(engine, tid)
        campaign_id = _draft(client, base, _segment(client, base)).json()["id"]
        assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 200
        assert _run(engine, tid)["sent"] == 1
        contact_id = friend["contacts"][0]["id"]
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""INSERT INTO consents
                (tenant_id, id, client_id, contact_id, channel, purpose, source,
                 wording_version, status, contact_value_snapshot)
                VALUES (:t, gen_random_uuid(), :client, :contact, 'email',
                        'marketing', 'client_self', 'v1', 'revoked',
                        (SELECT normalized_value FROM client_contacts
                         WHERE tenant_id=:t AND id=:contact))"""),
                {"t": tid, "client": friend["id"], "contact": contact_id})

        adapter = RecordingCampaignAdapter()
        result = dispatch_tenant_once(
            engine, tid, adapter=adapter,
            now=datetime.now(timezone.utc) + timedelta(hours=12))
        assert result["suppressed"] == 1 and adapter.records == []
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            send_state = conn.execute(text("SELECT state FROM campaign_sends WHERE tenant_id=:t"),
                                      {"t": tid}).scalar_one()
        assert send_state == "skipped_consent"
    finally:
        _teardown(engine, [tid], issuer)


def test_campaign_whatsapp_template_snapshot_fails_closed_if_changed(database_url):
    """AC-1/AC-2: the approved WhatsApp template version reaches dispatch intact."""
    issuer = "https://identity.example.invalid/camp-wa-snapshot"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tid = tenant["tenant_id"]
    try:
        friend = _phone_client(client, base, "Friend", "+14165550888")
        _grant(engine, tid, friend, channel="whatsapp")
        _allow_marketing(engine, tid)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            sender = register_wa_sender(conn, tenant_id=tid,
                                        business_name="Synthetic Salon",
                                        phone_number="+14165550777")
            submit_sender(conn, tenant_id=tid, sender_id=sender["id"])
            approve_sender(conn, tenant_id=tid, sender_id=sender["id"])
            template = create_template(conn, tenant_id=tid, name="spring_offer",
                                        category="marketing", locale="en_CA",
                                        body="Spring offer {{1}}")
            template["id"] = str(template["id"])
            submit_template(conn, tenant_id=tid, template_id=template["id"])
            approve_template(conn, tenant_id=tid, template_id=template["id"])
            approved_version = conn.execute(text(
                "SELECT version FROM whatsapp_templates WHERE tenant_id=:t AND id=:id"),
                {"t": tid, "id": template["id"]}).scalar_one()

        campaign_id = _draft(client, base, _segment(client, base), channel="whatsapp",
                             whatsapp_template_id=template["id"]).json()["id"]
        assert client.post(f"{base}/campaigns/{campaign_id}/approve").status_code == 200
        assert _run(engine, tid)["sent"] == 1
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            snapshot = conn.execute(text("""SELECT snapshot_whatsapp_template_id::text,
                    snapshot_whatsapp_template_version, snapshot_whatsapp_template_name,
                    snapshot_whatsapp_template_body FROM offer_campaigns
                    WHERE tenant_id=:t AND id=:campaign"""),
                {"t": tid, "campaign": campaign_id}).mappings().one()
            communication = conn.execute(text("""SELECT template_id::text,
                    template_version, template_body_snapshot FROM communications
                    WHERE tenant_id=:t AND source_type='campaign'"""),
                {"t": tid}).mappings().one()
            conn.execute(text("""UPDATE whatsapp_templates
                SET body='Changed template {{1}}', version=version+1
                WHERE tenant_id=:t AND id=:id"""), {"t": tid, "id": template["id"]})
        assert snapshot["snapshot_whatsapp_template_id"] == template["id"]
        assert snapshot["snapshot_whatsapp_template_version"] == approved_version
        assert snapshot["snapshot_whatsapp_template_name"] == "spring_offer"
        assert snapshot["snapshot_whatsapp_template_body"] == "Spring offer {{1}}"
        assert communication["template_id"] == template["id"]
        assert communication["template_version"] == approved_version

        adapter = RecordingCampaignAdapter()
        result = dispatch_tenant_once(
            engine, tid, adapter=adapter,
            now=datetime.now(timezone.utc) + timedelta(hours=12))
        assert result["failed"] == 1 and adapter.records == []
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            state = conn.execute(text("SELECT state FROM communications WHERE tenant_id=:t"),
                                 {"t": tid}).scalar_one()
        assert state == "failed"
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
        assert states == ["recorded", "skipped_budget"]
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
                         source, wording_version, status, contact_value_snapshot)
                        VALUES (:t, gen_random_uuid(), :c, :contact, 'email',
                                'marketing', 'client_self', 'v1', 'granted',
                                (SELECT normalized_value FROM client_contacts
                                 WHERE tenant_id=:t AND id=:contact))"""),
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
        _allow_marketing(engine, tid)
        segment_id = _segment(client, base)
        campaign_id = _draft(client, base, segment_id).json()["id"]
        tested = client.post(f"{base}/campaigns/{campaign_id}/test",
                             json={"client_id": friend["id"]})
        assert tested.status_code == 200, tested.text
        assert tested.json()["attribution_token"]
        test_adapter = RecordingCampaignAdapter()
        test_result = dispatch_tenant_once(
            engine, tid, adapter=test_adapter,
            now=datetime.now(timezone.utc) + timedelta(hours=12))
        assert test_result["dispatched"] == 1
        assert test_adapter.records[0]["message_body"] == "Hi! 20% off cuts."
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
                         source, wording_version, status, contact_value_snapshot)
                        VALUES (:t, gen_random_uuid(), :c, :contact, 'email',
                                'marketing', 'client_self', 'v1', 'revoked',
                                (SELECT normalized_value FROM client_contacts
                                 WHERE tenant_id=:t AND id=:contact))"""),
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
