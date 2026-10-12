"""Tests for WhatsApp ISV onboarding and approved template sends (TASK-031, FR-045).

AC-1: Per-salon sender status/templates/category/window rules enforced.
AC-2: Unapproved/restricted sender or template yields visible unsent task.
"""

import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.consent import check_suppressed, record_consent
from modules.communications.outbox import (
    dispatch_due_communications,
    record_booking_communication,
)
from modules.communications.senders import handle_inbound_sms, register_sender, verify_sender
from modules.communications.whatsapp import (
    WhatsAppModuleError,
    approve_sender,
    approve_template,
    create_template,
    register_wa_sender,
    submit_sender,
    submit_template,
    wa_window_open,
)
from test_clients import _salon, _client, _teardown
from test_communications import RecordingAdapter, _booked_appointment


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


def _approved_sender(conn, tenant_id, phone="+14165550901"):
    created = register_wa_sender(conn, tenant_id=tenant_id,
                                 business_name="Maple Salon", phone_number=phone)
    submit_sender(conn, tenant_id=tenant_id, sender_id=created["id"])
    return approve_sender(conn, tenant_id=tenant_id, sender_id=created["id"],
                          quality_rating="green", messaging_limit=1000)


def _approved_template(conn, tenant_id, name="reminder_24h", category="utility"):
    created = create_template(conn, tenant_id=tenant_id, name=name,
                              category=category, locale="en",
                              body="Hi {{1}}, your appointment is tomorrow at {{2}}.")
    submit_template(conn, tenant_id=tenant_id, template_id=created["id"])
    return approve_template(conn, tenant_id=tenant_id, template_id=created["id"])


