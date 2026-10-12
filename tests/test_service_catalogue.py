"""Tests for the versioned service catalogue (TASK-015, FR-019)."""

import os
import uuid

import pytest
from test_clients import _salon, _client, _teardown


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _service(client, base, **overrides):
    payload = {"name": "Cut and Finish", "duration_minutes": 45, "price_minor": 6500, **overrides}
    return client.post(f"{base}/services", json=payload)


def test_service_duration_buffers_and_price_validate(database_url):
    """AC-1: duration, buffers, price and rebooking interval persist and validate."""
    issuer = "https://identity.example.invalid/cat-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        cat = client.post(f"{base}/service-categories", json={"name": "Colour"})
        assert cat.status_code == 201
        resp = _service(client, base, category_id=cat.json()["id"], pre_buffer_minutes=10,
                        post_buffer_minutes=15, rebooking_interval_days=42, requires_resource=True)
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["duration_minutes"] == 45 and body["price_minor"] == 6500
        assert body["currency"] == "CAD" and body["rebooking_interval_days"] == 42
        assert body["pre_buffer_minutes"] == 10 and body["post_buffer_minutes"] == 15

        # Out-of-range and negative values are refused, not clamped.
        assert _service(client, base, name="Too short", duration_minutes=1).status_code == 422
        assert _service(client, base, name="Negative price", price_minor=-1).status_code == 422
        assert _service(client, base, name="Huge buffer", post_buffer_minutes=999).status_code == 422
        assert _service(client, base, name="Bad rebook", rebooking_interval_days=0).status_code == 422
        # A category from nowhere does not exist.
        assert _service(client, base, name="Orphan", category_id=str(uuid.uuid4())).status_code == 404
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_service_update_requires_version_and_bumps_it(database_url):
    """AC-1: an edit carries the version it read; a stale edit is refused."""
    issuer = "https://identity.example.invalid/cat-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        svc = _service(client, base).json()
        updated = client.patch(f"{base}/services/{svc['id']}",
                               json={"price_minor": 7000, "expected_version": svc["version"]})
        assert updated.status_code == 200
        assert updated.json()["price_minor"] == 7000 and updated.json()["version"] == svc["version"] + 1

        stale = client.patch(f"{base}/services/{svc['id']}",
                             json={"price_minor": 100, "expected_version": svc["version"]})
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "service_version_conflict"
        # The refused edit did not apply.
        assert client.get(f"{base}/services").json()["items"][0]["price_minor"] == 7000
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_archive_retains_snapshot_and_is_reversible(database_url):
    """AC-2: archiving hides the service but keeps the row, and can be undone."""
    issuer = "https://identity.example.invalid/cat-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        svc = _service(client, base).json()
        archived = client.post(f"{base}/services/{svc['id']}/archive")
        assert archived.status_code == 200 and archived.json()["archived_at"]

        # Hidden from the default list, still present with include_archived.
        assert client.get(f"{base}/services").json()["items"] == []
        with_archived = client.get(f"{base}/services", params={"include_archived": "true"}).json()["items"]
        assert len(with_archived) == 1 and with_archived[0]["price_minor"] == 6500

        # Archiving twice is a no-op, not an error.
        assert client.post(f"{base}/services/{svc['id']}/archive").json()["already_archived"] is True

        restored = client.post(f"{base}/services/{svc['id']}/restore")
        assert restored.status_code == 200 and restored.json()["archived_at"] is None
        assert len(client.get(f"{base}/services").json()["items"]) == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_staff_override_upserts_one_row_per_staff(database_url):
    """AC-1: a staff override replaces the prior one rather than duplicating."""
    issuer = "https://identity.example.invalid/cat-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        svc = _service(client, base).json()
        staff = str(uuid.uuid4())
        first = client.put(f"{base}/services/{svc['id']}/overrides",
                           json={"staff_ref": staff, "duration_minutes": 60, "price_minor": 8000})
        assert first.status_code == 200
        second = client.put(f"{base}/services/{svc['id']}/overrides",
                            json={"staff_ref": staff, "duration_minutes": 90, "price_minor": 9000})
        assert second.status_code == 200
        assert second.json()["version"] == first.json()["version"] + 1

        listed = client.get(f"{base}/services/{svc['id']}/overrides").json()["items"]
        assert len(listed) == 1 and listed[0]["duration_minutes"] == 90
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_catalogue_is_tenant_scoped(database_url):
    """AC-2: a service belongs to one tenant and cannot be read or edited by another."""
    issuer = "https://identity.example.invalid/cat-5"
    other_issuer = "https://identity.example.invalid/cat-5b"
    engine, owner, tenant = _salon(database_url, issuer)
    other_engine, other_owner, other_tenant = _salon(database_url, other_issuer)
    client = _client(database_url, owner)
    other_client = _client(database_url, other_owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    other_base = f"/v1/tenants/{other_tenant['tenant_id']}"
    try:
        svc = _service(client, base).json()
        assert other_client.get(f"{other_base}/services").json()["items"] == []
        assert other_client.patch(f"{other_base}/services/{svc['id']}",
                                  json={"price_minor": 1, "expected_version": 1}).status_code == 404
        assert other_client.post(f"{other_base}/services/{svc['id']}/archive").status_code == 404
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], other_issuer)


def test_reader_cannot_manage_services(database_url):
    """AC-1: a read-only role is refused a write."""
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine, text
    from modules.identity.main import AuthenticatedIdentity, create_app, require_identity

    issuer = "https://identity.example.invalid/cat-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    analyst = AuthenticatedIdentity(issuer, str(uuid.uuid4()))
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            uid = connection.execute(text("INSERT INTO identity_users (issuer,subject) VALUES (:i,:s) RETURNING id"),
                                     {"i": issuer, "s": analyst.subject}).scalar_one()
            connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:t,:u,'analyst')"),
                               {"t": tenant["tenant_id"], "u": str(uid)})
        app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
        app.dependency_overrides[require_identity] = lambda: analyst
        analyst_client = TestClient(app)
        assert analyst_client.get(f"{base}/services").status_code == 403
        assert analyst_client.post(f"{base}/services",
                                   json={"name": "Nope", "duration_minutes": 30, "price_minor": 1}).status_code == 403
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)