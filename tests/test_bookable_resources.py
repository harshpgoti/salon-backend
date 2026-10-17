"""Tests for bookable resources and service requirements (TASK-018, FR-021)."""

import os
import uuid

import pytest
from sqlalchemy import text
from test_clients import _salon, _client, _teardown
from modules.identity.main import AuthenticatedIdentity


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

        resp = client.put(f"{base}/services/{svc}/resources", json={"location_id": loc, "expected_version": 1, "resource_ids": [chair, room]})
        assert resp.status_code == 200, resp.text
        listed = client.get(f"{base}/services/{svc}/resources", params={"location_id": loc}).json()["items"]
        assert {r["id"] for r in listed} == {chair, room}
        assert all(r["quantity"] == 1 for r in listed)

        # The service flag and the requirement list agree.
        services = client.get(f"{base}/services").json()["items"]
        assert next(s for s in services if s["id"] == svc)["requires_resource"] is True

        # Clearing the requirements clears the flag too.
        assert client.put(f"{base}/services/{svc}/resources", json={"location_id": loc, "expected_version": resp.json()["version"], "resource_ids": []}).status_code == 200
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
        resp = client.put(f"{base}/services/{svc}/resources",
                          json={"location_id": tenant["location_id"], "expected_version": 1, "resource_ids": [foreign]})
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "resource_not_found"
        # Nothing was attached.
        assert client.get(f"{base}/services/{svc}/resources",
                          params={"location_id": tenant["location_id"]}).json()["items"] == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], other_issuer)


def test_service_resource_requirements_are_location_scoped(database_url):
    """AC-2: a service cannot require resources owned by another location."""
    issuer = "https://identity.example.invalid/res-location-scope"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    tenant_id = tenant["tenant_id"]
    location_a = tenant["location_id"]
    location_b = str(uuid.uuid4())
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            connection.execute(
                text("""INSERT INTO locations (tenant_id,id,display_name,timezone,slug,booking_slug)
                        VALUES (:tenant,:id,'Second','America/Toronto',:slug,:slug)"""),
                {"tenant": tenant_id, "id": location_b, "slug": f"second-{location_b}"},
            )
        service = _service(client, base, "Cross-site colour")
        chair_a = client.post(f"{base}/resources", json={"location_id": location_a, "name": "Chair A", "kind": "chair"}).json()["id"]
        room_b = client.post(f"{base}/resources", json={"location_id": location_b, "name": "Room B", "kind": "room"}).json()["id"]

        valid = client.put(f"{base}/services/{service}/resources", json={"location_id": location_a, "expected_version": 1, "resource_ids": [chair_a]})
        assert valid.status_code == 200, valid.text
        assert valid.json()["location_id"] == location_a
        assigned = client.get(f"{base}/services/{service}/resources", params={"location_id": location_a})
        assert assigned.status_code == 200, assigned.text
        assert {item["id"] for item in assigned.json()["items"]} == {chair_a}
        assert all(item["quantity"] == 1 for item in assigned.json()["items"])
        assert client.get(f"{base}/services/{service}/resources", params={"location_id": location_b}).json()["items"] == []

        mismatch = client.put(
            f"{base}/services/{service}/resources",
            json={"location_id": location_a, "expected_version": valid.json()["version"], "resource_ids": [chair_a, room_b]},
        )
        assert mismatch.status_code == 422, mismatch.text
        assert mismatch.json()["detail"]["code"] == "resource_location_mismatch"
        assert {item["id"] for item in client.get(f"{base}/services/{service}/resources", params={"location_id": location_a}).json()["items"]} == {chair_a}
    finally:
        _teardown(engine, [tenant_id], issuer)


def test_resource_routes_enforce_assigned_location_scope(database_url):
    """AC-2: a manager cannot configure a resource at an unassigned location."""
    issuer = "https://identity.example.invalid/res-assigned-location"
    engine, owner, tenant = _salon(database_url, issuer)
    owner_client = _client(database_url, owner)
    tenant_id = tenant["tenant_id"]
    location_a = tenant["location_id"]
    location_b = str(uuid.uuid4())
    manager = AuthenticatedIdentity(issuer, str(uuid.uuid4()))
    base = f"/v1/tenants/{tenant_id}"
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            connection.execute(
                text("""INSERT INTO locations (tenant_id,id,display_name,timezone,slug,booking_slug)
                        VALUES (:tenant,:id,'Second','America/Toronto',:slug,:slug)"""),
                {"tenant": tenant_id, "id": location_b, "slug": f"second-{location_b}"},
            )
            manager_id = connection.execute(
                text("INSERT INTO identity_users (issuer,subject) VALUES (:issuer,:subject) RETURNING id"),
                {"issuer": manager.issuer, "subject": manager.subject},
            ).scalar_one()
            connection.execute(
                text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'manager')"),
                {"tenant": tenant_id, "user": str(manager_id)},
            )
            connection.execute(
                text("INSERT INTO membership_locations (tenant_id,user_id,location_id) VALUES (:tenant,:user,:location)"),
                {"tenant": tenant_id, "user": str(manager_id), "location": location_a},
            )
        service = owner_client.post(f"{base}/services", json={
            "name": "Assigned location service", "duration_minutes": 60, "price_minor": 5000}).json()["id"]
        resource_a = owner_client.post(f"{base}/resources", json={
            "location_id": location_a, "name": "Assigned chair", "kind": "chair"})
        resource_b = owner_client.post(f"{base}/resources", json={
            "location_id": location_b, "name": "Other chair", "kind": "chair"})
        assert resource_a.status_code == resource_b.status_code == 201

        manager_client = _client(database_url, manager)
        visible = manager_client.get(f"{base}/resources")
        assert visible.status_code == 200
        assert {row["id"] for row in visible.json()["items"]} == {resource_a.json()["id"]}
        assert manager_client.get(f"{base}/resources", params={"location_id": location_b}).status_code == 404
        assert manager_client.post(f"{base}/resources", json={
            "location_id": location_b, "name": "Forbidden chair", "kind": "chair"}).status_code == 404
        assert manager_client.put(f"{base}/services/{service}/resources", json={
            "location_id": location_b, "expected_version": 1, "resource_ids": [resource_b.json()["id"]]}).status_code == 404
        assert manager_client.get(f"{base}/services/{service}/resources",
                                  params={"location_id": location_b}).status_code == 404
        assert manager_client.patch(f"{base}/resources/{resource_b.json()['id']}",
                                    json={"active": False}).status_code == 404
    finally:
        _teardown(engine, [tenant_id], issuer)


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
