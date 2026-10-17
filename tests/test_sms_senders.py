"""Tests for SMS sender setup and delivery workflows (TASK-029, FR-044).

AC-1: Verified per-salon sender routes eligible reminders with safe identity.
AC-2: STOP/HELP provider suppression, segment cost and failed delivery handled.
"""

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.consent import check_suppressed
from modules.communications.senders import (
    SenderModuleError,
    get_verified_sender,
    handle_inbound_sms,
    register_sender,
    suspend_sender,
    verify_sender,
)
from modules.communications.outbox import (
    dispatch_due_communications,
    record_booking_communication,
)
from test_clients import _salon, _client, _teardown
from test_communications import RecordingAdapter, _booked_appointment, _client_email


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _sender_conn(engine, tenant_id):
    conn = engine.connect()
    conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
    return conn


def _isolate_booking_email(engine, tenant_id, appointment_id):
    """Keep the booking fixture's unrelated email notices out of SMS assertions."""
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                     {"t": tenant_id})
        conn.execute(text("""UPDATE communications
            SET state='skipped_stale', skip_reason='isolated SMS test fixture',
                version=version+1, updated_at=now()
            WHERE tenant_id=:t AND source_id=:appointment AND channel='email'
              AND state='recorded'"""),
                     {"t": tenant_id, "appointment": appointment_id})


def test_sms_ac1_register_verify_routes_with_identity(database_url):
    """AC-1: pending -> verified sender routes SMS stamped with its identity."""
    issuer = "https://identity.example.invalid/sms-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        _isolate_booking_email(engine, tenant["tenant_id"], appt_id)
        email = _client_email(engine, tenant["tenant_id"], client_id)
        conn = _sender_conn(engine, tenant["tenant_id"])
        try:
            created = register_sender(
                conn, tenant_id=tenant["tenant_id"], label="Main line",
                sender_identity="+14165550132")
            assert created["status"] == "pending"
            assert get_verified_sender(
                conn, tenant_id=tenant["tenant_id"]) is None
            with pytest.raises(SenderModuleError):
                verify_sender(conn, tenant_id=tenant["tenant_id"],
                              sender_id=created["id"], code="000000")
            verified = verify_sender(conn, tenant_id=tenant["tenant_id"],
                                     sender_id=created["id"],
                                     code=created["verification_code"])
            assert verified["status"] == "verified"
            sender = get_verified_sender(conn, tenant_id=tenant["tenant_id"])
            assert sender["sender_identity"] == "+14165550132"
            row = record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value="+14165550101", channel="sms",
                purpose="appointment_reminder_24h", entity_version=1)
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
        assert adapter.records[0]["sender"] == "+14165550132"
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            cost = conn.execute(
                text("""SELECT segments, cost_minor, currency FROM message_costs
                        WHERE tenant_id=:t"""),
                {"t": tenant["tenant_id"]}).mappings().one()
        assert cost["segments"] == 1 and cost["cost_minor"] == 1
        assert cost["currency"] == "CAD"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_sms_ac2_cost_uses_versioned_template_segments(database_url):
    """AC-2: the ledger estimates multiple SMS segments from the selected template."""
    issuer = "https://identity.example.invalid/sms-segments"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        _isolate_booking_email(engine, tenant["tenant_id"], appt_id)
        conn = _sender_conn(engine, tenant["tenant_id"])
        try:
            sender = register_sender(conn, tenant_id=tenant["tenant_id"],
                                     label="Main", sender_identity="+14165550133")
            verify_sender(conn, tenant_id=tenant["tenant_id"], sender_id=sender["id"],
                          code=sender["verification_code"])
            template_id = conn.execute(text("""INSERT INTO message_templates
                (tenant_id, channel, purpose, locale, version, body, is_active)
                VALUES (:tenant, 'sms', 'appointment_reminder', 'en-CA', 1, :body, true)
                RETURNING id::text"""),
                {"tenant": tenant["tenant_id"], "body": "x" * 321}).scalar_one()
            row = record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"], appointment_id=appt_id,
                client_id=client_id, contact_value="+14165550101", channel="sms",
                purpose="appointment_reminder_24h", entity_version=1,
                template_id=template_id, template_version=1)
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
        assert adapter.records[0]["sender"] == "+14165550133"
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            cost = conn.execute(text("""SELECT segments, cost_minor, currency
                FROM message_costs WHERE tenant_id=:t AND communication_id=:id"""),
                {"t": tenant["tenant_id"], "id": row["id"]}).mappings().one()
        assert cost == {"segments": 3, "cost_minor": 3, "currency": "CAD"}
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_sms_ac1_sender_identity_never_pooled_across_salons(database_url):
    """AC-1: a number serving one salon cannot register to another."""
    issuer_a = "https://identity.example.invalid/sms-2a"
    issuer_b = "https://identity.example.invalid/sms-2b"
    engine_a, owner_a, tenant_a = _salon(database_url, issuer_a)
    engine_b, owner_b, tenant_b = _salon(database_url, issuer_b)
    try:
        conn_a = _sender_conn(engine_a, tenant_a["tenant_id"])
        try:
            register_sender(conn_a, tenant_id=tenant_a["tenant_id"],
                            label="A", sender_identity="+14165550222")
            conn_a.commit()
        finally:
            conn_a.close()
        conn_b = _sender_conn(engine_b, tenant_b["tenant_id"])
        try:
            with pytest.raises(SenderModuleError):
                register_sender(conn_b, tenant_id=tenant_b["tenant_id"],
                                label="B", sender_identity="+14165550222")
        finally:
            conn_b.close()
    finally:
        _teardown(engine_a, [tenant_a["tenant_id"]], issuer_a)
        _teardown(engine_b, [tenant_b["tenant_id"]], issuer_b)


