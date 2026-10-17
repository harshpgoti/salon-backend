"""Tests for client field definitions and values (TASK-011, FR-017)."""

import os
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
import uuid
from test_clients import _salon, _client, _teardown
from sqlalchemy import create_engine, text


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL CRM integration")
    return url


def test_field_definition_crud_and_validation(database_url):
    """AC-1: Field definitions enforce type/validation; versioning works."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/fdef-1")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        # Create text field
        resp = client.post(f"{base}/field-definitions", json={
            "key": "notes", "label": "Internal Notes", "type": "text",
            "validation": {"max_length": 500}, "purpose": "staff notes", "sensitivity": "private"
        })
        assert resp.status_code == 201
        def_id = resp.json()["id"]
        assert resp.json()["key"] == "notes"
        assert resp.json()["type"] == "text"

        # List definitions
        resp = client.get(f"{base}/field-definitions")
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 1

        # Create number field with min/max
        resp = client.post(f"{base}/field-definitions", json={
            "key": "visit_count", "label": "Visit Count", "type": "number",
            "validation": {"min": 0, "max": 1000}, "purpose": "analytics", "sensitivity": "standard"
        })
        assert resp.status_code == 201
        def_id2 = resp.json()["id"]

        # Create enum field
        resp = client.post(f"{base}/field-definitions", json={
            "key": "hair_type", "label": "Hair Type", "type": "enum",
            "validation": {"values": ["straight", "wavy", "curly", "coily"]},
            "purpose": "service matching", "sensitivity": "standard"
        })
        assert resp.status_code == 201
        def_id3 = resp.json()["id"]

        # Update definition (version bump)
        current_version = client.get(f"{base}/field-definitions").json()["items"][0]["version"]
        resp = client.patch(f"{base}/field-definitions/{def_id}",
                            json={"label": "Updated Notes", "expected_version": current_version})
        assert resp.status_code == 200
        assert resp.json()["label"] == "Updated Notes"
        assert resp.json()["version"] == current_version + 1

        # Version conflict
        resp = client.patch(f"{base}/field-definitions/{def_id}",
                            json={"label": "Conflict", "expected_version": current_version})
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "field_definition_version_conflict"

        # Invalid validation for enum (missing values)
        resp = client.post(f"{base}/field-definitions", json={
            "key": "bad_enum", "label": "Bad Enum", "type": "enum",
            "validation": {}, "purpose": "test", "sensitivity": "standard"
        })
        # Enum definitions without a usable schema are rejected before values can be written.
        assert resp.status_code == 422
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/fdef-1")


def test_field_value_validation_and_sensitivity(database_url):
    """AC-1/AC-2: Field values validated; sensitive fields require permission."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/fval-1")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        # Create definitions
        defs = [
            {"key": "public_text", "label": "Public Text", "type": "text",
             "validation": {"max_length": 100}, "purpose": "test", "sensitivity": "standard"},
            {"key": "private_number", "label": "Private Number", "type": "number",
             "validation": {"min": 0, "max": 100}, "purpose": "test", "sensitivity": "private"},
            {"key": "restricted_enum", "label": "Restricted Enum", "type": "enum",
             "validation": {"values": ["a", "b", "c"]}, "purpose": "test", "sensitivity": "restricted"},
        ]
        def_ids = {}
        for d in defs:
            resp = client.post(f"{base}/field-definitions", json=d)
            assert resp.status_code == 201
            def_ids[d["key"]] = resp.json()["id"]

        # Create client
        resp = client.post(f"{base}/clients", json={"name": "Field Test"})
        assert resp.status_code == 201
        client_id = resp.json()["id"]

