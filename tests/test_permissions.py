from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.identity.main import AuthenticatedIdentity, create_app, project_client_fields, require_identity


def test_field_projection_defaults_to_low_sensitivity_role_allowlists():
    record = {"display_name": "Synthetic Client", "preferred_name": "Sky", "locale": "en-CA",
              "lifecycle": "active", "contact_summary": "email on file", "service_preferences": "short layers",
              "private_note": "sensitive note", "medical_history": "never collect", "home_address": "not visible"}

    assert project_client_fields("stylist", record) == {"display_name": "Synthetic Client", "preferred_name": "Sky",
                                                          "locale": "en-CA", "service_preferences": "short layers"}
    assert "private_note" not in project_client_fields("owner", record)
    assert project_client_fields("manager", record, frozenset({"clients.sensitive.read"}))["private_note"] == "sensitive note"
    assert project_client_fields("analyst", record) == {}
    assert project_client_fields("unknown", record) == {}


def test_location_scope_permissions_and_revocation_use_real_postgres():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("set DATABASE_URL to run PostgreSQL authorization integration")
    engine = create_engine(database_url, pool_pre_ping=True)
    issuer = "https://identity.example.invalid/authorization-test"
    owner = AuthenticatedIdentity(issuer, str(uuid.uuid4()))
    manager = AuthenticatedIdentity(issuer, str(uuid.uuid4()))
    analyst = AuthenticatedIdentity(issuer, str(uuid.uuid4()))
    owner_app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    owner_app.dependency_overrides[require_identity] = lambda: owner
    created_tenant = None

    try:
        signup = TestClient(owner_app).post("/v1/onboarding", json={"business_name":"Synthetic Permission Salon",
                               "location_name":"East", "timezone":"America/Toronto"})
        assert signup.status_code == 201
        created_tenant = signup.json()["tenant_id"]
        first_location = signup.json()["location_id"]
        second_location = str(uuid.uuid4())

        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": created_tenant})
            connection.execute(text("""INSERT INTO locations (id,tenant_id,display_name,timezone,slug,booking_slug)
                                       VALUES (:id,:tenant,'West','America/Toronto','west',:booking)"""),
                               {"id": second_location, "tenant": created_tenant, "booking": "west-" + second_location.replace("-", "")[:12]})
            manager_id = connection.execute(text("INSERT INTO identity_users (issuer,subject) VALUES (:issuer,:subject) RETURNING id"),
                                            {"issuer": manager.issuer, "subject": manager.subject}).scalar_one()
            analyst_id = connection.execute(text("INSERT INTO identity_users (issuer,subject) VALUES (:issuer,:subject) RETURNING id"),
                                            {"issuer": analyst.issuer, "subject": analyst.subject}).scalar_one()
            connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'manager'),(:tenant,:analyst,'analyst')"),
                               {"tenant": created_tenant, "user": str(manager_id), "analyst": str(analyst_id)})
            connection.execute(text("INSERT INTO membership_locations (tenant_id,user_id,location_id) VALUES (:tenant,:user,:location)"),
                               {"tenant": created_tenant, "user": str(manager_id), "location": first_location})

        owner_locations = TestClient(owner_app).get(f"/v1/tenants/{created_tenant}/locations")
        assert owner_locations.status_code == 200
        assert {item["id"] for item in owner_locations.json()["items"]} == {first_location, second_location}

        manager_app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
        manager_app.dependency_overrides[require_identity] = lambda: manager
        manager_client = TestClient(manager_app)
        locations = manager_client.get(f"/v1/tenants/{created_tenant}/locations")
        assert locations.status_code == 200
        assert [item["id"] for item in locations.json()["items"]] == [first_location]
        assert manager_client.get(f"/v1/tenants/{created_tenant}/summary").json()["location_count"] == 1

        analyst_app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
        analyst_app.dependency_overrides[require_identity] = lambda: analyst
        assert TestClient(analyst_app).get(f"/v1/tenants/{created_tenant}/summary").status_code == 403
        assert TestClient(create_app()).get(f"/v1/tenants/{created_tenant}/locations").status_code == 401

        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": created_tenant})
            connection.execute(text("DELETE FROM memberships WHERE tenant_id=:tenant AND user_id=:user"),
                               {"tenant": created_tenant, "user": str(manager_id)})
        assert manager_client.get(f"/v1/tenants/{created_tenant}/locations").status_code == 404
    finally:
        if created_tenant:
            with engine.begin() as connection:
                connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": created_tenant})
                connection.execute(text("UPDATE identity_users SET tenant_id=NULL,onboarding_fingerprint=NULL WHERE tenant_id=:tenant"), {"tenant": created_tenant})
                connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": created_tenant})
                connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
                connection.execute(text("DELETE FROM identity_users WHERE issuer=:issuer AND subject IN (:owner,:manager,:analyst)"),
                                   {"issuer": issuer, "owner": owner.subject, "manager": manager.subject, "analyst": analyst.subject})
        engine.dispose()
