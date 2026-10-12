"""Tests for bookable resources and service requirements (TASK-018, FR-021)."""

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


def _service(client, base, name="Colour"):
    return client.post(f"{base}/services",
                       json={"name": name, "duration_minutes": 60, "price_minor": 9000}).json()["id"]


def test_resource_requires_owned_location_and_unique_name(database_url):
    """AC-1: a resource belongs to one owned location with a unique name."""
    issuer = "https://identity.example.invalid/res-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc = tenant["location_id"]
        room = client.post(f"{base}/resources", json={"location_id": loc, "name": "Room 1", "kind": "room"})
        assert room.status_code == 201, room.text
        assert room.json()["kind"] == "room" and room.json()["active"] is True

        duplicate = client.post(f"{base}/resources", json={"location_id": loc, "name": "Room 1", "kind": "chair"})
        assert duplicate.status_code == 409

        foreign_location = client.post(f"{base}/resources",
                                       json={"location_id": str(uuid.uuid4()), "name": "Ghost", "kind": "chair"})
        assert foreign_location.status_code == 404

        bad_kind = client.post(f"{base}/resources", json={"location_id": loc, "name": "X", "kind": "spaceship"})
        assert bad_kind.status_code == 422
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_service_requirements_persist_with_quantity_one(database_url):
    """AC-1: a service persists its exclusive resources at capacity one."""
    issuer = "https://identity.example.invalid/res-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc = tenant["location_id"]
        svc = _service(client, base)
        chair = client.post(f"{base}/resources", json={"location_id": loc, "name": "Chair A", "kind": "chair"}).json()["id"]
        room = client.post(f"{base}/resources", json={"location_id": loc, "name": "Wash Room", "kind": "room"}).json()["id"]

        resp = client.put(f"{base}/services/{svc}/resources", json={"resource_ids": [chair, room]})
        assert resp.status_code == 200, resp.text
        listed = client.get(f"{base}/services/{svc}/resources").json()["items"]
        assert {r["id"] for r in listed} == {chair, room}
        assert all(r["quantity"] == 1 for r in listed)

        # The service flag and the requirement list agree.
        services = client.get(f"{base}/services").json()["items"]
        assert next(s for s in services if s["id"] == svc)["requires_resource"] is True

        # Clearing the requirements clears the flag too.
        assert client.put(f"{base}/services/{svc}/resources", json={"resource_ids": []}).status_code == 200
        services = client.get(f"{base}/services").json()["items"]
        assert next(s for s in services if s["id"] == svc)["requires_resource"] is False
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_cross_tenant_resource_requirement_is_rejected(database_url):
    """AC-2: a requirement cannot reference a resource from another tenant."""
    issuer = "https://identity.example.invalid/res-3"
    other_issuer = "https://identity.example.invalid/res-3b"
    engine, owner, tenant = _salon(database_url, issuer)
    other_engine, other_owner, other_tenant = _salon(database_url, other_issuer)
    client = _client(database_url, owner)
    other_client = _client(database_url, other_owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    other_base = f"/v1/tenants/{other_tenant['tenant_id']}"
    try:
        svc = _service(client, base)
        foreign = other_client.post(f"{other_base}/resources",
                                    json={"location_id": other_tenant["location_id"],
                                          "name": "Foreign chair", "kind": "chair"}).json()["id"]
        resp = client.put(f"{base}/services/{svc}/resources", json={"resource_ids": [foreign]})
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "resource_not_found"
        # Nothing was attached.
        assert client.get(f"{base}/services/{svc}/resources").json()["items"] == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], other_issuer)


def test_deactivation_keeps_resource_and_hides_it(database_url):
    """AC-1: deactivating a resource hides it without deleting its history."""
    issuer = "https://identity.example.invalid/res-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        loc = tenant["location_id"]
        rid = client.post(f"{base}/resources", json={"location_id": loc, "name": "Chair B", "kind": "chair"}).json()["id"]
        assert client.patch(f"{base}/resources/{rid}", json={"active": False}).status_code == 200
        assert client.get(f"{base}/resources").json()["items"] == []
        assert len(client.get(f"{base}/resources", params={"include_inactive": "true"}).json()["items"]) == 1

        bad = client.patch(f"{base}/resources/{rid}", json={"active": "yes"})
        assert bad.status_code == 422
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_resources_are_tenant_scoped(database_url):
    """AC-2: resources are invisible across tenants."""
    issuer = "https://identity.example.invalid/res-5"
    other_issuer = "https://identity.example.invalid/res-5b"
    engine, owner, tenant = _salon(database_url, issuer)
    other_engine, other_owner, other_tenant = _salon(database_url, other_issuer)
    client = _client(database_url, owner)
    other_client = _client(database_url, other_owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    other_base = f"/v1/tenants/{other_tenant['tenant_id']}"
    try:
        rid = client.post(f"{base}/resources",
                          json={"location_id": tenant["location_id"], "name": "Chair C", "kind": "chair"}).json()["id"]
        assert other_client.get(f"{other_base}/resources").json()["items"] == []
        assert other_client.patch(f"{other_base}/resources/{rid}", json={"active": False}).status_code == 404
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], other_issuer)