# Set text value (standard - owner can see)
        resp = client.post(f"{base}/clients/{client_id}/field-values", json={
            "definition_id": def_ids["public_text"], "entity_id": client_id, "value_text": "Hello world"
        })
        assert resp.status_code == 201

        # Set number value (within range)
        resp = client.post(f"{base}/clients/{client_id}/field-values", json={
            "definition_id": def_ids["private_number"], "entity_id": client_id, "value_number": 42
        })
        assert resp.status_code == 201

        # Set number value (out of range -> 422)
        resp = client.post(f"{base}/clients/{client_id}/field-values", json={
            "definition_id": def_ids["private_number"], "entity_id": client_id, "value_number": 200
        })
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "field_value_above_max"

        # Set enum value (valid)
        resp = client.post(f"{base}/clients/{client_id}/field-values", json={
            "definition_id": def_ids["restricted_enum"], "entity_id": client_id, "value_text": "b"
        })
        assert resp.status_code == 201

        # Set enum value (invalid)
        resp = client.post(f"{base}/clients/{client_id}/field-values", json={
            "definition_id": def_ids["restricted_enum"], "entity_id": client_id, "value_text": "z"
        })
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "field_value_not_in_enum"

        # List field values (owner sees all including private/restricted)
        resp = client.get(f"{base}/clients/{client_id}/field-values")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 3

        # Update field value
        val_id = next(i["id"] for i in items if i["key"] == "public_text")
        resp = client.patch(f"{base}/clients/{client_id}/field-values/{val_id}",
                            json={"value_text": "Updated", "expected_version": next(i["version"] for i in items if i["id"] == val_id)})
        assert resp.status_code == 200
        assert resp.json()["value_text"] == "Updated"

        incompatible_schema = client.patch(f"{base}/field-definitions/{def_ids['private_number']}", json={
            "validation": {"min": 0, "max": 10}, "expected_version": 1,
        })
        assert incompatible_schema.status_code == 409
        assert incompatible_schema.json()["detail"]["code"] == "field_definition_in_use"
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/fval-1")


