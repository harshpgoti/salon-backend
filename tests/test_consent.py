"""Tests for consent evidence and opt-out suppression (TASK-028, FR-040/FR-041).

AC-1: Purpose/sender/channel proof persists; booking/import never opt in silently.
AC-2: Unsubscribe/STOP races block later authorization and queued sends.
"""

import os
import sys
import threading
from urllib.parse import parse_qs, urlsplit
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.consent import (
    ConsentModuleError,
    check_consented,
    check_suppressed,
    record_consent,
    record_suppression,
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


def _scoped(engine, tenant_id):
    """A begun connection with the tenant RLS scope set. Caller commits/rolls back."""
    conn = engine.connect()
    conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
    return conn


def _client_with_email(client, base):
    created = client.post(base, json={
        "name": "Consent Client",
        "contacts": [{"kind": "email", "value": "Consenter@Example.com", "is_primary": True}],
    })
    assert created.status_code == 201, created.text
    created = created.json()
    contact = next(c for c in created["contacts"] if c["kind"] == "email")
    return created["id"], contact["id"], contact["display_value"]


def _isolate_outbox(engine, tenant_id, appointment_id):
    """Keep TASK-027 booking-created notices out of these dispatch assertions."""
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                     {"t": tenant_id})
        conn.execute(text("""UPDATE communications
            SET state='skipped_stale', skip_reason='isolated test fixture',
                version=version+1, updated_at=now()
            WHERE tenant_id=:t AND source_id=:appointment
              AND purpose='booking_confirmation' AND state='recorded'"""),
                     {"t": tenant_id, "appointment": appointment_id})


def test_consent_ac1_booking_source_cannot_grant_marketing(database_url):
    """AC-1: a booking/import source asking for marketing consent is rejected."""
    issuer = "https://identity.example.invalid/consent-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        client_id, contact_id, _ = _client_with_email(client, base)
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            with pytest.raises(ConsentModuleError):
                record_consent(
                    conn, tenant_id=tenant["tenant_id"], client_id=client_id,
                    contact_id=contact_id, channel="email", purpose="marketing",
                    source="booking", wording_version="v3")
            with pytest.raises(ConsentModuleError):
                record_consent(
                    conn, tenant_id=tenant["tenant_id"], client_id=client_id,
                    contact_id=contact_id, channel="email", purpose="marketing",
                    source="import", wording_version="v3")
            # The same booking source may still record a transactional purpose.
            row = record_consent(
                conn, tenant_id=tenant["tenant_id"], client_id=client_id,
                contact_id=contact_id, channel="email",
                purpose="booking_confirmation", source="booking",
                wording_version="v3", proof_reference="appt-proof-1")
            assert row["status"] == "granted"
            conn.commit()
        finally:
            conn.close()
        # Booking-held transactional consent never authorizes marketing.
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            assert check_consented(
                conn, tenant_id=tenant["tenant_id"], client_id=client_id,
                channel="email", purpose="marketing") is False
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_consent_ac1_explicit_grant_persists_with_proof(database_url):
    """AC-1: an explicit grant persists with purpose/sender/channel proof."""
    issuer = "https://identity.example.invalid/consent-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        client_id, contact_id, _ = _client_with_email(client, base)
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            row = record_consent(
                conn, tenant_id=tenant["tenant_id"], client_id=client_id,
                contact_id=contact_id, channel="email", purpose="marketing",
                source="client_self", wording_version="v3",
                proof_reference="unsubscribe-page-2026-10-11")
            assert row["status"] == "granted"
            proof = conn.execute(
                text("""SELECT purpose, source, channel, wording_version, proof_reference
                        FROM consents WHERE tenant_id=:t AND id=:id"""),
                {"t": tenant["tenant_id"], "id": row["id"]}).mappings().one()
            assert proof["purpose"] == "marketing"
            assert proof["source"] == "client_self"
            assert proof["channel"] == "email"
            assert proof["wording_version"] == "v3"
            assert proof["proof_reference"] == "unsubscribe-page-2026-10-11"
            assert check_consented(
                conn, tenant_id=tenant["tenant_id"], client_id=client_id,
                channel="email", purpose="marketing") is True
            conn.commit()
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_consent_ac1_marketing_grant_is_bound_to_the_contact(database_url):
    """AC-1: an explicit grant for one address cannot authorize another address."""
    issuer = "https://identity.example.invalid/consent-contact"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    base = f"/v1/tenants/{tid}/clients"
    try:
        client_id, contact_id, _ = _client_with_email(client, base)
        changed = client.post(f"{base}/{client_id}/contacts", json={
            "kind": "email", "value": "replacement@example.invalid"})
        assert changed.status_code == 201, changed.text
        new_contact_id = next(c["id"] for c in changed.json()["contacts"]
                              if c["display_value"] == "replacement@example.invalid")
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            record_consent(
                conn, tenant_id=tid, client_id=client_id, contact_id=contact_id,
                channel="email", purpose="marketing", source="client_self",
                wording_version="v4", proof_reference="explicit-check-box")
            assert check_consented(
                conn, tenant_id=tid, client_id=client_id, contact_id=contact_id,
                contact_value="Consenter@example.com", channel="email",
                purpose="marketing") is True
            assert check_consented(
                conn, tenant_id=tid, client_id=client_id, contact_id=new_contact_id,
                contact_value="replacement@example.invalid", channel="email",
                purpose="marketing") is False
    finally:
        _teardown(engine, [tid], issuer)


