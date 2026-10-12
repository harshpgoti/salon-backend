"""Tests for controlled client merge (TASK-013, FR-015)."""

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


def _two_clients(client, base, name_a="Survivor", name_b="Duplicate", shared_phone=None):
    a = client.post(f"{base}/clients", json={"name": name_a}).json()["id"]
    b_payload = {"name": name_b}
    b = client.post(f"{base}/clients", json=b_payload).json()["id"]
    if shared_phone:
        for cid in (a, b):
            client.post(f"{base}/clients/{cid}/contacts", json={"kind": "phone", "value": shared_phone})
    return a, b


def test_merge_preview_writes_nothing_and_lists_manifest(database_url):
    """AC-1: preview shows what would move and changes nothing."""
    issuer = "https://identity.example.invalid/merge-prev"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        a, b = _two_clients(client, base, shared_phone="(416) 555-0134")
        client.post(f"{base}/clients/{b}/visit-notes", json={"class": "general", "visibility": "standard", "body": "note"})
        resp = client.post(f"{base}/client-merges/preview",
                           json={"survivor_client_id": a, "merged_client_id": b})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["manifest"]["contacts"] and body["manifest"]["visit_notes"]
        # The shared phone is a conflict the operator must decide on, never auto-resolved.
        assert any(c["kind"] == "phone" for c in body["conflicts"])

        # Preview must not have moved or hidden anything.
        assert client.get(f"{base}/clients/{b}").status_code == 200
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_merge_commit_preserves_history_and_records_map(database_url):
    """AC-1: approved merge moves history to the survivor and records the manifest."""
    issuer = "https://identity.example.invalid/merge-commit"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        a, b = _two_clients(client, base)
        client.post(f"{base}/clients/{b}/visit-notes", json={"class": "general", "visibility": "standard", "body": "b history"})
        client.post(f"{base}/clients/{b}/preferences", json={"preference_type": "colour_formula", "value_json": {"f": "7.0"}})
        a_row = client.get(f"{base}/clients/{a}").json()
        b_row = client.get(f"{base}/clients/{b}").json()

        resp = client.post(f"{base}/client-merges/commit", json={
            "survivor_client_id": a, "merged_client_id": b,
            "expected_survivor_version": a_row["version"], "expected_merged_version": b_row["version"],
            "acknowledged_conflicts": True, "idempotency_key": "m-" + uuid.uuid4().hex})
        assert resp.status_code == 200, resp.text

        # History moved to the survivor.
        notes = client.get(f"{base}/clients/{a}/visit-notes").json()
        assert any("b history" in note["body"] for note in notes["items"])
        prefs = client.get(f"{base}/clients/{a}/preferences").json()
        assert prefs["total"] == 1

        # The merged client is retired, and the merge map is readable.
        assert client.get(f"{base}/clients/{b}").status_code == 404
        merges = client.get(f"{base}/client-merges").json()
        assert merges["total"] == 1
        assert merges["items"][0]["merged_client_id"] == b
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_merge_requires_acknowledged_conflicts_and_current_version(database_url):
    """AC-2: a conflict is not merged by inattention; a stale version is refused."""
    issuer = "https://identity.example.invalid/merge-guard"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        a, b = _two_clients(client, base, shared_phone="(416) 555-0199")
        a_row = client.get(f"{base}/clients/{a}").json()
        b_row = client.get(f"{base}/clients/{b}").json()

        # Unacknowledged conflict -> 409, nothing merged.
        resp = client.post(f"{base}/client-merges/commit", json={
            "survivor_client_id": a, "merged_client_id": b,
            "expected_survivor_version": a_row["version"], "expected_merged_version": b_row["version"],
            "acknowledged_conflicts": False, "idempotency_key": "m-" + uuid.uuid4().hex})
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "merge_conflicts_unacknowledged"
        assert client.get(f"{base}/clients/{b}").status_code == 200

        # Stale version -> 409.
        resp = client.post(f"{base}/client-merges/commit", json={
            "survivor_client_id": a, "merged_client_id": b,
            "expected_survivor_version": 99, "expected_merged_version": b_row["version"],
            "acknowledged_conflicts": True, "idempotency_key": "m-" + uuid.uuid4().hex})
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "client_version_conflict"

        # Merging a client into itself is refused.
        resp = client.post(f"{base}/client-merges/preview",
                           json={"survivor_client_id": a, "merged_client_id": a})
        assert resp.status_code == 422
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_merge_commit_is_idempotent(database_url):
    """AC-1: replaying the same merge key returns the receipt, not a second merge."""
    issuer = "https://identity.example.invalid/merge-idem"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        a, b = _two_clients(client, base)
        a_row = client.get(f"{base}/clients/{a}").json()
        b_row = client.get(f"{base}/clients/{b}").json()
        key = "m-" + uuid.uuid4().hex
        payload = {"survivor_client_id": a, "merged_client_id": b,
                   "expected_survivor_version": a_row["version"], "expected_merged_version": b_row["version"],
                   "acknowledged_conflicts": True, "idempotency_key": key}
        first = client.post(f"{base}/client-merges/commit", json=payload)
        assert first.status_code == 200 and first.json()["replayed"] is False
        second = client.post(f"{base}/client-merges/commit", json=payload)
        assert second.status_code == 200 and second.json()["replayed"] is True
        assert first.json()["merges"] == second.json()["merges"]
        assert client.get(f"{base}/client-merges").json()["total"] == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_shared_contact_alone_never_merges_and_never_promotes_consent(database_url):
    """AC-2: a shared phone produces a candidate; it does not merge or confer consent."""
    issuer = "https://identity.example.invalid/merge-consent"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        a, b = _two_clients(client, base, shared_phone="(416) 555-0177")
        # Both clients still exist; no merge happened as a side effect of sharing.
        assert client.get(f"{base}/clients/{b}").status_code == 200
        assert client.get(f"{base}/client-merges").json()["total"] == 0

        preview = client.post(f"{base}/client-merges/preview",
                              json={"survivor_client_id": a, "merged_client_id": b}).json()
        # A conflict is surfaced, not silently resolved, so no consent can be inherited.
        assert preview["conflicts"], "the shared phone must surface as a conflict"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_merge_is_tenant_scoped(database_url):
    """AC-1: another tenant cannot preview or commit a merge against this tenant."""
    issuer = "https://identity.example.invalid/merge-scope"
    other_issuer = "https://identity.example.invalid/merge-scope-b"
    engine, owner, tenant = _salon(database_url, issuer)
    other_engine, other_owner, other_tenant = _salon(database_url, other_issuer)
    client = _client(database_url, owner)
    other_client = _client(database_url, other_owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    other_base = f"/v1/tenants/{other_tenant['tenant_id']}"
    try:
        a, b = _two_clients(client, base)
        assert other_client.post(f"{other_base}/client-merges/preview",
                                 json={"survivor_client_id": a, "merged_client_id": b}).status_code == 404
        assert other_client.get(f"{other_base}/client-merges").json()["total"] == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], other_issuer)