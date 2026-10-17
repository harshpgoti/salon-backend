from __future__ import annotations

import os
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

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
            for weekday in range(7):
                connection.execute(text("""INSERT INTO location_hours (tenant_id,location_id,weekday,opens_at,closes_at)
                                           VALUES (:tenant,:east,:weekday,'09:00','17:00'),
                                                  (:tenant,:west,:weekday,'09:00','17:00')"""),
                                   {"tenant": created_tenant, "east": first_location,
                                    "west": second_location, "weekday": weekday})
            manager_id = connection.execute(text("INSERT INTO identity_users (issuer,subject) VALUES (:issuer,:subject) RETURNING id"),
                                            {"issuer": manager.issuer, "subject": manager.subject}).scalar_one()
            analyst_id = connection.execute(text("INSERT INTO identity_users (issuer,subject) VALUES (:issuer,:subject) RETURNING id"),
                                            {"issuer": analyst.issuer, "subject": analyst.subject}).scalar_one()
            connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'manager'),(:tenant,:analyst,'analyst')"),
                               {"tenant": created_tenant, "user": str(manager_id), "analyst": str(analyst_id)})
            connection.execute(text("INSERT INTO membership_locations (tenant_id,user_id,location_id) VALUES (:tenant,:user,:location)"),
                               {"tenant": created_tenant, "user": str(manager_id), "location": first_location})

        owner_client = TestClient(owner_app)
        base = f"/v1/tenants/{created_tenant}"
        service_id = owner_client.post(f"{base}/services", json={
            "name": "Scope Cut", "duration_minutes": 30, "price_minor": 3000}).json()["id"]
        staff_id = owner_client.post(f"{base}/staff", json={"display_name": "West Stylist"}).json()["id"]
        assert owner_client.put(f"{base}/staff/{staff_id}/locations",
                                json={"location_ids": [second_location]}).status_code == 200
        assert owner_client.put(f"{base}/staff/{staff_id}/qualifications",
                                json={"service_ids": [service_id]}).status_code == 200
        day = date.today() + timedelta(days=7)
        assert owner_client.post(f"{base}/staff/{staff_id}/shifts", json={
            "location_id": second_location, "weekday": day.weekday(),
            "starts_at": "09:00", "ends_at": "17:00"}).status_code == 201
        client_id = owner_client.post(f"{base}/clients", json={
            "name": "West Client", "preferred_location_id": second_location}).json()["id"]
        starts_at = datetime.combine(day, datetime.min.time()).replace(
            hour=10, tzinfo=ZoneInfo("America/Toronto")).isoformat()
        booking = owner_client.post(f"{base}/appointments", json={
            "location_id": second_location, "client_id": client_id, "staff_id": staff_id,
            "service_id": service_id, "starts_at": starts_at, "source": "staff",
            "idempotency_key": "owner-west-booking"})
        assert booking.status_code == 201, booking.text
        appointment_id = booking.json()["appointment_id"]
        assert owner_client.get(f"/v1/tenants/{created_tenant}/locations/{second_location}/hours").status_code == 200
        assert owner_client.get(f"/v1/tenants/{created_tenant}/appointments").json()["total"] == 1

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
        hours = f"/v1/tenants/{created_tenant}/locations"
        assert manager_client.get(f"{hours}/{first_location}/hours").status_code == 200
        assert manager_client.get(f"{hours}/{second_location}/hours").status_code == 404
        assert manager_client.put(f"{hours}/{second_location}/hours",
                                  json={"intervals": []}).status_code == 404
        assert manager_client.get(f"{hours}/{second_location}/exceptions").status_code == 404
        assert manager_client.post(f"{hours}/{second_location}/exceptions", json={
            "exception_date": (date.today() + timedelta(days=8)).isoformat(),
            "kind": "closed"}).status_code == 404
        assert manager_client.get(f"{hours}/{second_location}/public-hours").status_code == 404
        scoped_calendar = manager_client.get(f"/v1/tenants/{created_tenant}/appointments")
        assert scoped_calendar.status_code == 200, scoped_calendar.text
        assert scoped_calendar.json()["items"] == []
        assert scoped_calendar.json()["total"] == 0
        assert manager_client.get(f"/v1/tenants/{created_tenant}/appointments",
                                  params={"location_id": second_location}).status_code == 404
        denied_booking = manager_client.post(f"/v1/tenants/{created_tenant}/appointments", json={
            "location_id": second_location, "client_id": str(uuid.uuid4()),
            "staff_id": str(uuid.uuid4()), "service_id": str(uuid.uuid4()),
            "starts_at": "2027-01-05T15:00:00+00:00", "source": "staff",
            "idempotency_key": "location-scope-test"})
        assert denied_booking.status_code == 404
        assert denied_booking.json()["detail"]["code"] == "location_not_found"
        denied_cancel = manager_client.post(
            f"/v1/tenants/{created_tenant}/appointments/{appointment_id}/cancel",
            json={"expected_version": 1, "reason": "scope test", "idempotency_key": "denied-cancel-west"})
        assert denied_cancel.status_code == 404
        denied_move = manager_client.post(
            f"/v1/tenants/{created_tenant}/appointments/{appointment_id}/reschedule",
            json={"expected_version": 1, "starts_at": starts_at,
                  "idempotency_key": "denied-move-west"})
        assert denied_move.status_code == 404
        denied_transition = manager_client.post(
            f"/v1/tenants/{created_tenant}/appointments/{appointment_id}/transitions",
            json={"expected_version": 1, "to_status": "cancelled",
                  "idempotency_key": "denied-transition-west", "reason": "scope test"})
        assert denied_transition.status_code == 404
        assert manager_client.get(
            f"/v1/tenants/{created_tenant}/appointments/{appointment_id}/receipt-entries").status_code == 404
        denied_receipt = manager_client.post(
            f"/v1/tenants/{created_tenant}/appointments/{appointment_id}/receipt-entries",
            json={"kind": "payment", "amount_minor": 100, "method": "cash",
                  "idempotency_key": "denied-receipt-west"})
        assert denied_receipt.status_code == 404

        analyst_app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
        analyst_app.dependency_overrides[require_identity] = lambda: analyst
        assert TestClient(analyst_app).get(f"/v1/tenants/{created_tenant}/summary").status_code == 403
        assert TestClient(create_app()).get(f"/v1/tenants/{created_tenant}/locations").status_code == 401
        unauthenticated = TestClient(create_app())
        assert unauthenticated.get(f"/v1/tenants/{created_tenant}/appointments").status_code == 401
        assert unauthenticated.get(f"/v1/tenants/{created_tenant}/locations/{first_location}/hours").status_code == 401

        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": created_tenant})
            connection.execute(text("DELETE FROM memberships WHERE tenant_id=:tenant AND user_id=:user"),
                               {"tenant": created_tenant, "user": str(manager_id)})
        assert manager_client.get(f"/v1/tenants/{created_tenant}/locations").status_code == 404
        assert manager_client.get(f"/v1/tenants/{created_tenant}/appointments").status_code == 404
        assert manager_client.get(f"{hours}/{first_location}/hours").status_code == 404
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