def test_consent_ac2_public_unsubscribe_capability_is_no_login_and_idempotent(database_url):
    """AC-2: an email bearer link suppresses queued email without a login cookie."""
    issuer = "https://identity.example.invalid/consent-unsubscribe"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        email = _client_email(engine, tenant["tenant_id"], client_id)

        class UnsubscribeAdapter:
            def __init__(self):
                self.records = []

            def send(self, *, channel, recipient, template, sender=None,
                     unsubscribe_url=None, idempotency_key=None):
                self.records.append({"channel": channel, "recipient": recipient,
                                     "template": template, "unsubscribe_url": unsubscribe_url})
                return {"status": "recorded", "id": f"unsubscribe-{len(self.records)}"}

        adapter = UnsubscribeAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(connection=conn, tenant_id=tenant["tenant_id"],
                                                 adapter=adapter)
        assert summary["dispatched"] == 1, summary
        url = adapter.records[0]["unsubscribe_url"]
        assert url
        token = parse_qs(urlsplit(url).fragment)["token"][0]
        response = client.post("/v1/public/communications/unsubscribe", json={"token": token})
        assert response.status_code == 204, response.text
        assert response.headers.get("set-cookie") is None
        replay = client.post("/v1/public/communications/unsubscribe", json={"token": token})
        assert replay.status_code == 204, replay.text

        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            suppressed = conn.execute(text("""SELECT count(*) FROM suppressions
                WHERE tenant_id=:t AND contact_value=:email
                  AND channel='email' AND reason='unsubscribe'"""),
                {"t": tenant["tenant_id"], "email": email}).scalar_one()
        assert suppressed == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_consent_ac2_unsubscribe_lock_wins_over_concurrent_queued_send(database_url):
    """AC-2: a committed opt-out serializes ahead of a concurrent dispatch."""
    issuer = "https://identity.example.invalid/consent-race"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        _, _, _, client_id, _, _ = _booked_appointment(client, tenant)
        email = _client_email(engine, tid, client_id)
        suppress_connection = engine.connect()
        suppress_transaction = suppress_connection.begin()
        suppress_connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                                    {"t": tid})
        record_suppression(
            suppress_connection, tenant_id=tid, client_id=client_id,
            contact_value=email, channel="email", reason="unsubscribe")

        adapter = RecordingAdapter()
        started = threading.Event()
        result = {}
        errors = []

        def dispatch():
            try:
                started.set()
                with engine.begin() as connection:
                    connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                                       {"t": tid})
                    result.update(dispatch_due_communications(
                        connection=connection, tenant_id=tid, adapter=adapter))
            except Exception as exc:  # surfaced in the parent thread below
                errors.append(exc)

        thread = threading.Thread(target=dispatch, daemon=True)
        thread.start()
        assert started.wait(2)
        thread.join(timeout=0.25)
        still_waiting_for_opt_out = thread.is_alive()
        suppress_transaction.commit()
        suppress_connection.close()
        thread.join(timeout=5)
        assert still_waiting_for_opt_out, "dispatch did not wait on the opt-out transaction lock"
        assert not thread.is_alive(), "dispatch remained blocked after opt-out committed"
        assert not errors, repr(errors)
        assert result["suppressed"] == 1
        assert result["dispatched"] == 0
        assert adapter.records == []
    finally:
        _teardown(engine, [tid], issuer)


