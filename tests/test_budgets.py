"""Tests for atomic usage allowances and messaging budgets (TASK-049, FR-069).

AC-1: Reservation/cost reconciliation enforces tenant approved spend ceiling.
AC-2: Duplicate provider usage not billed twice; unknown charges stay pending.
"""

import os
import sys
import threading
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.budgets import reconcile_provider_usage
from modules.communications.outbox import (
    dispatch_due_communications,
    record_booking_communication,
)
from modules.communications.senders import register_sender, verify_sender
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


def test_budget_ac1_ceiling_blocks_and_reconciles(database_url):
    """AC-1: the ceiling blocks the second cent; the first reconciles."""
    issuer = "https://identity.example.invalid/budget-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        saved = client.put(f"/v1/tenants/{tid}/billing/budget",
                           json={"spend_ceiling_minor": 1})
        assert saved.status_code == 200, saved.text
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            created = register_sender(conn, tenant_id=tid, label="Main",
                                      sender_identity="+14165550111")
            verify_sender(conn, tenant_id=tid, sender_id=created["id"],
                          code=created["verification_code"])
            record_booking_communication(
                connection=conn, tenant_id=tid, appointment_id=appt_id,
                client_id=client_id, contact_value="a@example.invalid", channel="email",
                purpose="note_1", entity_version=1)
            record_booking_communication(
                connection=conn, tenant_id=tid, appointment_id=appt_id,
                client_id=client_id, contact_value="a@example.invalid", channel="email",
                purpose="note_2", entity_version=1)
        # Email costs nothing, so both send and reconcile to zero.
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tid, adapter=adapter)
        assert summary["dispatched"] == 2
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            states = conn.execute(
                text("SELECT state FROM usage_reservations WHERE tenant_id=:t"),
                {"t": tid}).scalars().all()
        assert states == ["reconciled", "reconciled"]
        # A one-cent SMS ceiling is now exhausted by a one-cent send...
        sms = client.put(f"/v1/tenants/{tid}/billing/budget",
                         json={"spend_ceiling_minor": 1})
        assert sms.status_code == 200
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            record_booking_communication(
                connection=conn, tenant_id=tid, appointment_id=appt_id,
                client_id=client_id, contact_value="+14165550101", channel="sms",
                purpose="note_3", entity_version=1)
            before = len(adapter.records)
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tid, adapter=adapter)
        assert summary["dispatched"] == 1
        assert len(adapter.records) == before + 1
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            record_booking_communication(
                connection=conn, tenant_id=tid, appointment_id=appt_id,
                client_id=client_id, contact_value="+14165550101", channel="sms",
                purpose="note_4", entity_version=1)
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tid, adapter=adapter)
        assert summary["failed"] == 1 and summary["dispatched"] == 0
        assert len(adapter.records) == before + 1
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            blocked = conn.execute(
                text("""SELECT state, skip_reason FROM communications
                        WHERE tenant_id=:t AND purpose='note_4'"""),
                {"t": tid}).mappings().one()
        assert blocked["state"] == "failed"
        assert blocked["skip_reason"].startswith("budget_exceeded")
        # ...and the budget report shows the ceiling against real spend.
        report = client.get(f"/v1/tenants/{tid}/billing/budget").json()
        assert report["spend_ceiling_minor"] == 1
        assert report["spent_minor"] == 1
        assert report["currency"] == "CAD"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_budget_ac1_concurrent_dispatch_respects_ceiling(database_url):
    """AC-1: ten racers, five cents of ceiling — exactly five send."""
    issuer = "https://identity.example.invalid/budget-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        assert client.put(f"/v1/tenants/{tid}/billing/budget",
                          json={"spend_ceiling_minor": 5}).status_code == 200
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            created = register_sender(conn, tenant_id=tid, label="Main",
                                      sender_identity="+14165550222")
            verify_sender(conn, tenant_id=tid, sender_id=created["id"],
                          code=created["verification_code"])
            for n in range(10):
                assert record_booking_communication(
                    connection=conn, tenant_id=tid, appointment_id=appt_id,
                    client_id=client_id, contact_value="+14165550101", channel="sms",
                    purpose=f"race_{n}", entity_version=1) is not None
        adapter = RecordingAdapter()
        lock = threading.Lock()
        outcomes = []

        def worker():
            local_engine = create_engine(database_url, pool_pre_ping=True)
            try:
                with local_engine.begin() as conn:
                    conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                                 {"t": tid})
                    summary = dispatch_due_communications(
                        connection=conn, tenant_id=tid, adapter=adapter)
                with lock:
                    outcomes.append(summary)
            finally:
                local_engine.dispose()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        sent = sum(o["dispatched"] for o in outcomes)
        failed = sum(o["failed"] for o in outcomes)
        assert sent == 5
        assert failed == 5
        assert len(adapter.records) == 5
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            spent = conn.execute(
                text("SELECT COALESCE(SUM(cost_minor), 0) FROM message_costs WHERE tenant_id=:t"),
                {"t": tid}).scalar_one()
        assert spent == 5
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_budget_ac2_provider_usage_matches_once(database_url):
    """AC-2: matched lines settle once; duplicates ignore; unknown pends."""
    issuer = "https://identity.example.invalid/budget-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            row = record_booking_communication(
                connection=conn, tenant_id=tid, appointment_id=appt_id,
                client_id=client_id, contact_value="a@example.invalid", channel="email",
                purpose="note_9", entity_version=1)
            dispatch_due_communications(connection=conn, tenant_id=tid,
                                        adapter=RecordingAdapter())
        conn = _scoped(engine, tid)
        try:
            matched = reconcile_provider_usage(
                conn, tenant_id=tid, provider_ref="prov-001",
                communication_id=row["id"], segments=1, cost_minor=0)
            assert matched["status"] == "matched"
            assert reconcile_provider_usage(
                conn, tenant_id=tid, provider_ref="prov-001",
                communication_id=row["id"], segments=9, cost_minor=99)["status"] == "duplicate"
            ghost = reconcile_provider_usage(
                conn, tenant_id=tid, provider_ref="prov-ghost",
                communication_id=None, segments=3, cost_minor=3)
            assert ghost["status"] == "pending"
            states = conn.execute(
                text("""SELECT provider_ref, state FROM provider_usage
                        WHERE tenant_id=:t ORDER BY provider_ref"""),
                {"t": tid}).mappings().all()
            assert [(r["provider_ref"], r["state"]) for r in states] == [
                ("prov-001", "matched"), ("prov-ghost", "pending")]
            conn.commit()
        finally:
            conn.close()
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_budget_routes_validate(database_url):
    """AC-1: negative ceilings rejected; strangers see nothing."""
    issuer = "https://identity.example.invalid/budget-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        assert client.put(f"/v1/tenants/{tid}/billing/budget",
                          json={"spend_ceiling_minor": -1}).status_code == 422
        fresh = client.get(f"/v1/tenants/{tid}/billing/budget").json()
        assert fresh["spend_ceiling_minor"] is None
        assert fresh["spent_minor"] == 0
        from test_clients import _identity
        outsider = _client(database_url, _identity())
        assert outsider.get(f"/v1/tenants/{tid}/billing/budget").status_code == 404
        assert outsider.put(f"/v1/tenants/{tid}/billing/budget",
                            json={"spend_ceiling_minor": 5}).status_code == 404
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
