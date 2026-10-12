"""Tests for visit notes (TASK-011, FR-013)."""

import os
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


def test_visit_note_crud_and_visibility(database_url):
    """AC-1/AC-2: Visit notes with private visibility require sensitive permission."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/vnote-1")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        # Create client
        resp = client.post(f"{base}/clients", json={"name": "Note Test"})
        assert resp.status_code == 201
        client_id = resp.json()["id"]

        # Create standard note
        resp = client.post(f"{base}/clients/{client_id}/visit-notes", json={
            "class": "general", "visibility": "standard", "body": "Client prefers morning appointments."
        })
        assert resp.status_code == 201
        note_id_1 = resp.json()["id"]

        # Create private note (clinical)
        resp = client.post(f"{base}/clients/{client_id}/visit-notes", json={
            "class": "clinical", "visibility": "private", "body": "Allergic to ammonia-based color."
        })
        assert resp.status_code == 201
        note_id_2 = resp.json()["id"]

        # Create allergy note
        resp = client.post(f"{base}/clients/{client_id}/visit-notes", json={
            "class": "allergy", "visibility": "private", "body": "Contact dermatitis from PPD."
        })
        assert resp.status_code == 201
        note_id_3 = resp.json()["id"]

        # List notes (owner sees all)
        resp = client.get(f"{base}/clients/{client_id}/visit-notes")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 3
        classes = {i["class"] for i in items}
        assert classes == {"general", "clinical", "allergy"}

        # Update note
        resp = client.patch(f"{base}/clients/{client_id}/visit-notes/{note_id_1}",
                            json={"body": "Updated: prefers morning appointments.", "expected_version": 1})
        assert resp.status_code == 200
        assert resp.json()["body"] == "Updated: prefers morning appointments."
        assert resp.json()["version"] == 2

        # Version conflict
        resp = client.patch(f"{base}/clients/{client_id}/visit-notes/{note_id_1}",
                            json={"body": "Conflict", "expected_version": 1})
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "visit_note_version_conflict" or "version" in resp.json()["detail"]["code"].lower()

        # Body validation (no control characters)
        resp = client.post(f"{base}/clients/{client_id}/visit-notes", json={
            "class": "general", "visibility": "standard", "body": "Bad\x00chars"
        })
        assert resp.status_code == 422
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/vnote-1")


def test_visit_note_tenant_isolation(database_url):
    """AC-2: Visit notes are tenant-isolated."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/vnote-2")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        resp = client.post(f"{base}/clients", json={"name": "Note Isolation"})
        client_id = resp.json()["id"]

        resp = client.post(f"{base}/clients/{client_id}/visit-notes", json={
            "class": "general", "visibility": "standard", "body": "Tenant A note"
        })
        assert resp.status_code == 201

        # Foreign tenant
        other_engine, other_owner, other_tenant = _salon(database_url, "https://identity.example.invalid/vnote-2b")
        try:
            other_client = _client(database_url, other_owner)
            other_base = f"/v1/tenants/{other_tenant['tenant_id']}/clients"
            resp = other_client.get(f"{other_base}/{client_id}/visit-notes")
            # Client not found in foreign tenant -> 404
            assert resp.status_code == 404
        finally:
            _teardown(other_engine, [other_tenant["tenant_id"]], "https://identity.example.invalid/vnote-2b")
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/vnote-2")

def test_private_note_excluded_from_page_and_total(database_url):
    """AC-2: a private note is neither listed nor counted for a role without sensitive permission."""
    from modules.identity.main import AuthenticatedIdentity, create_app, require_identity
    from fastapi.testclient import TestClient

    issuer = "https://identity.example.invalid/vpage-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    receptionist = AuthenticatedIdentity(issuer, str(uuid.uuid4()))
    try:
        resp = client.post(f"{base}/clients", json={"name": "Paged Notes"})
        assert resp.status_code == 201
        client_id = resp.json()["id"]
        for body in ("standard one", "standard two"):
            created = client.post(f"{base}/clients/{client_id}/visit-notes",
                                  json={"class": "general", "visibility": "standard", "body": body})
            assert created.status_code == 201
        created = client.post(f"{base}/clients/{client_id}/visit-notes",
                              json={"class": "clinical", "visibility": "private", "body": "private detail"})
        assert created.status_code == 201

        # The owner sees all three, including the private one.
        owner_body = client.get(f"{base}/clients/{client_id}/visit-notes").json()
        assert owner_body["total"] == 3 and len(owner_body["items"]) == 3

        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant["tenant_id"]})
            user_id = connection.execute(text("INSERT INTO identity_users (issuer,subject) VALUES (:issuer,:subject) RETURNING id"),
                                         {"issuer": issuer, "subject": receptionist.subject}).scalar_one()
            connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'receptionist')"),
                               {"tenant": tenant["tenant_id"], "user": str(user_id)})
        receptionist_app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
        receptionist_app.dependency_overrides[require_identity] = lambda: receptionist
        receptionist_client = TestClient(receptionist_app)

        # Hiding the note must also remove it from the count, or the pager advertises rows nobody can open.
        body = receptionist_client.get(f"{base}/clients/{client_id}/visit-notes").json()
        assert body["total"] == 2
        assert all(note["visibility"] == "standard" for note in body["items"])
        assert "private detail" not in str(body)
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