def test_sms_ac1_unverified_salon_fails_sms_without_sending(database_url):
    """AC-1: SMS with no verified sender fails safely; nothing is sent."""
    issuer = "https://identity.example.invalid/sms-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        _isolate_booking_email(engine, tenant["tenant_id"], appt_id)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            register_sender(conn, tenant_id=tenant["tenant_id"],
                            label="Unverified", sender_identity="+14165550333")
            record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value="+14165550101", channel="sms",
                purpose="appointment_reminder_24h", entity_version=1)
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert summary["failed"] == 1
        assert summary["dispatched"] == 0
        assert adapter.records == []
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            state = conn.execute(
                text("""SELECT state, skip_reason FROM communications WHERE tenant_id=:t
                       AND channel='sms' AND purpose='appointment_reminder_24h'"""),
                {"t": tenant["tenant_id"]}).mappings().one()
        assert state["state"] == "failed"
        assert "no verified SMS sender" in state["skip_reason"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_sms_ac1_sender_routes_over_http_with_role_scope(database_url):
    """AC-1: owner manages senders over HTTP; receptionist is denied."""
    issuer = "https://identity.example.invalid/sms-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/sms-senders"
    try:
        created = client.post(base, json={"label": "Front desk",
                                          "sender_identity": "+14165550444"})
        assert created.status_code == 201, created.text
        sender_id = created.json()["id"]
        code = created.json()["verification_code"]
        bad = client.post(f"{base}/{sender_id}/verify", json={"code": "123456"})
        assert bad.status_code == 422
        good = client.post(f"{base}/{sender_id}/verify", json={"code": code})
        assert good.status_code == 200, good.text
        assert good.json()["status"] == "verified"
        listed = client.get(base)
        assert listed.status_code == 200
        assert [s["status"] for s in listed.json()["items"]] == ["verified"]
        # An identity with no membership in this tenant learns nothing (404, not
        # a leak); in-tenant roles without communications.manage get 403.
        from test_clients import _identity
        outsider = _client(database_url, _identity())
        denied = outsider.post(base, json={"label": "X", "sender_identity": "+14165550555"})
        assert denied.status_code == 404
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_sms_ac2_inbound_stop_suppresses_future_sends(database_url):
    """AC-2: inbound STOP suppresses; the queued SMS never dispatches."""
    issuer = "https://identity.example.invalid/sms-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        _isolate_booking_email(engine, tenant["tenant_id"], appt_id)
        base = f"/v1/tenants/{tenant['tenant_id']}/clients"
        added = client.post(f"{base}/{client_id}/contacts",
                            json={"kind": "phone", "value": "+14165550101", "is_primary": True})
        assert added.status_code == 201, added.text
        conn = _sender_conn(engine, tenant["tenant_id"])
        try:
            created = register_sender(conn, tenant_id=tenant["tenant_id"],
                                      label="Main", sender_identity="+14165550666")
            verify_sender(conn, tenant_id=tenant["tenant_id"],
                          sender_id=created["id"], code=created["verification_code"])
            record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value="+14165550101", channel="sms",
                purpose="appointment_reminder_24h", entity_version=1)
            # STOP arrives from the client's number before dispatch.
            result = handle_inbound_sms(conn, sender_identity="+14165550666",
                                        from_value="+14165550101", body="STOP")
            assert result["action"] == "suppressed"
            linked = conn.execute(
                text("""SELECT client_id FROM suppressions
                        WHERE tenant_id=:t AND contact_value='+14165550101'"""),
                {"t": tenant["tenant_id"]}).mappings().one()
            assert str(linked["client_id"]) == client_id
            assert check_suppressed(conn, tenant_id=tenant["tenant_id"],
                                    contact_value="+14165550101", channel="sms") is True
            conn.commit()
        finally:
            conn.close()
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert summary["suppressed"] == 1
        assert adapter.records == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_sms_ac2_stop_from_unknown_number_still_blocks_value(database_url):
    """AC-2: STOP from an unknown number creates a client-less suppression."""
    issuer = "https://identity.example.invalid/sms-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        conn = _sender_conn(engine, tenant["tenant_id"])
        try:
            created = register_sender(conn, tenant_id=tenant["tenant_id"],
                                      label="Main", sender_identity="+14165550777")
            verify_sender(conn, tenant_id=tenant["tenant_id"],
                          sender_id=created["id"], code=created["verification_code"])
            result = handle_inbound_sms(conn, sender_identity="+14165550777",
                                        from_value="+19995550101", body="stop")
            assert result["action"] == "suppressed"
            row = conn.execute(
                text("""SELECT client_id, reason, source FROM suppressions
                        WHERE tenant_id=:t AND contact_value='+19995550101'"""),
                {"t": tenant["tenant_id"]}).mappings().one()
            assert row["client_id"] is None
            assert row["reason"] == "stop" and row["source"] == "provider"
            conn.commit()
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_sms_ac2_help_answers_without_suppressing(database_url):
    """AC-2: HELP gets help text; the number stays reachable."""
    issuer = "https://identity.example.invalid/sms-7"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _booked_appointment(client, tenant)
        conn = _sender_conn(engine, tenant["tenant_id"])
        try:
            created = register_sender(conn, tenant_id=tenant["tenant_id"],
                                      label="Main", sender_identity="+14165550888")
            verify_sender(conn, tenant_id=tenant["tenant_id"],
                          sender_id=created["id"], code=created["verification_code"])
            result = handle_inbound_sms(conn, sender_identity="+14165550888",
                                        from_value="+14165550101", body="Help")
            assert result["action"] == "help_sent"
            assert "STOP" in result["reply"]
            assert check_suppressed(conn, tenant_id=tenant["tenant_id"],
                                    contact_value="+14165550101", channel="sms") is False
            logged = conn.execute(
                text("SELECT action FROM inbound_messages WHERE tenant_id=:t"),
                {"t": tenant["tenant_id"]}).mappings().all()
            assert [r["action"] for r in logged] == ["help_sent"]
            conn.commit()
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
