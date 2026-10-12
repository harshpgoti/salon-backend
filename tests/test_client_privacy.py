"""Tests for verified export, correction and deletion (TASK-014, FR-016)."""

import os
import uuid

import pytest
from test_clients import _salon, _client, _teardown


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL CRM integration")
    return url


def _client_with_data(client, base, name="Data Subject"):
    cid = client.post(f"{base}/clients", json={"name": name}).json()["id"]
    client.post(f"{base}/clients/{cid}/contacts", json={"kind": "email", "value": "subject@example.com"})
    client.post(f"{base}/clients/{cid}/visit-notes",
                json={"class": "general", "visibility": "standard", "body": "a visit note"})
    client.post(f"{base}/clients/{cid}/preferences",
                json={"preference_type": "colour_formula", "value_json": {"f": "7.0"}})
    return cid


def test_verified_export_returns_scoped_canonical_records(database_url):
    """AC-1: a verified export returns the subject's own records only."""
    issuer = "https://identity.example.invalid/privacy-export"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        cid = _client_with_data(client, base)
        other = client.post(f"{base}/clients", json={"name": "Unrelated"}).json()["id"]

        resp = client.post(f"{base}/privacy-requests/export",
                           json={"client_id": cid, "request_type": "export", "verification": "photo ID at desk"})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["client"]["name"] == "Data Subject"
        assert len(data["contacts"]) == 1 and len(data["visit_notes"]) == 1 and len(data["preferences"]) == 1
        # The unrelated client's id must never appear in the exported payload.
        assert other not in str(data)
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_correction_updates_canonical_field_and_closes_request(database_url):
    """AC-1: a verified correction changes the record and is audited."""
    issuer = "https://identity.example.invalid/privacy-correct"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        cid = _client_with_data(client, base, name="Typo Nmae")
        req = client.post(f"{base}/privacy-requests",
                          json={"client_id": cid, "request_type": "correction", "verification": "written request"})
        assert req.status_code == 201
        resp = client.post(f"{base}/privacy-requests/correction",
                           json={"request_id": req.json()["id"], "field": "name", "value": "Typo Name"})
        assert resp.status_code == 200, resp.text
        assert client.get(f"{base}/clients/{cid}").json()["name"] == "Typo Name"

        # A field outside the correctable set is refused, not written.
        req2 = client.post(f"{base}/privacy-requests",
                           json={"client_id": cid, "request_type": "correction", "verification": "written request"})
        bad = client.post(f"{base}/privacy-requests/correction",
                          json={"request_id": req2.json()["id"], "field": "lifecycle", "value": "archived"})
        assert bad.status_code == 422
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_deletion_purges_records_and_writes_non_identifying_tombstone(database_url):
    """AC-2: deletion removes dependent rows and stores a hash tombstone, not the id."""
    issuer = "https://identity.example.invalid/privacy-delete"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        cid = _client_with_data(client, base)
        req = client.post(f"{base}/privacy-requests",
                          json={"client_id": cid, "request_type": "deletion", "verification": "signed form"})
        resp = client.post(f"{base}/privacy-requests/deletion",
                           json={"request_id": req.json()["id"], "idempotency_key": "d-" + uuid.uuid4().hex})
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["subject_hash"]) == 64  # sha256 hex, not an id

        # The client is retired and its dependent rows are gone.
        assert client.get(f"{base}/clients/{cid}").status_code == 404
        notes = client.get(f"{base}/clients/{cid}/visit-notes")
        assert notes.status_code == 404

        from sqlalchemy import text as sql_text
        with engine.begin() as connection:
            connection.execute(sql_text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            remaining = connection.execute(
                sql_text("SELECT count(*) FROM client_contacts WHERE tenant_id=:t AND client_id=:c"),
                {"t": tenant["tenant_id"], "c": cid}).scalar_one()
            tombstones = connection.execute(
                sql_text("SELECT subject_hash FROM deletion_tombstones WHERE tenant_id=:t"),
                {"t": tenant["tenant_id"]}).scalars().all()
            # The tombstone must not contain the raw client id.
            for tomb in tombstones:
                assert cid.replace("-", "") not in tomb
        assert remaining == 0
        assert len(tombstones) == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_active_retention_hold_blocks_deletion(database_url):
    """AC-1: a legal hold prevents deletion until explicitly overridden with a reason."""
    issuer = "https://identity.example.invalid/privacy-hold"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        cid = _client_with_data(client, base)
        hold = client.post(f"{base}/retention-holds",
                           json={"client_id": cid, "basis": "open dispute 2026-10"})
        assert hold.status_code == 201

        req = client.post(f"{base}/privacy-requests",
                          json={"client_id": cid, "request_type": "deletion", "verification": "signed form"})
        blocked = client.post(f"{base}/privacy-requests/deletion",
                              json={"request_id": req.json()["id"], "idempotency_key": "d-" + uuid.uuid4().hex})
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "retention_hold_active"
        # Nothing was deleted.
        assert client.get(f"{base}/clients/{cid}").status_code == 200

        # With an explicit override reason the deletion proceeds.
        allowed = client.post(f"{base}/privacy-requests/deletion",
                              json={"request_id": req.json()["id"], "hold_override_reason": "dispute closed 2026-11-01",
                                    "idempotency_key": "d-" + uuid.uuid4().hex})
        assert allowed.status_code == 200
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_deletion_is_idempotent_and_requires_verification(database_url):
    """AC-2: a replayed deletion returns completed; an unverified request is refused."""
    issuer = "https://identity.example.invalid/privacy-idem"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        cid = _client_with_data(client, base)
        # Empty verification is rejected by the schema.
        bad = client.post(f"{base}/privacy-requests",
                          json={"client_id": cid, "request_type": "deletion", "verification": ""})
        assert bad.status_code == 422

        req = client.post(f"{base}/privacy-requests",
                          json={"client_id": cid, "request_type": "deletion", "verification": "signed form"}).json()
        first = client.post(f"{base}/privacy-requests/deletion",
                            json={"request_id": req["id"], "idempotency_key": "d-" + uuid.uuid4().hex})
        assert first.json()["replayed"] is False
        second = client.post(f"{base}/privacy-requests/deletion",
                             json={"request_id": req["id"], "idempotency_key": "d-" + uuid.uuid4().hex})
        assert second.status_code == 200 and second.json()["replayed"] is True
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_privacy_workflow_is_tenant_scoped(database_url):
    """AC-1: another tenant cannot export or delete this tenant's subject."""
    issuer = "https://identity.example.invalid/privacy-scope"
    other_issuer = "https://identity.example.invalid/privacy-scope-b"
    engine, owner, tenant = _salon(database_url, issuer)
    other_engine, other_owner, other_tenant = _salon(database_url, other_issuer)
    client = _client(database_url, owner)
    other_client = _client(database_url, other_owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    other_base = f"/v1/tenants/{other_tenant['tenant_id']}"
    try:
        cid = _client_with_data(client, base)
        resp = other_client.post(f"{other_base}/privacy-requests/export",
                                 json={"client_id": cid, "request_type": "export", "verification": "id"})
        assert resp.status_code == 404
        assert other_client.get(f"{other_base}/privacy-requests").json()["total"] == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], other_issuer)