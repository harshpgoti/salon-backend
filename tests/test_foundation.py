from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text

from modules.identity.main import (
    AuthenticatedIdentity, DEMO_SALON, RecordingDeliveryAdapter, create_app, require_identity,
)


def test_health_is_ok_only_when_database_is_reachable():
    ready = TestClient(create_app(database_probe=lambda: True))
    unavailable = TestClient(create_app(database_probe=lambda: False))

    assert ready.get("/health").status_code == 200
    assert ready.get("/health").json() == {"status": "ok", "database": "connected"}
    assert unavailable.get("/health").status_code == 503
    assert unavailable.get("/health").json() == {"status": "degraded", "database": "unavailable"}


def test_recording_delivery_is_local_and_does_not_contact_a_provider():
    adapter = RecordingDeliveryAdapter()
    receipt = adapter.send(channel="email", recipient="client@example.invalid", template="appointment-confirmation")

    assert receipt == {"status": "recorded", "id": "recorded-1"}
    # TASK-029 stamps the sender identity on every record (None when the
    # channel carries no sender, e.g. email); the adapter still only records.
    assert adapter.records == [{"channel": "email", "recipient": "client@example.invalid",
                                "template": "appointment-confirmation", "sender": None}]
    assert DEMO_SALON == {"name": "Maple Demo Salon", "timezone": "America/Toronto"}


def test_onboarding_requires_authenticated_identity_and_valid_timezone():
    client = TestClient(create_app())
    payload = {"business_name": "Synthetic Salon", "location_name": "Main", "timezone": "America/Toronto"}

    assert client.post("/v1/onboarding", json=payload).status_code == 401
    app = create_app()
    app.dependency_overrides[require_identity] = lambda: AuthenticatedIdentity("issuer", "subject")
    response = TestClient(app).post("/v1/onboarding", json={**payload, "timezone": "Mars/Olympus"})
    assert response.status_code == 422


def test_onboarding_is_atomic_idempotent_and_tenant_scoped():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("set DATABASE_URL to run PostgreSQL onboarding integration")
    engine = create_engine(database_url, pool_pre_ping=True)
    identity_a = AuthenticatedIdentity("https://identity.example.invalid/test", str(uuid.uuid4()))
    identity_b = AuthenticatedIdentity("https://identity.example.invalid/test", str(uuid.uuid4()))
    body_a = {"business_name": "Synthetic Cedar Studio", "location_name": "Main", "timezone": "America/Toronto"}
    body_b = {"business_name": "Synthetic Birch Studio", "location_name": "Main", "timezone": "America/Vancouver"}
    created_ids = []

    failed_identity = AuthenticatedIdentity(identity_a.issuer, str(uuid.uuid4()))
    failed_engine = create_engine(database_url, pool_pre_ping=True)
    @event.listens_for(failed_engine, "before_cursor_execute")
    def fail_owner_membership(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO MEMBERSHIPS"):
            raise RuntimeError("synthetic failure before owner membership")
    failed_app = create_app(engine_factory=lambda: failed_engine)
    failed_app.dependency_overrides[require_identity] = lambda: failed_identity
    failure = TestClient(failed_app, raise_server_exceptions=False).post("/v1/onboarding", json=body_a)
    assert failure.status_code == 500
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM identity_users WHERE issuer=:issuer AND subject=:subject"),
                                  {"issuer": failed_identity.issuer, "subject": failed_identity.subject}).scalar_one() == 0
    failed_engine.dispose()

    def onboard_as(identity, body):
        app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
        app.dependency_overrides[require_identity] = lambda: identity
        return TestClient(app).post("/v1/onboarding", json=body)

    try:
        first = onboard_as(identity_a, body_a)
        assert first.status_code == 201
        created_ids.append(first.json()["tenant_id"])
        replay = onboard_as(identity_a, body_a)
        assert replay.status_code == 200
        assert replay.json() == first.json()
        assert first.json()["role"] == "owner"
        assert first.json()["timezone"] == "America/Toronto"
        assert onboard_as(identity_a, {**body_a, "business_name": "Changed Studio"}).status_code == 409

        second = onboard_as(identity_b, body_b)
        assert second.status_code == 201
        created_ids.append(second.json()["tenant_id"])
        with engine.connect() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, false)"), {"tenant": created_ids[1]})
            visible = connection.execute(text("SELECT id::text FROM tenants ORDER BY id")).scalars().all()
            assert visible == [created_ids[1]]
    finally:
        with engine.begin() as connection:
            for tenant_id in created_ids:
                connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
                connection.execute(text("UPDATE identity_users SET tenant_id=NULL, onboarding_fingerprint=NULL WHERE tenant_id=:tenant"), {"tenant": tenant_id})
                connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": tenant_id})
            connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            connection.execute(text("DELETE FROM identity_users WHERE issuer=:issuer AND subject IN (:a, :b)"),
                               {"issuer": identity_a.issuer, "a": identity_a.subject, "b": identity_b.subject})
        engine.dispose()
