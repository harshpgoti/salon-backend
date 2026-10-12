"""Tests for request tracing and sanitized telemetry (TASK-053, FR-077/FR-078).

AC-1: Request-to-worker/provider-callback correlation available to case diagnostics.
AC-2: PII/tokens/bodies blocked; no secret or customer URL in telemetry.
"""

import json
import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.callbacks import ingest_callback, sign_callback_body
from modules.communications.outbox import (
    dispatch_due_communications,
    record_booking_communication,
)
from modules.identity.main import (
    AuthenticatedIdentity,
    create_app,
    require_company_identity,
)
from modules.trace import (
    metrics_snapshot,
    record_trace_event,
    sanitize_for_trace,
    trace_timeline,
)
from test_clients import _salon, _client, _teardown
from test_communications import RecordingAdapter, _booked_appointment, _client_email

COMPANY = "https://company.example.invalid/trace"
SECRET = "test-trace-secret-0123456789abcdef01"


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


def _company_client(database_url: str, subject: str) -> TestClient:
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_company_identity] = lambda: AuthenticatedIdentity(
        COMPANY, subject, "company")
    return TestClient(app)


def test_trace_ac1_journey_correlates_to_case_diagnostics(database_url):
    """AC-1: record -> worker send -> callback share one trace, readable by grant."""
    issuer = "https://identity.example.invalid/trace-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        email = _client_email(engine, tid, client_id)
        trace_id = str(uuid.uuid4())
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            row = record_booking_communication(
                connection=conn, tenant_id=tid, appointment_id=appt_id,
                client_id=client_id, contact_value=email, channel="email",
                purpose="booking_confirmation", entity_version=1, trace_id=trace_id)
            assert row is not None
            conn.execute(
                text("""INSERT INTO provider_webhook_secrets (tenant_id, channel, secret)
                        VALUES (:t, 'email', :s) ON CONFLICT DO NOTHING"""),
                {"t": tid, "s": SECRET})
            dispatch_due_communications(connection=conn, tenant_id=tid,
                                        adapter=RecordingAdapter())
            body = b"{}"
            ingest_callback(conn, tenant_id=tid, channel="email", raw_body=body,
                            signature=sign_callback_body(SECRET, body),
                            event="delivered", provider_event_id="trace-evt-1",
                            communication_id=row["id"])
        conn = _scoped(engine, tid)
        try:
            spans = trace_timeline(conn, tenant_id=tid, trace_id=trace_id)
            assert [s["span"] for s in spans] == ["outbox_record", "worker_send",
                                                  "provider_callback"]
            assert all(s["detail"].get("communication_id") == str(row["id"]) for s in spans)
        finally:
            conn.close()
        # Case diagnostics leg: support case + approved grant unlock the read.
        admin = _company_client(database_url, "admin-1")
        assert admin.post("/v1/company/staff", json={
            "issuer": COMPANY, "subject": "admin-1", "display_name": "Ava",
            "role": "admin", "mfa_enrolled": True}).status_code == 201
        assert admin.post("/v1/company/staff", json={
            "issuer": COMPANY, "subject": "support-1", "display_name": "Sam",
            "role": "support", "mfa_enrolled": True}).status_code == 201
        support = _company_client(database_url, "support-1")
        case_id = support.post("/v1/company/cases", json={
            "tenant_id": tid, "title": "Where is the reminder?"}).json()["id"]
        grant_id = support.post(f"/v1/company/cases/{case_id}/grants", json={
            "justification": "Trace the reminder."}).json()["id"]
        assert staff_approve(client, tid, grant_id)
        read = support.get(f"/v1/company/traces/{trace_id}",
                           params={"tenant_id": tid, "grant_id": grant_id})
        assert read.status_code == 200, read.text
        assert [s["span"] for s in read.json()["spans"]] == ["outbox_record", "worker_send",
                                                              "provider_callback"]
        assert support.get(f"/v1/company/traces/{trace_id}",
                           params={"tenant_id": tid,
                                   "grant_id": "00000000-0000-0000-0000-000000000000"}
                           ).status_code == 403
    finally:
        _teardown(engine, [tid], issuer)
        with create_engine(database_url).connect() as conn:
            conn.execute(text("DELETE FROM company_staff WHERE issuer=:i"), {"i": COMPANY})
            conn.execute(text("DELETE FROM identity_users WHERE issuer=:i"), {"i": COMPANY})
            conn.commit()


