"""Tests for verified export, correction and deletion (TASK-014, FR-016)."""

import os
import uuid
import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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
    preference = client.post(f"{base}/clients/{cid}/preferences",
                             json={"preference_type": "colour_formula", "value_json": {"formula": "7.0"}})
    assert preference.status_code == 201, preference.text
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

        request = client.post(f"{base}/privacy-requests", json={
            "client_id": cid, "request_type": "export", "verification": "photo ID at desk",
        })
        resp = client.post(f"{base}/privacy-requests/export", json={"request_id": request.json()["id"]})
        assert resp.status_code == 200, resp.text
        data = resp.json()["data"]
        assert data["client"]["name"] == "Data Subject"
        assert len(data["contacts"]) == 1 and len(data["visit_notes"]) == 1 and len(data["preferences"]) == 1
        assert {"custom_fields", "appointments", "visits", "receipts", "communications",
                "consents", "suppressions", "tags", "attachments"} <= data.keys()
        replay = client.post(f"{base}/privacy-requests/export", json={"request_id": request.json()["id"]})
        assert replay.status_code == 409
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
        earlier_request = client.post(f"{base}/privacy-requests", json={
            "client_id": cid, "request_type": "export", "verification": "checked signed export request"})
        assert earlier_request.status_code == 201
        req = client.post(f"{base}/privacy-requests",
                          json={"client_id": cid, "request_type": "deletion", "verification": "signed form"})
        deletion_key = "d-" + uuid.uuid4().hex
        resp = client.post(f"{base}/privacy-requests/deletion",
                           json={"request_id": req.json()["id"], "idempotency_key": deletion_key})
        assert resp.status_code == 200, resp.text
        assert len(resp.json()["subject_hash"]) == 64  # sha256 hex, not an id
        replay = client.post(f"{base}/privacy-requests/deletion",
                             json={"request_id": req.json()["id"], "idempotency_key": deletion_key})
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        conflict = client.post(f"{base}/privacy-requests/deletion",
                               json={"request_id": req.json()["id"], "idempotency_key": "different-key"})
        assert conflict.status_code == 409

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
        with engine.begin() as connection:
            connection.execute(sql_text("SELECT set_config('app.tenant_id', :t, true)"),
                               {"t": tenant["tenant_id"]})
            request_records = connection.execute(sql_text("""SELECT client_id, verification, subject_hash
                FROM privacy_requests WHERE tenant_id=:t AND id IN (:delete_id,:earlier_id)"""),
                {"t": tenant["tenant_id"], "delete_id": req.json()["id"],
                 "earlier_id": earlier_request.json()["id"]}).mappings().all()
        assert len(request_records) == 2
        assert all(row["client_id"] is None and row["verification"] is None for row in request_records)
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_active_retention_hold_requires_separate_audited_release(database_url):
    """AC-1: a free-text deletion override cannot bypass an active legal hold."""
    issuer = "https://identity.example.invalid/privacy-hold"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        cid = _client_with_data(client, base)
        hold = client.post(f"{base}/retention-holds",
                           json={"client_id": cid, "basis": "open dispute 2026-10"})
        assert hold.status_code == 201
        listed = client.get(f"{base}/retention-holds", params={"client_id": cid})
        assert listed.status_code == 200 and listed.json()["items"][0]["id"] == hold.json()["id"]

        req = client.post(f"{base}/privacy-requests",
                          json={"client_id": cid, "request_type": "deletion", "verification": "signed form"})
        blocked = client.post(f"{base}/privacy-requests/deletion",
                              json={"request_id": req.json()["id"], "idempotency_key": "d-" + uuid.uuid4().hex})
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "retention_hold_active"
        # Nothing was deleted.
        assert client.get(f"{base}/clients/{cid}").status_code == 200

        invalid_release = client.post(f"{base}/retention-holds/{hold.json()['id']}/release", json={"reason": "   "})
        assert invalid_release.status_code == 422

        # A deletion payload cannot bypass a hold by supplying an override reason.
        allowed = client.post(f"{base}/privacy-requests/deletion",
                              json={"request_id": req.json()["id"], "hold_override_reason": "dispute closed 2026-11-01",
                                    "idempotency_key": "d-" + uuid.uuid4().hex})
        assert allowed.status_code == 409
        released = client.post(f"{base}/retention-holds/{hold.json()['id']}/release",
                               json={"reason": "dispute resolved under synthetic test policy"})
        assert released.status_code == 200, released.text
        from sqlalchemy import text as sql_text
        with engine.begin() as connection:
            connection.execute(sql_text("SELECT set_config('app.tenant_id', :t, true)"),
                               {"t": tenant["tenant_id"]})
            release_record = connection.execute(sql_text("""SELECT released_by_issuer,
                released_by_subject, release_reason FROM retention_holds
                WHERE tenant_id=:t AND id=:id"""),
                {"t": tenant["tenant_id"], "id": hold.json()["id"]}).mappings().one()
        assert release_record["released_by_issuer"] == issuer
        assert release_record["released_by_subject"] == owner.subject
        assert release_record["release_reason"] == "dispute resolved under synthetic test policy"
        allowed = client.post(f"{base}/privacy-requests/deletion",
                              json={"request_id": req.json()["id"], "idempotency_key": "d-" + uuid.uuid4().hex})
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
        deletion_key = "d-" + uuid.uuid4().hex
        first = client.post(f"{base}/privacy-requests/deletion",
                            json={"request_id": req["id"], "idempotency_key": deletion_key})
        assert first.json()["replayed"] is False
        second = client.post(f"{base}/privacy-requests/deletion",
                             json={"request_id": req["id"], "idempotency_key": deletion_key})
        assert second.status_code == 200 and second.json()["replayed"] is True
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_concurrent_deletion_replays_create_one_tombstone(database_url):
    issuer = "https://identity.example.invalid/privacy-concurrent"
    engine, owner, tenant = _salon(database_url, issuer)
    first_client, second_client = _client(database_url, owner), _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        cid = _client_with_data(first_client, base)
        req = first_client.post(f"{base}/privacy-requests", json={
            "client_id": cid, "request_type": "deletion", "verification": "signed synthetic request"}).json()
        payload = {"request_id": req["id"], "idempotency_key": "concurrent-" + uuid.uuid4().hex[:24]}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(client.post, f"{base}/privacy-requests/deletion", json=payload)
                       for client in (first_client, second_client)]
            responses = [future.result(timeout=15) for future in futures]
        assert [response.status_code for response in responses] == [200, 200]
        assert sum(not response.json().get("replayed", False) for response in responses) == 1
        from sqlalchemy import text as sql_text
        with engine.begin() as connection:
            connection.execute(sql_text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            count = connection.execute(sql_text("SELECT count(*) FROM deletion_tombstones WHERE tenant_id=:t"),
                                        {"t": tenant["tenant_id"]}).scalar_one()
        assert count == 1
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
        request = client.post(f"{base}/privacy-requests", json={
            "client_id": cid, "request_type": "export", "verification": "verified",
        }).json()
        resp = other_client.post(f"{other_base}/privacy-requests/export", json={"request_id": request["id"]})
        assert resp.status_code == 404
        assert other_client.get(f"{other_base}/privacy-requests").json()["total"] == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], other_issuer)