def test_field_value_entity_mismatch(database_url):
    """Field value entity_id must match client_id for client entity."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/fval-2")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        resp = client.post(f"{base}/field-definitions", json={
            "key": "test", "label": "Test", "type": "text", "validation": {}, "purpose": "test", "sensitivity": "standard"
        })
        def_id = resp.json()["id"]

        resp = client.post(f"{base}/clients", json={"name": "Client A"})
        client_id_a = resp.json()["id"]
        resp = client.post(f"{base}/clients", json={"name": "Client B"})
        client_id_b = resp.json()["id"]

        # Try to set value for client A but with entity_id = client B
        resp = client.post(f"{base}/clients/{client_id_a}/field-values", json={
            "definition_id": def_id, "entity_type": "client", "entity_id": client_id_b, "value_text": "bad"
        })
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "entity_id_mismatch"

        value = client.post(f"{base}/clients/{client_id_b}/field-values", json={
            "definition_id": def_id, "entity_type": "client", "entity_id": client_id_b, "value_text": "belongs to B",
        })
        assert value.status_code == 201, value.text
        cross_client_patch = client.patch(f"{base}/clients/{client_id_a}/field-values/{value.json()['id']}", json={
            "value_text": "tampered", "expected_version": 1,
        })
        assert cross_client_patch.status_code == 404
        stored = client.get(f"{base}/clients/{client_id_b}/field-values").json()["items"]
        assert stored[0]["value_text"] == "belongs to B"
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/fval-2")

def test_list_endpoints_return_pagination_envelope(database_url):
    """AC-1: paged list endpoints report total/limit/offset so the UI pager is truthful."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/fpage-1")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        resp = client.post(f"{base}/clients", json={"name": "Paged Client"})
        client_id = resp.json()["id"]
        for index in range(3):
            created = client.post(f"{base}/field-definitions", json={
                "key": f"field_{index}", "label": f"Field {index}", "type": "text",
                "validation": {}, "purpose": "paging check", "sensitivity": "standard"})
            assert created.status_code == 201

        resp = client.get(f"{base}/field-definitions?limit=2&offset=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3 and body["limit"] == 2 and body["offset"] == 0
        assert len(body["items"]) == 2

        resp = client.get(f"{base}/field-definitions?limit=2&offset=2")
        assert len(resp.json()["items"]) == 1

        # A limit outside the accepted range is refused, not silently clamped.
        assert client.get(f"{base}/field-definitions?limit=0").status_code == 422
        assert client.get(f"{base}/field-definitions?offset=-1").status_code == 422
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/fpage-1")


def test_custom_definition_schema_and_sensitive_authorization(database_url):
    """AC-2: malformed validation schemas and unauthorized restricted fields fail closed."""
    from fastapi.testclient import TestClient
    from modules.identity.main import AuthenticatedIdentity, create_app, require_identity

    issuer = "https://identity.example.invalid/fdef-policy"
    engine, owner, tenant = _salon(database_url, issuer)
    owner_client = _client(database_url, owner)
    manager = AuthenticatedIdentity(issuer, str(uuid.uuid4()))
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        invalid = owner_client.post(f"{base}/field-definitions", json={
            "key": "invalid_number", "label": "Invalid", "type": "number",
            "validation": {"min": "not a number"}, "purpose": "synthetic test",
        })
        assert invalid.status_code == 422, invalid.text

        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            user_id = connection.execute(
                text("INSERT INTO identity_users (issuer,subject) VALUES (:issuer,:subject) RETURNING id"),
                {"issuer": issuer, "subject": manager.subject}).scalar_one()
            connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'manager')"),
                               {"tenant": tenant["tenant_id"], "user": str(user_id)})
            connection.execute(text("INSERT INTO membership_locations (tenant_id,user_id,location_id) VALUES (:tenant,:user,:location)"),
                               {"tenant": tenant["tenant_id"], "user": str(user_id), "location": tenant["location_id"]})
        manager_app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
        manager_app.dependency_overrides[require_identity] = lambda: manager
        manager_client = TestClient(manager_app)
        restricted = manager_client.post(f"{base}/field-definitions", json={
            "key": "restricted_formula", "label": "Restricted", "type": "text",
            "validation": {}, "purpose": "synthetic test", "sensitivity": "restricted",
        })
        assert restricted.status_code == 403, restricted.text
        private = manager_client.post(f"{base}/field-definitions", json={
            "key": "private_formula", "label": "Private", "type": "text",
            "validation": {"max_length": 80}, "purpose": "synthetic test", "sensitivity": "private",
        })
        assert private.status_code == 201, private.text
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_field_creation_serializes_with_schema_update(database_url):
    """A value validates against the definition version it writes under."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/fdef-race")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        definition = client.post(f"{base}/field-definitions", json={
            "key": "bounded_number", "label": "Bounded", "type": "number",
            "validation": {"min": 0, "max": 100}, "purpose": "synthetic race test",
        })
        assert definition.status_code == 201, definition.text
        client_row = client.post(f"{base}/clients", json={"name": "Schema Race"})
        assert client_row.status_code == 201
        client_id = client_row.json()["id"]

        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            connection.execute(text("SELECT id FROM client_field_definitions WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                               {"tenant": tenant["tenant_id"], "id": definition.json()["id"]})
            connection.execute(text("""UPDATE client_field_definitions SET validation=jsonb_build_object('min',0,'max',10)
                                      WHERE tenant_id=:tenant AND id=:id"""),
                               {"tenant": tenant["tenant_id"], "id": definition.json()["id"]})
            with ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(client.post, f"{base}/clients/{client_id}/field-values", json={
                    "definition_id": definition.json()["id"], "entity_id": client_id, "value_number": 42,
                })
                time.sleep(0.25)
                blocked_by_schema_lock = not pending.done()
                transaction.commit()
                response = pending.result(timeout=10)
        assert blocked_by_schema_lock, "field value creation must wait for a concurrent schema update"
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "field_value_above_max"
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/fdef-race")
