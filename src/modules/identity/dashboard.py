"""Session discovery and explicitly isolated local demo login.

Production uses the existing OIDC flow; this entrypoint never accepts credentials
outside APP_ENV=demo and the separate salon_demo PostgreSQL database.
"""
from __future__ import annotations
import hashlib
import hmac
import os
import secrets
import time
import uuid
from urllib.parse import urlparse
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from modules.company import company_scope
from salon_platform.tenant import set_tenant_context

class DemoLogin(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=128)

def demo_enabled():
    return os.getenv("APP_ENV") == "demo" and os.getenv("DEMO_AUTH_ENABLED") == "true"

def _seed_salon(engine, identity, create_tenant, onboarding_request):
    result, _ = create_tenant(engine, identity, onboarding_request(
        business_name="Maple Demo Salon", location_name="Downtown", timezone="America/Toronto"), uuid.uuid4())
    tenant = result["tenant_id"]
    with engine.begin() as connection:
        set_tenant_context(connection, tenant)
        connection.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key,0))"), {"key":"demo-seed:"+tenant})
        if connection.execute(text("SELECT 1 FROM tenant_setup_steps WHERE tenant_id=:tenant AND step='business_identity' AND state='complete'"), {"tenant":tenant}).scalar_one_or_none():
            return
        connection.execute(text("""INSERT INTO location_hours (tenant_id,location_id,weekday,opens_at,closes_at)
            SELECT :tenant,:location,day,'09:00','18:00' FROM generate_series(0,6) AS day
            WHERE NOT EXISTS (SELECT 1 FROM location_hours WHERE tenant_id=:tenant
                AND location_id=:location AND weekday=day AND superseded_at IS NULL)"""),
            {"tenant":tenant,"location":result["location_id"]})
        for name, duration, price in [("Signature haircut",45,6500),("Colour and finish",90,14000),("Blow dry",30,4500)]:
            connection.execute(text("""INSERT INTO services (tenant_id,name,duration_minutes,price_minor)
                VALUES (:tenant,:name,:duration,:price) ON CONFLICT (tenant_id,name) DO NOTHING"""),
                {"tenant":tenant,"name":name,"duration":duration,"price":price})
        for name in ["Alex Demo", "Jordan Demo", "Taylor Demo"]:
            connection.execute(text("INSERT INTO clients (tenant_id,name,source) SELECT :tenant,:name,'staff' WHERE NOT EXISTS (SELECT 1 FROM clients WHERE tenant_id=:tenant AND name=:name)"),
                {"tenant":tenant,"name":name})
        for name in ["Sam Demo", "Jamie Demo"]:
            sid = connection.execute(text("SELECT id FROM staff_profiles WHERE tenant_id=:tenant AND display_name=:name LIMIT 1"),
                {"tenant":tenant,"name":name}).scalar_one_or_none()
            if sid is None:
                sid = connection.execute(text("INSERT INTO staff_profiles (tenant_id,display_name) VALUES (:tenant,:name) RETURNING id"),
                    {"tenant":tenant,"name":name}).scalar_one()
            connection.execute(text("INSERT INTO staff_locations (tenant_id,staff_id,location_id) VALUES (:tenant,:staff,:location) ON CONFLICT (tenant_id,staff_id,location_id) DO NOTHING"),
                {"tenant":tenant,"staff":sid,"location":result["location_id"]})
            connection.execute(text("""INSERT INTO staff_service_qualifications (tenant_id,staff_id,service_id)
                SELECT :tenant,:staff,id FROM services WHERE tenant_id=:tenant ON CONFLICT (tenant_id,staff_id,service_id) DO NOTHING"""), {"tenant":tenant,"staff":sid})
            for weekday in range(7):
                connection.execute(text("""INSERT INTO staff_shifts (tenant_id,staff_id,location_id,weekday,starts_at,ends_at)
                    SELECT :tenant,:staff,:location,:day,'09:00','18:00' WHERE NOT EXISTS (SELECT 1 FROM staff_shifts WHERE tenant_id=:tenant AND staff_id=:staff AND location_id=:location AND weekday=:day)"""),
                    {"tenant":tenant,"staff":sid,"location":result["location_id"],"day":weekday})

        connection.execute(text("INSERT INTO tenant_setup_steps (tenant_id,step,state) VALUES (:tenant,'business_identity','complete') ON CONFLICT (tenant_id,step) DO UPDATE SET state='complete'"), {"tenant":tenant})

