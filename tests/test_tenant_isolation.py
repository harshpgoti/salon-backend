from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.identity.main import AuthenticatedIdentity, create_app, require_identity
from salon_platform.tenant import TenantContextError, list_tenant_locations, set_tenant_context, tenant_transaction


def test_tenant_queries_pool_reuse_rls_and_composite_job_references():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("set DATABASE_URL to run PostgreSQL tenant isolation integration")
    engine = create_engine(database_url, pool_size=1, max_overflow=0, pool_pre_ping=True)
    issuer = "https://identity.example.invalid/rls-test"
    owner_a = AuthenticatedIdentity(issuer, str(uuid.uuid4()))
    owner_b = AuthenticatedIdentity(issuer, str(uuid.uuid4()))
    app_a = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app_a.dependency_overrides[require_identity] = lambda: owner_a
    app_b = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app_b.dependency_overrides[require_identity] = lambda: owner_b
    tenants = []

    def onboard(client, name):
        response = TestClient(client).post("/v1/onboarding", json={"business_name":name, "location_name":"Main", "timezone":"America/Toronto"})
        assert response.status_code == 201
        tenants.append(response.json()["tenant_id"])
        return response.json()

    try:
        first = onboard(app_a, "Synthetic Isolation A")
        second = onboard(app_b, "Synthetic Isolation B")
        tenant_a, tenant_b = tenants

        with tenant_transaction(engine, tenant_a) as connection:
            assert {row["id"] for row in list_tenant_locations(connection, tenant_a)} == {first["location_id"]}
            with pytest.raises(TenantContextError):
                set_tenant_context(connection, tenant_b)
            with pytest.raises(TenantContextError):
                list_tenant_locations(connection, tenant_b)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM tenants")).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM locations WHERE tenant_id=:other"), {"other": tenant_b}).scalar_one() == 0

        file_a, job_a, job_b = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
        with tenant_transaction(engine, tenant_a) as connection:
            connection.execute(text("INSERT INTO tenant_files (tenant_id,id,object_key,purpose,state) VALUES (:tenant,:id,'synthetic/a','import','available')"),
                               {"tenant": tenant_a, "id": file_a})
            connection.execute(text("INSERT INTO tenant_jobs (tenant_id,id,kind,state) VALUES (:tenant,:id,'synthetic_import','queued')"),
                               {"tenant": tenant_a, "id": job_a})
            assert connection.execute(text("SELECT count(*) FROM tenant_files")).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM tenant_files WHERE tenant_id=:other"), {"other": tenant_b}).scalar_one() == 0
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(text("INSERT INTO tenant_job_attempts (tenant_id,job_id,attempt_no,state) VALUES (:tenant,:job,1,'running')"),
                                       {"tenant": tenant_a, "job": job_b})
        with tenant_transaction(engine, tenant_b) as connection:
            connection.execute(text("INSERT INTO tenant_jobs (tenant_id,id,kind,state) VALUES (:tenant,:id,'synthetic_export','queued')"),
                               {"tenant": tenant_b, "id": job_b})
            assert connection.execute(text("SELECT id::text FROM tenant_jobs WHERE tenant_id=:other"), {"other": tenant_a}).scalars().all() == []
            assert connection.execute(text("SELECT count(*) FROM tenant_files")).scalar_one() == 0
            connection.execute(text("INSERT INTO tenant_job_attempts (tenant_id,job_id,attempt_no,state) VALUES (:tenant,:job,1,'running')"),
                               {"tenant": tenant_b, "job": job_b})
        with tenant_transaction(engine, tenant_a) as connection:
            assert connection.execute(text("SELECT count(*) FROM tenant_jobs")).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM tenant_job_attempts")).scalar_one() == 0
        with tenant_transaction(engine, tenant_b) as connection:
            assert connection.execute(text("SELECT count(*) FROM tenant_jobs")).scalar_one() == 1
            assert connection.execute(text("SELECT count(*) FROM tenant_job_attempts")).scalar_one() == 1
    finally:
        if tenants:
            with engine.begin() as connection:
                for tenant_id in tenants:
                    connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
                    connection.execute(text("UPDATE identity_users SET tenant_id=NULL,onboarding_fingerprint=NULL WHERE tenant_id=:tenant"), {"tenant": tenant_id})
                    connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": tenant_id})
                connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
                connection.execute(text("DELETE FROM identity_users WHERE issuer=:issuer AND subject IN (:a,:b)"),
                                   {"issuer": issuer, "a": owner_a.subject, "b": owner_b.subject})
        engine.dispose()