def staff_approve(client, tid, grant_id):
    resp = client.post(f"/v1/tenants/{tid}/support-access/{grant_id}/approve")
    return resp.status_code == 200


def test_trace_ac1_middleware_echoes_and_mints(database_url):
    """AC-1: the edge echoes a valid trace id and mints a missing one."""
    issuer = "https://identity.example.invalid/trace-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        sent = str(uuid.uuid4())
        echoed = client.get("/health", headers={"X-Trace-ID": sent})
        assert echoed.status_code == 200
        assert echoed.headers["X-Trace-ID"] == sent
        minted = client.get("/health")
        assert minted.status_code == 200
        uuid.UUID(minted.headers["X-Trace-ID"])
        assert minted.headers["X-Trace-ID"] != sent
        forged = client.get("/health", headers={"X-Trace-ID": "not-a-trace"})
        assert forged.status_code == 200
        uuid.UUID(forged.headers["X-Trace-ID"])
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_trace_ac1_metrics_snapshot_counts(database_url):
    """AC-1: the FR-078 snapshot reconciles with direct counts."""
    issuer = "https://identity.example.invalid/trace-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        email = _client_email(engine, tid, client_id)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            record_booking_communication(
                connection=conn, tenant_id=tid, appointment_id=appt_id,
                client_id=client_id, contact_value=email, channel="email",
                purpose="booking_confirmation", entity_version=1)
            dispatch_due_communications(connection=conn, tenant_id=tid,
                                        adapter=RecordingAdapter())
        conn = _scoped(engine, tid)
        try:
            snap = metrics_snapshot(conn, tenant_id=tid)
            assert snap["booking_commits_24h"] == 1
            assert snap["communications_recorded_24h"] == 1
            assert snap["failed_communications"] == 0
            assert snap["open_retry_queue"] == 0
            assert snap["generated_at"]
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_trace_ac2_sanitizer_blocks_everything_sensitive():
    """AC-2: hostile payloads leave telemetry with identifiers only."""
    hostile = {
        "appointment_id": "123e4567-e89b-12d3-a456-426614174000",
        "state": "dispatched",
        "contact_value": "+14165550000",
        "body": "Hi Ada, your code is 482910",
        "recipient": "ada@example.com",
        "token": "bearer abcdef123456",
        "password": "hunter2",
        "callback": "https://customer.example.invalid/hook/secret",
        "nested": {"phone": "+14165550000"},
        "count": 3,
    }
    clean = sanitize_for_trace(hostile)
    assert clean == {"appointment_id": "123e4567-e89b-12d3-a456-426614174000",
                     "state": "dispatched", "count": 3}
    blob = json.dumps(clean)
    for leaked in ("+14165550000", "ada@example.com", "abcdef123456", "hunter2",
                   "customer.example.invalid", "482910"):
        assert leaked not in blob


def test_trace_ac2_stored_spans_carry_no_pii(database_url):
    """AC-2: even a hostile record call persists only safe fields."""
    issuer = "https://identity.example.invalid/trace-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        trace_id = str(uuid.uuid4())
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            record_trace_event(conn, tenant_id=tid, trace_id=trace_id,
                               span="worker_send",
                               detail={"communication_id": "abc", "channel": "sms",
                                       "contact_value": "+14165550000",
                                       "token": "secret-123",
                                       "callback": "https://evil.example.invalid/x"})
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            stored = conn.execute(
                text("SELECT detail FROM trace_events WHERE tenant_id=:t"),
                {"t": tid}).mappings().one()["detail"]
        assert set(stored) <= {"communication_id", "channel"}
        blob = json.dumps(stored)
        assert "+14165550000" not in blob and "evil.example.invalid" not in blob
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
