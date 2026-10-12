"""Tests for operational alert evaluation (TASK-060, FR-078).

Breach fires, recovery resolves, no duplicate actives, unknown rule rejected.
"""

import os
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.identity.main import AuthenticatedIdentity, create_app, require_identity
from modules.ops_alerts import (
    ALERT_RULES,
    OpsAlertsError,
    evaluate_alerts,
    list_active_alerts,
    resolve_alert,
)
from test_clients import _teardown

from fastapi.testclient import TestClient


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _staff_client(database_url: str, identity: AuthenticatedIdentity) -> TestClient:
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_identity] = lambda: identity
    return TestClient(app)


def _salon(database_url: str, issuer: str):
    """Onboard a tenant and return (engine, owner identity, tenant payload)."""
    owner = AuthenticatedIdentity(issuer, str(uuid.uuid4()))
    signup = _staff_client(database_url, owner).post("/v1/onboarding", json={
        "business_name": "Synthetic Ops Salon", "location_name": "Main",
        "timezone": "America/Toronto"})
    assert signup.status_code == 201, signup.text
    engine = create_engine(database_url, pool_pre_ping=True)
    return engine, owner, signup.json()


def _client_id(database_url: str, owner, tid: str) -> str:
    created = _staff_client(database_url, owner).post(
        f"/v1/tenants/{tid}/clients", json={"name": "Synthetic Client"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _failed_comms(connection, tid: str, client_id: str, n: int) -> None:
    connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
    for i in range(n):
        connection.execute(
            text("""INSERT INTO communications
                    (tenant_id, client_id, contact_value, channel, purpose,
                     source_type, source_id, expected_entity_version, state, dedupe_key)
                    VALUES (:t, :c, 'ops60@example.invalid', 'email',
                            'booking_confirmation', 'appointment', gen_random_uuid(),
                            1, 'failed', :d)"""),
            {"t": tid, "c": client_id, "d": f"ops60-failed-{uuid.uuid4()}-{i}"})


def test_ops60_breach_fires(database_url):
    """Breaching failed_communications opens one major active alert."""
    issuer = "https://identity.example.invalid/ops60-breach"
    engine, owner, tenant = _salon(database_url, issuer)
    tid = tenant["tenant_id"]
    try:
        cid = _client_id(database_url, owner, tid)
        with engine.begin() as conn:
            _failed_comms(conn, tid, cid, 5)
            fired = evaluate_alerts(conn, tenant_id=tid)
        failed = [a for a in fired if a["rule"] == "failed_communications"]
        assert len(failed) == 1
        assert failed[0]["state"] == "active"
        assert failed[0]["severity"] == "major"
        assert failed[0]["fired_at"]
        with engine.connect() as conn:
            assert len(list_active_alerts(conn, tenant_id=tid)) >= 1
    finally:
        _teardown(engine, [tid], issuer)


def test_ops60_recovery_resolves(database_url):
    """Metric back below threshold resolves the active alert with a timestamp."""
    issuer = "https://identity.example.invalid/ops60-recovery"
    engine, owner, tenant = _salon(database_url, issuer)
    tid = tenant["tenant_id"]
    try:
        cid = _client_id(database_url, owner, tid)
        with engine.begin() as conn:
            _failed_comms(conn, tid, cid, 5)
            assert any(a["rule"] == "failed_communications"
                       for a in evaluate_alerts(conn, tenant_id=tid))
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            conn.execute(text("""UPDATE communications SET state='delivered'
                                 WHERE tenant_id=:t AND state='failed'"""), {"t": tid})
            still = evaluate_alerts(conn, tenant_id=tid)
            assert all(a["rule"] != "failed_communications" for a in still)
            row = conn.execute(
                text("""SELECT state, resolved_at FROM ops_alerts
                        WHERE rule='failed_communications' AND tenant_id=:t"""),
                {"t": tid}).mappings().one()
            assert row["state"] == "resolved"
            assert row["resolved_at"] is not None
    finally:
        _teardown(engine, [tid], issuer)


def test_ops60_no_duplicate_actives(database_url):
    """Repeated evaluation while breached keeps exactly one active row."""
    issuer = "https://identity.example.invalid/ops60-dedupe"
    engine, owner, tenant = _salon(database_url, issuer)
    tid = tenant["tenant_id"]
    try:
        cid = _client_id(database_url, owner, tid)
        with engine.begin() as conn:
            _failed_comms(conn, tid, cid, 5)
            evaluate_alerts(conn, tenant_id=tid)
            evaluate_alerts(conn, tenant_id=tid)
            count = conn.execute(
                text("""SELECT count(*) FROM ops_alerts
                        WHERE rule='failed_communications' AND tenant_id=:t
                          AND state='active'"""),
                {"t": tid}).scalar_one()
            assert count == 1
    finally:
        _teardown(engine, [tid], issuer)


def test_ops60_unknown_rule_rejected(database_url):
    """Resolve of an unknown rule raises instead of touching alert state."""
    issuer = "https://identity.example.invalid/ops60-unknown"
    engine, owner, tenant = _salon(database_url, issuer)
    tid = tenant["tenant_id"]
    try:
        with engine.connect() as conn:
            with pytest.raises(OpsAlertsError):
                resolve_alert(conn, rule="not_a_rule", tenant_id=tid)
            assert [r["rule"] for r in ALERT_RULES] == [
                "failed_communications", "dead_letters_open", "retry_queue_depth",
                "provider_error_rate", "spend_spike_24h"]
    finally:
        _teardown(engine, [tid], issuer)