def test_export_includes_attachment_bytes_and_deletion_retries_object_purge(database_url, monkeypatch, tmp_path):
    """AC-2: an interrupted local-file purge stays queued and the same request can finish it."""
    from modules.communications.consent import record_consent
    from modules.crm import attachments
    from sqlalchemy import text as sql_text

    issuer = "https://identity.example.invalid/privacy-file-purge"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    monkeypatch.setenv("ATTACHMENT_STORE_DIR", str(tmp_path / "private-store"))
    content = b"synthetic privacy export attachment"
    storage_key = hashlib.sha256(content).hexdigest()
    object_path = tmp_path / "private-store" / tenant["tenant_id"] / storage_key
    object_path.parent.mkdir(parents=True)
    object_path.write_bytes(content)
    try:
        cid = _client_with_data(client, base)
        details = client.get(f"{base}/clients/{cid}").json()
        contact_id = details["contacts"][0]["id"]
        with engine.begin() as connection:
            connection.execute(sql_text("SELECT set_config('app.tenant_id', :t, true)"),
                               {"t": tenant["tenant_id"]})
            consent = record_consent(connection, tenant_id=tenant["tenant_id"], client_id=cid,
                contact_id=contact_id, channel="email", purpose="attachments", source="staff",
                wording_version="v1", status="granted")
            connection.execute(sql_text("""INSERT INTO attachments
                (tenant_id,id,client_id,consent_id,idempotency_key,storage_key,purpose,
                 content_type,size_bytes,checksum_sha256,retention_until,uploaded_by_issuer,uploaded_by_subject)
                VALUES (:tenant,:id,:client,:consent,:key,:storage,'visit_photo','image/png',
                 :size,:checksum,now()+interval '30 days',:issuer,:subject)"""), {
                "tenant": tenant["tenant_id"], "id": str(uuid.uuid4()), "client": cid,
                "consent": str(consent["id"]), "key": "privacy-" + uuid.uuid4().hex,
                "storage": storage_key, "size": len(content), "checksum": storage_key,
                "issuer": issuer, "subject": owner.subject})
            assert connection.execute(sql_text("SELECT count(*) FROM attachments WHERE tenant_id=:tenant AND client_id=:client"),
                {"tenant": tenant["tenant_id"], "client": cid}).scalar_one() == 1

        export_request = client.post(f"{base}/privacy-requests", json={
            "client_id": cid, "request_type": "export", "verification": "signed synthetic request"}).json()
        exported = client.post(f"{base}/privacy-requests/export", json={"request_id": export_request["id"]})
        assert exported.status_code == 200, exported.text
        attachment = exported.json()["data"]["attachments"][0]
        assert base64.b64decode(attachment["content_base64"]) == content
        assert "storage_key" not in attachment

        deletion = client.post(f"{base}/privacy-requests", json={
            "client_id": cid, "request_type": "deletion", "verification": "signed synthetic request"}).json()
        original_object_path = attachments._object_path
        attempts = {"count": 0}

        class FailOnce:
            def __init__(self, path): self.path = path
            def __getattr__(self, name): return getattr(self.path, name)
            def unlink(self, missing_ok=False):
                if attempts["count"] == 0:
                    attempts["count"] += 1
                    raise OSError("synthetic one-shot filesystem failure")
                return self.path.unlink(missing_ok=missing_ok)

        monkeypatch.setattr(attachments, "_object_path",
            lambda tenant_id, key: FailOnce(original_object_path(tenant_id, key)))
        payload = {"request_id": deletion["id"], "idempotency_key": "privacy-retry-" + uuid.uuid4().hex[:12]}
        pending = client.post(f"{base}/privacy-requests/deletion", json=payload)
        assert pending.status_code == 503, f"unlink attempts={attempts['count']} body={pending.text}"
        assert pending.json()["detail"]["retryable"] is True and object_path.exists()
        pending_requests = client.get(f"{base}/privacy-requests").json()["items"]
        pending_record = next(row for row in pending_requests if row["id"] == deletion["id"])
        assert pending_record["cleanup_pending"] is True
        assert pending_record["cleanup_idempotency_key"] == payload["idempotency_key"]
        conflict = client.post(f"{base}/privacy-requests/deletion", json={
            "request_id": deletion["id"], "idempotency_key": "different-retry-key"})
        assert conflict.status_code == 409
        replay = client.post(f"{base}/privacy-requests/deletion", json=payload)
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert not object_path.exists()
        with engine.begin() as connection:
            connection.execute(sql_text("SELECT set_config('app.tenant_id', :t, true)"),
                               {"t": tenant["tenant_id"]})
            status = connection.execute(sql_text("""SELECT status FROM privacy_requests
                WHERE tenant_id=:t AND id=:id"""), {"t": tenant["tenant_id"], "id": deletion["id"]}).scalar_one()
            completed = connection.execute(sql_text("""SELECT status FROM privacy_file_purge_jobs
                WHERE tenant_id=:t AND privacy_request_id=:id"""),
                {"t": tenant["tenant_id"], "id": deletion["id"]}).scalar_one()
        assert status == "completed" and completed == "completed"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
