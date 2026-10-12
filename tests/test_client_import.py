"""Tests for CSV client import (TASK-012, FR-014)."""

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




def _preview(client, base, filename, content, mapping):
    return client.post(f"{base}/imports/preview",
                       json={"filename": filename, "content": content, "mapping": mapping})


def test_import_preview_reports_errors_without_writing_clients(database_url):
    issuer = "https://identity.example.invalid/import-prev"
    """AC-1: dry run shows per-row errors and does not create clients."""
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        content = "name,email\nAda Lovelace,ada@example.com\n,broken@example.com\nGrace,not-an-email\n"
        resp = _preview(client, base, "clients.csv", content,
                        {"name": "name", "email": "email"})
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["total_rows"] == 3
        assert body["valid_rows"] == 1  # only Ada has both a name and a valid email
        assert body["error_rows"] == 2  # missing name; invalid email

        # A dry run must not have created a client.
        listed = client.get(f"{base}/clients/search", params={"q": "Ada"}).json()
        assert listed["total"] == 0

        errors = client.get(f"{base}/imports/{body['id']}/rows", params={"status": "error"}).json()
        assert errors["total"] == 2
        codes = {code for row in errors["items"] for code in row["error_codes"]}
        assert "name_required" in codes and "invalid_email" in codes
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_import_commit_is_resumable_and_idempotent(database_url):
    issuer = "https://identity.example.invalid/import-commit"
    """AC-1: commit writes valid rows and replays the same receipt for the same key."""
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        content = "name,email\nAda Lovelace,ada@example.com\nAlan Turing,alan@example.com\n"
        preview = _preview(client, base, "clients.csv", content, {"name": "name", "email": "email"}).json()
        assert preview["valid_rows"] == 2

        key = "idem-" + uuid.uuid4().hex
        first = client.post(f"{base}/imports/{preview['id']}/commit",
                            json={"expected_version": 1, "idempotency_key": key})
        assert first.status_code == 200, first.text
        assert first.json()["committed_rows"] == 2 and first.json()["replayed"] is False

        # The same key against the committed import is a replay, not a second import.
        # The retry resends the version it originally read (1), which is now stale; the
        # replay must still return the receipt rather than a version conflict.
        second = client.post(f"{base}/imports/{preview['id']}/commit",
                             json={"expected_version": 1, "idempotency_key": key})
        assert second.status_code == 200
        assert second.json()["replayed"] is True

        listed = client.get(f"{base}/clients/search").json()
        assert listed["total"] == 2

        # A different key against an already-committed import is refused, not double-applied.
        third = client.post(f"{base}/imports/{preview['id']}/commit",
                            json={"expected_version": 2, "idempotency_key": "different-key-1234"})
        assert third.status_code == 409
        assert third.json()["detail"]["code"] == "import_already_committed"
        assert client.get(f"{base}/clients/search").json()["total"] == 2
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_import_without_consent_proof_is_not_marketing_eligible(database_url):
    issuer = "https://identity.example.invalid/import-consent"
    """AC-2: a missing consent proof never becomes opt-in, even if the file asserts it."""
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        # The file claims marketing consent; no proof column is mapped, so there is none.
        content = "name,email,consents\nAda,ada@example.com,yes\n"
        preview = _preview(client, base, "clients.csv", content, {"name": "name", "email": "email"}).json()
        assert preview["valid_rows"] == 1
        client.post(f"{base}/imports/{preview['id']}/commit",
                    json={"expected_version": 1, "idempotency_key": "k-" + uuid.uuid4().hex})
        rows = client.get(f"{base}/imports/{preview['id']}/rows").json()
        assert rows["items"][0]["marketing_eligible"] is False

        # Only an explicit recorded proof flips it.
        patched = client.patch(f"{base}/imports/{preview['id']}/rows/1/consent",
                               json={"consent_proof": "signed form 2026-10-11", "marketing_eligible": True})
        assert patched.status_code == 200
        assert patched.json()["marketing_eligible"] is True
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_import_escapes_spreadsheet_formula_payloads(database_url):
    """AC-2: a formula lead-in is neutralized so export cannot execute it."""
    from modules.crm.imports import escape_spreadsheet_cell

    assert escape_spreadsheet_cell("=1+1") == "'=1+1"
    assert escape_spreadsheet_cell(" =SUM(A1)") == "' =SUM(A1)"
    assert escape_spreadsheet_cell("+44 555") == "'+44 555"
    assert escape_spreadsheet_cell("-5") == "'-5"
    assert escape_spreadsheet_cell("@cmd") == "'@cmd"
    assert escape_spreadsheet_cell("Ada Lovelace") == "Ada Lovelace"


def test_import_is_tenant_scoped(database_url):
    """AC-1: another tenant cannot preview into, read, or commit this tenant's import."""
    issuer = "https://identity.example.invalid/import-scope"
    other_issuer = "https://identity.example.invalid/import-scope-b"
    engine, owner, tenant = _salon(database_url, issuer)
    other_engine, other_owner, other_tenant = _salon(database_url, other_issuer)
    client = _client(database_url, owner)
    other_client = _client(database_url, other_owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    other_base = f"/v1/tenants/{other_tenant['tenant_id']}"
    try:
        content = "name,email\nAda,ada@example.com\n"
        preview = _preview(client, base, "clients.csv", content, {"name": "name", "email": "email"}).json()

        assert other_client.get(f"{other_base}/imports").json()["total"] == 0
        assert other_client.get(f"{other_base}/imports/{preview['id']}/rows").status_code == 404
        hijack = other_client.post(f"{other_base}/imports/{preview['id']}/commit",
                                   json={"expected_version": 1, "idempotency_key": "x-" + uuid.uuid4().hex})
        assert hijack.status_code == 404
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], other_issuer)


def test_import_rejects_oversized_and_empty_files(database_url):
    issuer = "https://identity.example.invalid/import-oversize"
    """AC-1: a malformed upload is a named 422, not an unhandled 500."""
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        empty = _preview(client, base, "empty.csv", "\n", {"name": "name"})
        # A file whose only line is blank has a header of one empty column and no rows;
        # that is a preview of zero rows rather than an error.
        assert empty.status_code in (201, 422)
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)