def test_consent_ac1_revocation_wins_over_earlier_grant(database_url):
    """AC-1: a later revocation blocks marketing even after an earlier grant."""
    issuer = "https://identity.example.invalid/consent-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        client_id, contact_id, _ = _client_with_email(client, base)
        conn = _scoped(engine, tenant["tenant_id"])
        try:
            record_consent(
                conn, tenant_id=tenant["tenant_id"], client_id=client_id,
                contact_id=contact_id, channel="email", purpose="marketing",
                source="marketing_form", wording_version="v3")
            assert check_consented(
                conn, tenant_id=tenant["tenant_id"], client_id=client_id,
                channel="email", purpose="marketing") is True
            record_consent(
                conn, tenant_id=tenant["tenant_id"], client_id=client_id,
                contact_id=contact_id, channel="email", purpose="marketing",
                source="client_self", wording_version="v3", status="revoked")
            assert check_consented(
                conn, tenant_id=tenant["tenant_id"], client_id=client_id,
                channel="email", purpose="marketing") is False
            conn.commit()
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_consent_ac2_queued_send_blocked_by_suppression(database_url):
    """AC-2: a recorded message is suppressed at dispatch; the adapter is silent."""
    issuer = "https://identity.example.invalid/consent-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        email = _client_email(engine, tenant["tenant_id"], client_id)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            row = conn.execute(text("""SELECT id FROM communications
                WHERE tenant_id=:t AND source_id=:appointment
                  AND purpose='booking_confirmation' AND state='recorded'"""),
                {"t": tenant["tenant_id"], "appointment": appt_id}).mappings().one()
            assert row is not None
            # The STOP arrives after the message was recorded (the race).
            record_suppression(
                conn, tenant_id=tenant["tenant_id"], client_id=client_id,
                contact_value=email, channel="email", reason="stop")
            assert check_suppressed(
                conn, tenant_id=tenant["tenant_id"],
                contact_value=email, channel="email") is True
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert summary["suppressed"] == 1
        assert summary["dispatched"] == 0
        assert adapter.records == []
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            state = conn.execute(
                text("SELECT state FROM communications WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": row["id"]}).scalar_one()
        assert state == "suppressed"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_consent_ac2_marketing_send_needs_current_grant(database_url):
    """AC-2: a queued marketing message without a grant is suppressed, not sent."""
    issuer = "https://identity.example.invalid/consent-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        _isolate_outbox(engine, tenant["tenant_id"], appt_id)
        email = _client_email(engine, tenant["tenant_id"], client_id)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            record_booking_communication(
                connection=conn, tenant_id=tenant["tenant_id"],
                appointment_id=appt_id, client_id=client_id,
                contact_value=email, channel="email",
                purpose="marketing", entity_version=1)
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert summary["suppressed"] == 1
        assert summary["dispatched"] == 0
        assert adapter.records == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_consent_ac2_suppression_is_tenant_scoped_and_normalized(database_url):
    """AC-2: one tenant's opt-out never blocks another tenant; keys normalize."""
    issuer_a = "https://identity.example.invalid/consent-6a"
    issuer_b = "https://identity.example.invalid/consent-6b"
    engine_a, owner_a, tenant_a = _salon(database_url, issuer_a)
    engine_b, owner_b, tenant_b = _salon(database_url, issuer_b)
    client_a = _client(database_url, owner_a)
    client_b = _client(database_url, owner_b)
    try:
        _, _, _, client_a_id, appt_a, _ = _booked_appointment(client_a, tenant_a)
        _, _, _, client_b_id, appt_b, _ = _booked_appointment(client_b, tenant_b)
        _isolate_outbox(engine_a, tenant_a["tenant_id"], appt_a)
        _isolate_outbox(engine_b, tenant_b["tenant_id"], appt_b)
        email_b = _client_email(engine_b, tenant_b["tenant_id"], client_b_id)
        with engine_a.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant_a["tenant_id"]})
            # Mixed-case opt-out in tenant A normalizes to the same key.
            record_suppression(
                conn, tenant_id=tenant_a["tenant_id"], client_id=client_a_id,
                contact_value="  UNSUBSCRIBED@Example.COM ", channel="email",
                reason="unsubscribe")
        with engine_a.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant_a["tenant_id"]})
            assert check_suppressed(
                conn, tenant_id=tenant_a["tenant_id"],
                contact_value="unsubscribed@example.com", channel="email") is True
            record_booking_communication(
                connection=conn, tenant_id=tenant_a["tenant_id"],
                appointment_id=appt_a, client_id=client_a_id,
                contact_value="unsubscribed@example.com", channel="email",
                purpose="booking_confirmation_test", entity_version=1)
        with engine_b.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant_b["tenant_id"]})
            assert check_suppressed(
                conn, tenant_id=tenant_b["tenant_id"],
                contact_value=email_b, channel="email") is False
            record_booking_communication(
                connection=conn, tenant_id=tenant_b["tenant_id"],
                appointment_id=appt_b, client_id=client_b_id,
                contact_value=email_b, channel="email",
                purpose="booking_confirmation_test", entity_version=1)
        adapter = RecordingAdapter()
        with engine_a.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant_a["tenant_id"]})
            summary_a = dispatch_due_communications(
                connection=conn, tenant_id=tenant_a["tenant_id"], adapter=adapter)
        with engine_b.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant_b["tenant_id"]})
            summary_b = dispatch_due_communications(
                connection=conn, tenant_id=tenant_b["tenant_id"], adapter=adapter)
        assert summary_a["suppressed"] == 1 and summary_a["dispatched"] == 0
        assert summary_b["dispatched"] == 1 and summary_b["suppressed"] == 0
    finally:
        _teardown(engine_a, [tenant_a["tenant_id"]], issuer_a)
        _teardown(engine_b, [tenant_b["tenant_id"]], issuer_b)
