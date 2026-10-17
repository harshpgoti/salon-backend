"""Fresh synthetic demo initialization exposes owner-editable bookable hours."""

import hashlib
import os
import secrets
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text

from modules.identity.main import AuthenticatedIdentity, OnboardingRequest, _create_tenant, create_app
from modules.identity.dashboard import _seed_salon
from salon_platform.tenant import set_tenant_context
from test_clients import _teardown

ORIGIN = "http://127.0.0.1:3000"


@pytest.fixture
def demo(monkeypatch):
    url = os.environ.get("DEMO_DATABASE_URL")
    if not url:
        pytest.fail("DEMO_DATABASE_URL must select the isolated synthetic database")
    monkeypatch.setenv("APP_ENV", "demo")
    monkeypatch.setenv("DEMO_AUTH_ENABLED", "true")
    monkeypatch.setenv("WEB_BASE_URL", ORIGIN)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    return TestClient(create_app(lambda: True, engine_factory=lambda: create_engine(url)), base_url=ORIGIN)


def test_new_demo_tenant_gets_bookable_hours_and_seed_retry_preserves_owner_edit(demo):
    url = os.environ["DEMO_DATABASE_URL"]
    engine = create_engine(url)
    issuer = "urn:salon:demo-hours:" + uuid.uuid4().hex
    subject = "demo-owner"
    identity = AuthenticatedIdentity(issuer, subject, email="hours-owner@example.invalid",
                                     email_verified=True, auth_time=time.time())
    tenant_id = None
    token = None
    fault_reached = False

    def fail_after_seeded_hours(conn, cursor, statement, parameters, context, executemany):
        nonlocal fault_reached
        if statement.startswith("INSERT INTO staff_profiles"):
            fault_reached = True
            raise RuntimeError("synthetic failure after default hours")

    try:
        event.listen(engine, "before_cursor_execute", fail_after_seeded_hours)
        with pytest.raises(RuntimeError, match="synthetic failure after default hours"):
            _seed_salon(engine, identity, _create_tenant, OnboardingRequest)
        event.remove(engine, "before_cursor_execute", fail_after_seeded_hours)
        assert fault_reached

        with engine.begin() as connection:
            tenant_id = str(connection.execute(
                text("SELECT tenant_id FROM identity_users WHERE issuer=:issuer AND subject=:subject"),
                {"issuer": issuer, "subject": subject}).scalar_one())
            set_tenant_context(connection, tenant_id)
            assert connection.execute(text(
                "SELECT count(*) FROM location_hours WHERE tenant_id=:tenant"),
                {"tenant": tenant_id}).scalar_one() == 0
            assert connection.execute(text(
                "SELECT count(*) FROM tenant_setup_steps WHERE tenant_id=:tenant "
                "AND step='business_identity' AND state='complete'"),
                {"tenant": tenant_id}).scalar_one() == 0

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: _seed_salon(engine, identity, _create_tenant, OnboardingRequest), range(2)))

        token = secrets.token_urlsafe(32)
        with engine.begin() as connection:
            set_tenant_context(connection, tenant_id)
            location = connection.execute(text(
                "SELECT id::text AS id, booking_slug FROM locations WHERE tenant_id=:tenant"),
                {"tenant": tenant_id}).mappings().one()
            connection.execute(text(
                "INSERT INTO auth_sessions (session_hash,surface,issuer,subject,expires_at) "
                "VALUES (:hash,'staff',:issuer,:subject,now()+interval '8 hours')"),
                {"hash": hashlib.sha256(token.encode()).hexdigest(), "issuer": issuer, "subject": subject})
        demo.cookies.set("salon_staff_session", token, domain="127.0.0.1", path="/")

        hours_url = f"/v1/tenants/{tenant_id}/locations/{location['id']}/hours"
        initial = demo.get(hours_url)
        assert initial.status_code == 200, initial.text
        assert [(row["weekday"], row["opens_at"], row["closes_at"])
                for row in initial.json()["items"]] == [
                    (weekday, "09:00", "18:00") for weekday in range(7)]
        staff = demo.get(f"/v1/tenants/{tenant_id}/staff")
        assert staff.status_code == 200, staff.text
        assert len(staff.json()["items"]) == 2
        for member in staff.json()["items"]:
            shifts = demo.get(f"/v1/tenants/{tenant_id}/staff/{member['id']}/shifts")
            assert shifts.status_code == 200, shifts.text
            assert [(row["weekday"], row["starts_at"], row["ends_at"])
                    for row in shifts.json()["items"]] == [
                        (weekday, "09:00", "18:00") for weekday in range(7)]

        services = demo.get(f"/v1/tenants/{tenant_id}/services")
        assert services.status_code == 200, services.text
        service_id = services.json()["items"][0]["id"]
        visit_day = datetime.now(ZoneInfo("America/Toronto")).date() + timedelta(days=7)
        availability_url = f"/v1/public/salons/{location['booking_slug']}/availability"
        query = {"service_id": service_id, "date_from": visit_day.isoformat(),
                 "date_to": visit_day.isoformat()}
        first_slots = demo.get(availability_url, params=query)
        assert first_slots.status_code == 200, first_slots.text
        assert first_slots.json()["slots"]

        edited = demo.put(hours_url, headers={"Origin": ORIGIN}, json={"intervals": [
            {"weekday": weekday, "opens_at": "09:00",
             "closes_at": "16:00" if weekday == visit_day.weekday() else "18:00"}
            for weekday in range(7)]})
        assert edited.status_code == 200, edited.text

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: _seed_salon(engine, identity, _create_tenant, OnboardingRequest), range(2)))

        current = demo.get(hours_url)
        history = demo.get(hours_url, params={"include_history": "true"})
        assert current.status_code == history.status_code == 200
        assert len(current.json()["items"]) == 7
        assert len(history.json()["items"]) == 14
        assert next(row for row in current.json()["items"]
                    if row["weekday"] == visit_day.weekday())["closes_at"] == "16:00"
        owner_slots = demo.get(availability_url, params=query)
        assert owner_slots.status_code == 200, owner_slots.text
        assert owner_slots.json()["slots"]
        assert all(datetime.fromisoformat(slot["ends_at"]).astimezone(ZoneInfo("America/Toronto")).hour <= 16
                   for slot in owner_slots.json()["slots"])
    finally:
        if event.contains(engine, "before_cursor_execute", fail_after_seeded_hours):
            event.remove(engine, "before_cursor_execute", fail_after_seeded_hours)
        if token is not None:
            with engine.begin() as connection:
                connection.execute(text("DELETE FROM auth_sessions WHERE session_hash=:hash"),
                                   {"hash": hashlib.sha256(token.encode()).hexdigest()})
        if tenant_id is not None:
            _teardown(engine, [tenant_id], issuer)
        else:
            engine.dispose()
