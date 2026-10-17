"""Dashboard session and synthetic-only demo entrypoint regressions."""
import os
import uuid
import hashlib
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from modules.identity.main import create_app

ORIGIN = "http://127.0.0.1:3000"

@pytest.mark.parametrize("environment,enabled", [("production", "true"), ("test", "true"), ("demo", "false")])
def test_demo_login_disabled_by_default(monkeypatch, environment, enabled):
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setenv("DEMO_AUTH_ENABLED", enabled)
    client = TestClient(create_app(lambda: True))
    response = client.post("/v1/auth/staff/demo", json={"email":"salon@example.invalid", "password":"SalonDemo!2026"}, headers={"Origin":ORIGIN})
    assert response.status_code == 404
    assert "set-cookie" not in response.headers

@pytest.mark.parametrize("surface", ["staff", "company"])
def test_session_requires_real_cookie(surface):
    client = TestClient(create_app(lambda: True))
    assert client.get(f"/v1/auth/{surface}/session").status_code == 401

@pytest.fixture
def demo(monkeypatch):
    url = os.environ.get("DEMO_DATABASE_URL")
    if not url:
        pytest.skip("isolated synthetic demo database required")
    monkeypatch.setenv("APP_ENV", "demo")
    monkeypatch.setenv("DEMO_AUTH_ENABLED", "true")
    monkeypatch.setenv("WEB_BASE_URL", ORIGIN)
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    return TestClient(create_app(lambda: True, engine_factory=lambda: create_engine(url)), base_url=ORIGIN)

def login(client, surface):
    email = "admin@example.invalid" if surface == "company" else "salon@example.invalid"
    return client.post(f"/v1/auth/{surface}/demo", json={"email":email,"password":"SalonDemo!2026"}, headers={"Origin":ORIGIN})

def test_demo_rejects_origin_and_credentials(demo):
    assert demo.post("/v1/auth/staff/demo",json={"email":"salon@example.invalid","password":"SalonDemo!2026"}).status_code == 403
    bad = demo.post("/v1/auth/staff/demo",json={"email":"salon@example.invalid","password":"wrong"},headers={"Origin":ORIGIN})
    assert bad.status_code == 401
    assert "set-cookie" not in bad.headers

def test_salon_session_workspace_and_logout(demo):
    response = login(demo, "staff")
    assert response.status_code == 200, response.text
    assert "HttpOnly" in response.headers["set-cookie"]
    assert "SameSite=lax" in response.headers["set-cookie"]
    session = demo.get("/v1/auth/staff/session")
    assert session.status_code == 200, session.text
    workspaces = session.json()["workspaces"]
    assert len(workspaces) == 1
    assert workspaces[0]["business_name"] == "Maple Demo Salon"
    assert workspaces[0]["role"] == "owner"
    assert demo.get("/v1/company/tenants").status_code == 401
    assert demo.get("/v1/auth/company/session").status_code == 401
    tid = workspaces[0]["tenant_id"]
    assert demo.get(f"/v1/tenants/{tid}/services").status_code == 200
    assert demo.get(f"/v1/tenants/{uuid.uuid4()}/services").status_code == 404
    token = demo.cookies.get("salon_staff_session")
    assert demo.post("/v1/auth/staff/logout",headers={"Origin":ORIGIN}).status_code == 204
    assert demo.get("/v1/auth/staff/session").status_code == 401
    demo.cookies.set("salon_staff_session", token)
    assert demo.get("/v1/auth/staff/session").status_code == 401

def test_company_session_has_no_salon_power(demo):
    response = login(demo, "company")
    assert response.status_code == 200, response.text
    assert "salon_company_session" in response.headers["set-cookie"]
    session = demo.get("/v1/auth/company/session")
    assert session.status_code == 200
    assert session.json()["role"] == "admin"
    assert session.json()["workspaces"] == []
    assert demo.get("/v1/company/tenants").status_code == 200
    assert demo.get("/v1/auth/staff/session").status_code == 401
    assert demo.get(f"/v1/tenants/{uuid.uuid4()}/services").status_code == 401

