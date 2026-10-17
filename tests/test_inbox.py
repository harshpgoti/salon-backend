"""Tests for the minimal inbound inbox and follow-up tasks (TASK-034, FR-048).

AC-1: Incoming consent/help/reply events create scoped client follow-up tasks.
AC-2: Permission/window limits and redaction apply to reply handling.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.callbacks import sign_callback_body
from modules.communications.consent import check_suppressed
from modules.communications.inbox import (
    create_task_for_inbound,
    list_inbox,
)
from modules.communications.senders import (
    handle_inbound_sms,
    register_sender,
    verify_sender,
)
from modules.communications.outbox import dispatch_due_communications
from modules.communications.whatsapp import approve_sender, register_wa_sender, submit_sender
from test_clients import _salon, _client, _teardown

SECRET = "test-inbox-secret-0123456789abcdef01"


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _scoped(engine, tenant_id):
    conn = engine.connect()
    conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
    return conn


def _sms_sender(conn, tenant_id, identity="+14165550801"):
    created = register_sender(conn, tenant_id=tenant_id, label="Main",
                              sender_identity=identity)
    verify_sender(conn, tenant_id=tenant_id, sender_id=created["id"],
                  code=created["verification_code"])
    return created


def _client_with_phone(client, base, value="+14165550101"):
    created = client.post(base, json={
        "name": "Inbox Client",
        "contacts": [{"kind": "phone", "value": value, "is_primary": True}],
    })
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_inbox_ac1_stop_help_reply_create_tasks(database_url):
    """AC-1: consent/help/reply events each open their own scoped task."""
    issuer = "https://identity.example.invalid/inbox-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        client_id = _client_with_phone(client, base)
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            _sms_sender(conn, tenant["tenant_id"])
            stop = handle_inbound_sms(conn, sender_identity="+14165550801",
                                      from_value="+14165550101", body="STOP")
            assert stop["action"] == "suppressed"
            stop_task = create_task_for_inbound(
                conn, tenant_id=tenant["tenant_id"], inbound_id=stop["inbound_id"])
            assert stop_task["kind"] == "opt_out"
            assert stop_task["status"] == "open"
            # Reprocessing the same message returns the same task, not a twin.
            assert create_task_for_inbound(
                conn, tenant_id=tenant["tenant_id"],
                inbound_id=stop["inbound_id"])["id"] == stop_task["id"]
            assert check_suppressed(conn, tenant_id=tenant["tenant_id"],
                                    contact_value="+14165550101", channel="sms") is True
            helped = handle_inbound_sms(conn, sender_identity="+14165550801",
                                        from_value="+14165550101", body="HELP")
            help_task = create_task_for_inbound(
                conn, tenant_id=tenant["tenant_id"], inbound_id=helped["inbound_id"])
            assert help_task["kind"] == "help_request"
            replied = handle_inbound_sms(conn, sender_identity="+14165550801",
                                         from_value="+14165550101",
                                         body="Do you validate parking?")
            reply_task = create_task_for_inbound(
                conn, tenant_id=tenant["tenant_id"], inbound_id=replied["inbound_id"])
            assert reply_task["kind"] == "inbound_reply"
            tasks = list_inbox(conn, tenant_id=tenant["tenant_id"])
            assert len(tasks) == 3
            assert {t["kind"] for t in tasks} == {"opt_out", "help_request", "inbound_reply"}
            # Every task links the same resolved client.
            assert {t["client_id"] for t in tasks} == {client_id}
            conn.commit()
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_inbox_ac1_unknown_sender_creates_nothing(database_url):
    """AC-1: unknown sender or client identities never create follow-up tasks."""
    issuer = "https://identity.example.invalid/inbox-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            out = handle_inbound_sms(conn, sender_identity="+19995550000",
                                     from_value="+14165550101", body="STOP")
            assert out == {"action": "unresolved", "tenant_id": None}
            _sms_sender(conn, tenant["tenant_id"], identity="+14165550812")
            unknown = handle_inbound_sms(conn, sender_identity="+14165550812",
                from_value="+14165550112", body="STOP")
            assert create_task_for_inbound(conn, tenant_id=tenant["tenant_id"],
                                           inbound_id=unknown["inbound_id"]) is None
            assert check_suppressed(conn, tenant_id=tenant["tenant_id"],
                contact_value="+14165550112", channel="sms") is True
            for name in ("Shared Client One", "Shared Client Two"):
                created = client.post(f"/v1/tenants/{tenant['tenant_id']}/clients", json={
                    "name": name, "contacts": [{"kind": "phone",
                        "value": "+14165550113", "is_primary": True}]})
                assert created.status_code == 201, created.text
            ambiguous = handle_inbound_sms(conn, sender_identity="+14165550812",
                from_value="+14165550113", body="Can someone call me?")
            assert create_task_for_inbound(conn, tenant_id=tenant["tenant_id"],
                                           inbound_id=ambiguous["inbound_id"]) is None
            assert list_inbox(conn, tenant_id=tenant["tenant_id"]) == []
            conn.commit()
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_inbox_ac1_http_inbound_is_signed_and_tasked(database_url):
    """AC-1: the provider webhook verifies HMAC and opens the task."""
    issuer = "https://identity.example.invalid/inbox-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        _client_with_phone(client, base, value="+14165550202")
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            _sms_sender(conn, tenant["tenant_id"], identity="+14165550802")
            conn.execute(
                text("""INSERT INTO provider_webhook_secrets (tenant_id, channel, secret)
                        VALUES (:t, 'sms', :s) ON CONFLICT DO NOTHING"""),
                {"t": tenant["tenant_id"], "s": SECRET})
        payload = {"sender_identity": "+14165550802", "from_value": "+14165550202",
                   "body": "STOP", "channel": "sms"}
        raw = json.dumps(payload).encode()
        ok = client.post(f"/v1/tenants/{tenant['tenant_id']}/inbound/sms", content=raw,
                         headers={"Content-Type": "application/json",
                                  "X-Provider-Signature": sign_callback_body(SECRET, raw)})
        assert ok.status_code == 200, ok.text
        assert ok.json()["action"] == "suppressed"
        assert ok.json()["task_id"]
        forged = client.post(f"/v1/tenants/{tenant['tenant_id']}/inbound/sms", content=raw,
                             headers={"Content-Type": "application/json",
                                      "X-Provider-Signature": "0" * 64})
        assert forged.status_code == 401
        listed = client.get(f"/v1/tenants/{tenant['tenant_id']}/inbox")
        assert listed.status_code == 200
        assert [t["kind"] for t in listed.json()["items"]] == ["opt_out"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_inbox_ac2_list_hides_message_and_sender_content_and_resolve_is_audited(database_url):
    """AC-2: list exposes only static triage labels; resolution is audited."""
    issuer = "https://identity.example.invalid/inbox-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        _client_with_phone(client, base, value="+14165550303")
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            _sms_sender(conn, tenant["tenant_id"], identity="+14165550803")
            long_body = "Please call me back about " + "the invoice " * 30
            assert len(long_body) > 140
            handled = handle_inbound_sms(conn, sender_identity="+14165550803",
                                         from_value="+14165550303", body=long_body)
            task = create_task_for_inbound(
                conn, tenant_id=tenant["tenant_id"], inbound_id=handled["inbound_id"])
            conn.commit()
        finally:
            conn.close()
        listed = client.get(f"/v1/tenants/{tenant['tenant_id']}/inbox").json()["items"]
        assert len(listed) == 1
        item = listed[0]
        assert item["client_name"] == "Inbox Client"
        assert item["detail"] == "Client replied; message content hidden"
        assert "from_value" not in item
        assert "+14165550303" not in json.dumps(item)
        assert "invoice" not in json.dumps(item)
        assert "body" not in item
        resolved = client.post(
            f"/v1/tenants/{tenant['tenant_id']}/inbox/{task['id']}/resolve",
            json={"resolution": "done", "note": "Called back, rebooked."})
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["status"] == "done"
        repeat = client.post(
            f"/v1/tenants/{tenant['tenant_id']}/inbox/{task['id']}/resolve",
            json={"resolution": "done"})
        assert repeat.status_code == 422
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            audit = conn.execute(
                text("""SELECT action FROM tenant_audit_events
                        WHERE tenant_id=:t AND resource_id=:r"""),
                {"t": tenant["tenant_id"], "r": task["id"]}).scalars().all()
        assert "inbox.task_resolved" in audit
        assert client.get(f"/v1/tenants/{tenant['tenant_id']}/inbox").json()["items"] == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_inbox_reply_is_bounded_idempotent_and_redacted(database_url):
    """AC-3/AC-5: staff reply retries are safe and never echo content."""
    issuer = "https://identity.example.invalid/inbox-reply"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        _client_with_phone(client, base, value="+14165550404")
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            _sms_sender(conn, tenant["tenant_id"], identity="+14165550804")
            inbound = handle_inbound_sms(conn, sender_identity="+14165550804",
                                         from_value="+14165550404", body="Can I book Friday?")
            task = create_task_for_inbound(conn, tenant_id=tenant["tenant_id"],
                                           inbound_id=inbound["inbound_id"])
            conn.commit()
        finally:
            conn.close()
        path = f"/v1/tenants/{tenant['tenant_id']}/inbox/{task['id']}/reply"
        headers = {"Idempotency-Key": "support-reply-001"}
        payload = {"body": "Yes, Friday has openings. What time works?"}
        sent = client.post(path, json=payload, headers=headers)
        assert sent.status_code == 200, sent.text
        assert set(sent.json()) == {"id", "state"}
        replay = client.post(path, json=payload, headers=headers)
        assert replay.status_code == 200
        assert replay.json()["id"] == sent.json()["id"]
        conflict = client.post(path, json={"body": "A different answer"}, headers=headers)
        assert conflict.status_code == 409
        oversized = client.post(path, json={"body": "x" * 2001},
                                headers={"Idempotency-Key": "support-reply-002"})
        assert oversized.status_code == 422
        assert payload["body"] not in sent.text
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_inbox_whatsapp_reply_window_is_checked_at_queue_and_dispatch(database_url):
    """AC-4/AC-5: WhatsApp replies require the 24h window at both seams."""
    issuer = "https://identity.example.invalid/inbox-wa-window"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        client_id = _client_with_phone(client,
            f"/v1/tenants/{tenant['tenant_id']}/clients", value="+14165550505")
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            sender = register_wa_sender(conn, tenant_id=tenant["tenant_id"],
                business_name="Maple Salon", phone_number="+14165550805")
            submit_sender(conn, tenant_id=tenant["tenant_id"], sender_id=sender["id"])
            approve_sender(conn, tenant_id=tenant["tenant_id"], sender_id=sender["id"],
                           quality_rating="green", messaging_limit=1000)
            inbound = handle_inbound_sms(conn, sender_identity="+14165550805",
                from_value="+14165550505", body="What time do you close?", channel="whatsapp")
            task = create_task_for_inbound(conn, tenant_id=tenant["tenant_id"],
                                           inbound_id=inbound["inbound_id"])
            conn.commit()
        finally:
            conn.close()
        path = f"/v1/tenants/{tenant['tenant_id']}/inbox/{task['id']}/reply"
        response = client.post(path, json={"body": "We close at six."},
                               headers={"Idempotency-Key": "wa-reply-key-001"})
        assert response.status_code == 200, response.text
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            stored = conn.execute(text("""SELECT reply_body, state FROM communications
                WHERE tenant_id=:t AND id=:id"""),
                {"t": tenant["tenant_id"], "id": response.json()["id"]}).mappings().one()
            assert stored["reply_body"] == "We close at six."
            conn.execute(text("""UPDATE communications SET expires_at=:expired
                WHERE tenant_id=:t AND id=:id"""),
                {"t": tenant["tenant_id"], "id": response.json()["id"],
                 "expired": datetime.now(timezone.utc) - timedelta(seconds=1)})
            summary = dispatch_due_communications(conn, tenant_id=tenant["tenant_id"],
                adapter=type("Sink", (), {"send": lambda self, **kwargs: {"id": "sent"}})())
            assert summary["skipped_expired"] == 1
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            conn.execute(text("""UPDATE inbound_messages SET received_at=:old
                WHERE tenant_id=:t AND id=:id"""),
                {"t": tenant["tenant_id"], "id": inbound["inbound_id"],
                 "old": datetime.now(timezone.utc) - timedelta(hours=25)})
        closed = client.post(
            f"/v1/tenants/{tenant['tenant_id']}/inbox/{task['id']}/reply",
            json={"body": "Too late"}, headers={"Idempotency-Key": "wa-reply-key-002"})
        assert closed.status_code == 422
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_inbox_ac2_read_is_role_scoped():
    """AC-2: triage reading is receptionist-visible; resolving is manager-level."""
    from modules.identity.main import ROLE_PERMISSIONS
    assert "inbox.read" in ROLE_PERMISSIONS["manager"]
    assert "inbox.read" in ROLE_PERMISSIONS["receptionist"]
    assert "inbox.read" not in ROLE_PERMISSIONS["stylist"]
    assert "communications.manage" in ROLE_PERMISSIONS["manager"]
    assert "communications.manage" not in ROLE_PERMISSIONS["receptionist"]
