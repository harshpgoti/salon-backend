from __future__ import annotations

import os
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import DBAPIError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.identity.main import AuthenticatedIdentity, create_app, require_identity
from test_booking_holds import _bookable, _first_slot
from test_clients import _client, _salon, _teardown


def test_audit_rows_are_safe_tenant_scoped_immutable_and_request_correlated():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("set DATABASE_URL to run PostgreSQL audit integration")
    engine = create_engine(database_url, pool_pre_ping=True)
    identity = AuthenticatedIdentity("https://identity.example.invalid/audit-test", str(uuid.uuid4()))
    other = AuthenticatedIdentity(identity.issuer, str(uuid.uuid4()))
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_identity] = lambda: identity
    client = TestClient(app)
    tenant_id = None
    request_id = str(uuid.uuid4())

    try:
        created = client.post("/v1/onboarding", json={"business_name":"Synthetic Audit Studio", "location_name":"Main", "timezone":"America/Toronto"},
                              headers={"X-Request-ID": request_id})
        assert created.status_code == 201
        tenant_id = created.json()["tenant_id"]
        assert created.headers["X-Request-ID"] == request_id

        replay = client.post("/v1/onboarding", json={"business_name":"Synthetic Audit Studio", "location_name":"Main", "timezone":"America/Toronto"},
                             headers={"X-Request-ID": "personal@example.invalid"})
        assert replay.status_code == 200
        assert uuid.UUID(replay.headers["X-Request-ID"])
        assert replay.headers["X-Request-ID"] != "personal@example.invalid"

        result = client.get(f"/v1/tenants/{tenant_id}/audit")
        assert result.status_code == 200
        rows = result.json()["items"]
        assert len(rows) == 1
        row = rows[0]
        assert row["actor_issuer"] == identity.issuer and row["actor_subject"] == identity.subject
        assert (row["action"], row["resource_type"], row["resource_id"]) == ("tenant.onboarded", "tenant", tenant_id)
        assert row["reason"] == "owner initiated onboarding"
        assert row["request_id"] == request_id
        assert row["changed_fields"] == ["display_name", "default_currency", "location", "owner_membership"]
        assert "Synthetic Audit Studio" not in str(row)

        app.dependency_overrides[require_identity] = lambda: other
        assert client.get(f"/v1/tenants/{tenant_id}/audit").status_code == 404
        app.dependency_overrides[require_identity] = lambda: identity

        with engine.connect() as connection:
            with connection.begin():
                connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
                with pytest.raises(DBAPIError):
                    with connection.begin_nested():
                        connection.execute(text("UPDATE tenant_audit_events SET reason='edited' WHERE tenant_id=:tenant"),
                                           {"tenant": tenant_id})
                with pytest.raises(DBAPIError):
                    with connection.begin_nested():
                        connection.execute(text("DELETE FROM tenant_audit_events WHERE tenant_id=:tenant"),
                                           {"tenant": tenant_id})
    finally:
        if tenant_id:
            with engine.begin() as connection:
                connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
                connection.execute(text("UPDATE identity_users SET tenant_id=NULL,onboarding_fingerprint=NULL WHERE tenant_id=:tenant"), {"tenant": tenant_id})
                connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": tenant_id})
                connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
                connection.execute(text("DELETE FROM identity_users WHERE issuer=:issuer AND subject IN (:owner,:other)"),
                                   {"issuer": identity.issuer, "owner": identity.subject, "other": other.subject})
        engine.dispose()


def test_audit_failure_rolls_back_the_business_mutation():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("set DATABASE_URL to run PostgreSQL audit rollback integration")
    identity = AuthenticatedIdentity("https://identity.example.invalid/audit-rollback", str(uuid.uuid4()))
    engine = create_engine(database_url, pool_pre_ping=True)
    @event.listens_for(engine, "before_cursor_execute")
    def reject_audit_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("INSERT INTO TENANT_AUDIT_EVENTS"):
            raise RuntimeError("synthetic audit sink failure")

    app = create_app(engine_factory=lambda: engine)
    app.dependency_overrides[require_identity] = lambda: identity
    try:
        response = TestClient(app, raise_server_exceptions=False).post(
            "/v1/onboarding", json={"business_name":"Synthetic Rollback Studio", "location_name":"Main", "timezone":"America/Toronto"})
        assert response.status_code == 500
        with create_engine(database_url).connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM identity_users WHERE issuer=:issuer AND subject=:subject"),
                                      {"issuer": identity.issuer, "subject": identity.subject}).scalar_one() == 0
    finally:
        engine.dispose()


def test_public_hold_confirmation_is_audited_and_free_text_stays_out_of_audit():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("set DATABASE_URL to run PostgreSQL audit integration")
    issuer = "https://identity.example.invalid/audit-public-hold"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    marker = "SYNTHETIC_PRIVATE_AUDIT_MARKER"
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        created_staff = client.post(f"{base}/staff", json={"display_name": marker})
        assert created_staff.status_code == 201, created_staff.text

        day = date.today() + timedelta(days=1)
        starts_at = _first_slot(client, slug, service, day)
        hold = client.post(f"/v1/public/salons/{slug}/holds", json={
            "service_id": service, "staff_id": staff, "starts_at": starts_at,
            "idempotency_key": "audit-hold-" + uuid.uuid4().hex,
        })
        assert hold.status_code == 201, hold.text

        request_id = str(uuid.uuid4())
        confirmed = client.post(
            f"/v1/public/salons/{slug}/holds/{hold.json()['hold_id']}/confirm",
            json={"owner_token": hold.json()["owner_token"],
                  "client_name": "Synthetic Audit Booker", "email": "audit@example.invalid",
                  "policy_accepted": True, "policy_version": 1,
                  "idempotency_key": "audit-confirm-" + uuid.uuid4().hex},
            headers={"X-Request-ID": request_id},
        )
        assert confirmed.status_code == 200, confirmed.text
        appointment_id = confirmed.json()["appointment_id"]
        assert confirmed.headers["X-Request-ID"] == request_id

        cancelled = client.post(
            f"{base}/appointments/{appointment_id}/cancel",
            json={"expected_version": 1, "reason": marker,
                  "idempotency_key": "audit-cancel-" + uuid.uuid4().hex},
        )
        assert cancelled.status_code == 200, cancelled.text

        events = client.get(f"{base}/audit")
        assert events.status_code == 200, events.text
        rows = events.json()["items"]
        confirmation = next(row for row in rows
                            if row["action"] == "appointment.created"
                            and row["resource_id"] == appointment_id)
        cancellation = next(row for row in rows
                            if row["action"] == "appointment.cancelled"
                            and row["resource_id"] == appointment_id)
        staff_event = next(row for row in rows
                           if row["action"] == "staff.created"
                           and row["resource_id"] == created_staff.json()["id"])

        assert confirmation["actor_issuer"] == "public"
        assert confirmation["actor_subject"] == "public_booking"
        assert confirmation["request_id"] == request_id
        assert confirmation["reason"] == "appointment created"
        assert cancellation["reason"] == "appointment cancelled"
        assert staff_event["reason"] == "staff profile created"
        assert marker not in str(rows)
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