def register_dashboard_routes(app, *, require_identity, require_company_identity, identity_type,
                              create_tenant, onboarding_request, cookie_name):
    @app.get("/v1/auth/{surface}/session", tags=["identity"])
    def session(surface: str, request: Request):
        if surface not in {"staff", "company"}:
            raise HTTPException(404)
        identity = require_identity(request) if surface == "staff" else require_company_identity(request)
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity) if surface == "company" else None
                workspaces = [] if surface == "company" else [dict(row) for row in connection.execute(
                    text("SELECT * FROM identity_workspace_list(:issuer,:subject)"),
                    {"issuer":identity.issuer,"subject":identity.subject}).mappings()]
                for workspace in workspaces:
                    workspace["tenant_id"] = str(workspace["tenant_id"])
            return JSONResponse({"surface":surface,"email":identity.email,"role":role,"workspaces":workspaces,"demo":demo_enabled()}, headers={"Cache-Control":"no-store"})
        finally:
            engine.dispose()

    @app.post("/v1/auth/{surface}/demo", tags=["identity"], include_in_schema=False)
    def demo_login(surface: str, payload: DemoLogin, request: Request):
        if not demo_enabled() or surface not in {"staff", "company"}:
            raise HTTPException(404)
        origin = os.getenv("WEB_BASE_URL", "").rstrip("/")
        parsed = urlparse(origin)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.scheme != "http" or request.headers.get("origin") != origin:
            raise HTTPException(403, detail={"code":"origin_rejected"})
        expected = "admin@example.invalid" if surface == "company" else "salon@example.invalid"
        if not (hmac.compare_digest(payload.email.lower().encode(), expected.encode()) and hmac.compare_digest(payload.password.encode(),b"SalonDemo!2026")):
            raise HTTPException(401, detail={"code":"invalid_demo_credentials"})
        engine = app.state.engine_factory()
        try:
            if engine.url.database != "salon_demo":
                raise HTTPException(503, detail={"code":"isolated_demo_database_required"})
            issuer, subject = "urn:salon:local-demo:" + surface, "demo-" + surface
            identity = identity_type(issuer,subject,surface,email=expected,email_verified=True,auth_time=time.time())
            if surface == "staff":
                _seed_salon(engine,identity,create_tenant,onboarding_request)
            with engine.begin() as connection:
                connection.execute(text("""INSERT INTO identity_users (issuer,subject,email,email_verified,auth_time)
                    VALUES (:issuer,:subject,:email,true,now()) ON CONFLICT (issuer,subject) DO UPDATE
                    SET email=EXCLUDED.email,email_verified=true,auth_time=now()"""),
                    {"issuer":issuer,"subject":subject,"email":expected})
                if surface == "company":
                    connection.execute(text("""INSERT INTO company_staff (issuer,subject,display_name,role,mfa_enrolled)
                        VALUES (:issuer,:subject,'Demo Administrator','admin',true) ON CONFLICT (issuer,subject) DO NOTHING"""),
                        {"issuer":issuer,"subject":subject})
                    company_scope(connection,identity)
                token = secrets.token_urlsafe(32)
                connection.execute(text("""INSERT INTO auth_sessions (session_hash,surface,issuer,subject,expires_at)
                    VALUES (:hash,:surface,:issuer,:subject,now()+interval '8 hours')"""),
                    {"hash":hashlib.sha256(token.encode()).hexdigest(),"surface":surface,"issuer":issuer,"subject":subject})
            response = JSONResponse({"redirect":"/admin" if surface == "company" else "/salon"})
            response.set_cookie(cookie_name(surface),token,max_age=28800,httponly=True,samesite="lax",
                                secure=os.getenv("SESSION_COOKIE_SECURE","true").lower()=="true",path="/")
            response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            engine.dispose()