def test_demo_cannot_use_integration_database(monkeypatch):
    monkeypatch.setenv("APP_ENV", "demo")
    monkeypatch.setenv("DEMO_AUTH_ENABLED", "true")
    monkeypatch.setenv("WEB_BASE_URL", ORIGIN)
    url = os.environ["DATABASE_URL"]
    client = TestClient(create_app(lambda: True, engine_factory=lambda:create_engine(url)))
    response = login(client, "staff")
    assert response.status_code == 503
    assert "set-cookie" not in response.headers


def test_manual_booking_uses_selected_staff_location_and_replays(demo):
    assert login(demo,"staff").status_code == 200
    tid = demo.get("/v1/auth/staff/session").json()["workspaces"][0]["tenant_id"]
    services=demo.get(f"/v1/tenants/{tid}/services").json()["items"]
    staff=demo.get(f"/v1/tenants/{tid}/staff").json()["items"]
    clients=demo.get(f"/v1/tenants/{tid}/clients/search").json()["items"]
    engine=create_engine(os.environ["DEMO_DATABASE_URL"])
    location=str(uuid.uuid4()); appointment=None
    from salon_platform.tenant import set_tenant_context
    try:
        with engine.begin() as conn:
            set_tenant_context(conn,tid)
            conn.execute(text("INSERT INTO locations (tenant_id,id,display_name,timezone,slug,booking_slug) VALUES (:t,:l,'Second demo location','America/Vancouver',:s,:s)"),{"t":tid,"l":location,"s":"demo-"+uuid.uuid4().hex})
            conn.execute(text("INSERT INTO staff_locations (tenant_id,staff_id,location_id) VALUES (:t,:s,:l)"),{"t":tid,"s":staff[0]["id"],"l":location})
            for weekday in range(7):
                conn.execute(text("""INSERT INTO location_hours
                    (tenant_id,location_id,weekday,opens_at,closes_at,effective_from)
                    VALUES (:t,:l,:weekday,'00:00','23:59',CURRENT_DATE)"""),
                    {"t":tid,"l":location,"weekday":weekday})
                conn.execute(text("""INSERT INTO staff_shifts
                    (tenant_id,staff_id,location_id,weekday,starts_at,ends_at,effective_from)
                    VALUES (:t,:staff,:l,:weekday,'00:00','23:59',CURRENT_DATE)"""),
                    {"t":tid,"staff":staff[0]["id"],"l":location,"weekday":weekday})
        local_start = (datetime.now(ZoneInfo("America/Vancouver")) + timedelta(days=20)).replace(
            hour=12, minute=0, second=0, microsecond=0)
        payload={"location_id":location,"client_id":clients[0]["id"],"staff_id":staff[0]["id"],"service_id":services[0]["id"],"starts_at":local_start.astimezone(timezone.utc).isoformat(),"idempotency_key":str(uuid.uuid4())}
        response=demo.post(f"/v1/tenants/{tid}/appointments",json=payload,headers={"Origin":ORIGIN})
        assert response.status_code == 201,response.text
        appointment=response.json()["appointment_id"]
        report=demo.get(f"/v1/tenants/{tid}/reports/today",params={"day":payload["starts_at"][:10]}).json()
        assert next(a for a in report["appointments"] if a["id"]==appointment)["local_timezone"]=="America/Vancouver"
        replay=demo.post(f"/v1/tenants/{tid}/appointments",json=payload,headers={"Origin":ORIGIN})
        assert replay.json()["appointment_id"] == appointment
        with engine.begin() as conn:
            set_tenant_context(conn,tid)
            row=conn.execute(text("SELECT location_id,local_timezone FROM appointments WHERE tenant_id=:t AND id=:a"),{"t":tid,"a":appointment}).one()
            assert str(row.location_id)==location
            assert row.local_timezone=="America/Vancouver"
            assert conn.execute(text("SELECT count(*) FROM appointments WHERE tenant_id=:t AND idempotency_key=:k"),{"t":tid,"k":payload["idempotency_key"]}).scalar_one()==1
        payload["idempotency_key"]=str(uuid.uuid4());payload["location_id"]=str(uuid.uuid4())
        assert demo.post(f"/v1/tenants/{tid}/appointments",json=payload,headers={"Origin":ORIGIN}).status_code==409
    finally:
        with engine.begin() as conn:
            set_tenant_context(conn,tid)
            if appointment:conn.execute(text("DELETE FROM appointments WHERE tenant_id=:t AND id=:a"),{"t":tid,"a":appointment})
            conn.execute(text("DELETE FROM staff_locations WHERE tenant_id=:t AND location_id=:l"),{"t":tid,"l":location})
            conn.execute(text("DELETE FROM locations WHERE tenant_id=:t AND id=:l"),{"t":tid,"l":location})
        engine.dispose()