def test_wa_ac1_onboarding_lifecycle_over_http(database_url):
    """AC-1: draft -> submitted -> approved with quality/limit visible."""
    issuer = "https://identity.example.invalid/wa-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/whatsapp-senders"
    try:
        created = client.post(base, json={"business_name": "Maple",
                                          "phone_number": "+14165550111"})
        assert created.status_code == 201, created.text
        assert created.json()["status"] == "draft"
        sender_id = created.json()["id"]
        early = client.post(f"{base}/{sender_id}/decide", json={})
        assert early.status_code == 422
        submitted = client.post(f"{base}/{sender_id}/submit")
        assert submitted.status_code == 200, submitted.text
        assert submitted.json()["status"] == "submitted"
        assert submitted.json()["isv_reference"]
        decided = client.post(f"{base}/{sender_id}/decide",
                              json={"quality_rating": "green", "messaging_limit": 1000})
        assert decided.status_code == 200, decided.text
        assert decided.json()["status"] == "approved"
        listed = client.get(base).json()["items"]
        assert len(listed) == 1
        assert listed[0]["quality_rating"] == "green"
        assert listed[0]["messaging_limit"] == 1000
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_wa_ac1_template_params_validated(database_url):
    """AC-1: gapped template parameters are rejected at creation."""
    issuer = "https://identity.example.invalid/wa-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            with pytest.raises(WhatsAppModuleError):
                create_template(conn, tenant_id=tenant["tenant_id"], name="bad",
                                category="utility", locale="en",
                                body="Hi {{1}}, code {{3}}.")
            ok = create_template(conn, tenant_id=tenant["tenant_id"], name="good",
                                 category="utility", locale="en",
                                 body="Hi {{1}}, code {{2}}.")
            assert ok["status"] == "draft"
            with pytest.raises(WhatsAppModuleError):
                create_template(conn, tenant_id=tenant["tenant_id"], name="good",
                                category="utility", locale="en", body="Dup {{1}}.")
            conn.commit()
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_wa_ac1_approved_template_sends_outside_window(database_url):
    """AC-1: approved sender + approved template routes with salon identity."""
    issuer = "https://identity.example.invalid/wa-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            sender = _approved_sender(conn, tenant["tenant_id"])
            template = _approved_template(conn, tenant["tenant_id"])
            assert wa_window_open(conn, tenant_id=tenant["tenant_id"],
                                  contact_value="+14165550101") is False
            row = record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value="+14165550101", channel="whatsapp",
                purpose="appointment_reminder_24h", entity_version=1,
                template_id=template["id"])
            assert row is not None
            conn.commit()
        finally:
            conn.close()
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert summary["dispatched"] == 1
        assert adapter.records[0]["sender"] == sender["phone_number"]
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            cost = conn.execute(
                text("SELECT cost_minor FROM message_costs WHERE tenant_id=:t"),
                {"t": tenant["tenant_id"]}).scalar_one()
        assert cost == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_wa_ac2_unapproved_sender_and_template_stay_unsent(database_url):
    """AC-2: restricted sender/template fail visibly; the row is the task."""
    issuer = "https://identity.example.invalid/wa-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            # Sender stuck at submitted: nothing may route.
            created = register_wa_sender(conn, tenant_id=tenant["tenant_id"],
                                         business_name="Maple", phone_number="+14165550222")
            submit_sender(conn, tenant_id=tenant["tenant_id"], sender_id=created["id"])
            record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value="+14165550101", channel="whatsapp",
                purpose="appointment_reminder_24h", entity_version=1)
            conn.commit()
        finally:
            conn.close()
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert summary["failed"] == 1 and summary["dispatched"] == 0
        assert adapter.records == []
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            state = conn.execute(
                text("SELECT state, skip_reason FROM communications WHERE tenant_id=:t"),
                {"t": tenant["tenant_id"]}).mappings().one()
        assert state["state"] == "failed"
        assert "no approved WhatsApp sender" in state["skip_reason"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_wa_ac2_red_quality_and_draft_template_blocked(database_url):
    """AC-2: red quality and draft templates are visible, unsent failures."""
    issuer = "https://identity.example.invalid/wa-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            created = register_wa_sender(conn, tenant_id=tenant["tenant_id"],
                                         business_name="Maple", phone_number="+14165550333")
            submit_sender(conn, tenant_id=tenant["tenant_id"], sender_id=created["id"])
            approve_sender(conn, tenant_id=tenant["tenant_id"],
                           sender_id=created["id"], quality_rating="red")
            draft = create_template(conn, tenant_id=tenant["tenant_id"], name="promo",
                                    category="marketing", locale="en",
                                    body="Sale {{1}}.")
            record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value="+14165550101", channel="whatsapp",
                purpose="appointment_reminder_24h", entity_version=1,
                template_id=draft["id"])
            conn.commit()
        finally:
            conn.close()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"],
                adapter=RecordingAdapter())
        assert summary["failed"] == 1
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            reason = conn.execute(
                text("SELECT skip_reason FROM communications WHERE tenant_id=:t"),
                {"t": tenant["tenant_id"]}).scalar_one()
        assert "quality is red" in reason
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_wa_ac1_free_reply_inside_open_window(database_url):
    """AC-1: a client message opens the window; the reply needs no template."""
    issuer = "https://identity.example.invalid/wa-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            _approved_sender(conn, tenant["tenant_id"], phone="+14165550444")
            # The client writes first: anything but STOP/HELP just opens state.
            opened = handle_inbound_sms(conn, sender_identity="+14165550444",
                                        from_value="+14165550101",
                                        body="Running 5 minutes late!",
                                        channel="whatsapp")
            assert opened["action"] == "logged"
            assert wa_window_open(conn, tenant_id=tenant["tenant_id"],
                                  contact_value="+14165550101") is True
            record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value="+14165550101", channel="whatsapp",
                purpose="appointment_reminder_2h", entity_version=1)
            conn.commit()
        finally:
            conn.close()
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert summary["dispatched"] == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_wa_ac2_stop_suppresses_whatsapp_only(database_url):
    """AC-2: a WhatsApp STOP blocks WhatsApp; SMS stays reachable."""
    issuer = "https://identity.example.invalid/wa-7"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            _approved_sender(conn, tenant["tenant_id"], phone="+14165550555")
            created = register_sender(conn, tenant_id=tenant["tenant_id"],
                                      label="SMS", sender_identity="+14165550666")
            verify_sender(conn, tenant_id=tenant["tenant_id"],
                          sender_id=created["id"], code=created["verification_code"])
            stopped = handle_inbound_sms(conn, sender_identity="+14165550555",
                                         from_value="+14165550101", body="STOP",
                                         channel="whatsapp")
            assert stopped["action"] == "suppressed"
            assert check_suppressed(conn, tenant_id=tenant["tenant_id"],
                                    contact_value="+14165550101",
                                    channel="whatsapp") is True
            assert check_suppressed(conn, tenant_id=tenant["tenant_id"],
                                    contact_value="+14165550101",
                                    channel="sms") is False
            record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value="+14165550101", channel="whatsapp",
                purpose="appointment_reminder_24h", entity_version=1)
            record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value="+14165550101", channel="sms",
                purpose="appointment_reminder_2h", entity_version=1)
            conn.commit()
        finally:
            conn.close()
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert summary["suppressed"] == 1 and summary["dispatched"] == 1
        assert adapter.records[0]["channel"] == "sms"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_wa_ac1_marketing_template_needs_consent(database_url):
    """AC-1: marketing templates ride the shared consent gate."""
    issuer = "https://identity.example.invalid/wa-8"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        _, _, _, client_id, appt_id, starts = _booked_appointment(client, tenant)
        created = client.post(base, json={
            "name": "WA Client",
            "contacts": [{"kind": "phone", "value": "+14165550101", "is_primary": True}],
        })
        assert created.status_code == 201, created.text
        contact_id = next(c["id"] for c in created.json()["contacts"] if c["kind"] == "phone")
        wa_client_id = created.json()["id"]
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            _approved_sender(conn, tenant_id=tenant["tenant_id"], phone="+14165550777")
            template = _approved_template(conn, tenant["tenant_id"],
                                          name="spring_offer", category="marketing")
            record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=wa_client_id,
                contact_value="+14165550101", channel="whatsapp",
                purpose="marketing", entity_version=1,
                template_id=template["id"])
            conn.commit()
        finally:
            conn.close()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            blocked = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"],
                adapter=RecordingAdapter())
        assert blocked["suppressed"] == 1
        # An explicit grant authorizes a later send of the same template. The
        # appointment is rescheduled first so the second send carries the new
        # version (stale versions never dispatch).
        resched = client.post(
            f"/v1/tenants/{tenant['tenant_id']}/appointments/{appt_id}/reschedule", json={
                "starts_at": (starts + timedelta(days=1)).isoformat(),
                "expected_version": 1, "idempotency_key": "wa-resched-0001"})
        assert resched.status_code == 200, resched.text
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            record_consent(conn, tenant_id=tenant["tenant_id"], client_id=wa_client_id,
                           contact_id=contact_id, channel="whatsapp",
                           purpose="marketing", source="client_self",
                           wording_version="v1")
            record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=wa_client_id,
                contact_value="+14165550101", channel="whatsapp",
                purpose="marketing", entity_version=2,
                template_id=template["id"])
            conn.commit()
        finally:
            conn.close()
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            allowed = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert allowed["dispatched"] == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