def test_non_ascii_demo_credentials_are_denied(demo):
    response=demo.post("/v1/auth/staff/demo",json={"email":"salon@example.invalid","password":"wrong-é"},headers={"Origin":ORIGIN})
    assert response.status_code==401
    assert "set-cookie" not in response.headers


def test_expired_session_is_rejected(demo):
    assert login(demo,"staff").status_code==200
    token=demo.cookies.get("salon_staff_session")
    engine=create_engine(os.environ["DEMO_DATABASE_URL"])
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE auth_sessions SET created_at=now()-interval '1 day', expires_at=now()-interval '1 hour' WHERE session_hash=:hash"),{"hash":hashlib.sha256(token.encode()).hexdigest()})
        assert demo.get("/v1/auth/staff/session").status_code==401
    finally:engine.dispose()

def test_demo_seed_recovers_after_real_transaction_fault_and_concurrent_retry(demo):
    from concurrent.futures import ThreadPoolExecutor
    from sqlalchemy import event
    from modules.identity.dashboard import _seed_salon
    from modules.identity.main import AuthenticatedIdentity,_create_tenant,OnboardingRequest
    from salon_platform.tenant import set_tenant_context
    from test_clients import _teardown
    engine=create_engine(os.environ["DEMO_DATABASE_URL"])
    issuer="urn:salon:seed-test:"+uuid.uuid4().hex
    identity=AuthenticatedIdentity(issuer,"seed-owner")
    tid=None
    def fault(conn,cursor,statement,parameters,context,executemany):
        if statement.startswith("INSERT INTO staff_profiles"):
            raise RuntimeError("synthetic seed transaction fault")
    try:
        event.listen(engine,"before_cursor_execute",fault)
        with pytest.raises(RuntimeError,match="synthetic seed transaction fault"):
            _seed_salon(engine,identity,_create_tenant,OnboardingRequest)
        event.remove(engine,"before_cursor_execute",fault)
        with engine.begin() as conn:
            tid=str(conn.execute(text("SELECT tenant_id FROM identity_users WHERE issuer=:issuer AND subject='seed-owner'"),{"issuer":issuer}).scalar_one())
            set_tenant_context(conn,tid)
            assert conn.execute(text("SELECT count(*) FROM services WHERE tenant_id=:t"),{"t":tid}).scalar_one()==0
            assert conn.execute(text("SELECT count(*) FROM tenant_setup_steps WHERE tenant_id=:t AND step='business_identity' AND state='complete'"),{"t":tid}).scalar_one()==0
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _: _seed_salon(engine,identity,_create_tenant,OnboardingRequest),range(2)))
        _seed_salon(engine,identity,_create_tenant,OnboardingRequest)
        with engine.begin() as conn:
            set_tenant_context(conn,tid)
            for table,expected in [("clients",3),("services",3),("staff_profiles",2),("staff_shifts",14)]:
                assert conn.execute(text("SELECT count(*) FROM "+table+" WHERE tenant_id=:t"),{"t":tid}).scalar_one()==expected
            assert conn.execute(text("SELECT state FROM tenant_setup_steps WHERE tenant_id=:t AND step='business_identity'"),{"t":tid}).scalar_one()=="complete"
    finally:
        if tid:_teardown(engine,[tid],issuer)
        else:engine.dispose()

def test_workspace_discovery_never_lists_foreign_salon(demo):
    from test_clients import _salon,_teardown
    issuer="urn:salon:foreign-workspace:"+uuid.uuid4().hex
    engine,owner,foreign=_salon(os.environ["DEMO_DATABASE_URL"],issuer)
    try:
        assert login(demo,"staff").status_code==200
        workspaces=demo.get("/v1/auth/staff/session").json()["workspaces"]
        assert foreign["tenant_id"] not in {w["tenant_id"] for w in workspaces}
        assert demo.get('/v1/tenants/'+foreign["tenant_id"]+'/services').status_code==404
    finally:_teardown(engine,[foreign["tenant_id"]],issuer)
