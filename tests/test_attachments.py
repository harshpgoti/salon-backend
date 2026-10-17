"""Tests for consent-controlled visit photos and attachments (TASK-068, FR-017).

AC-1: an upload needs an ACTIVE client consent whose purpose covers attachments,
each attachment carries its own purpose and retention deadline, and the file lands
in a private content-addressed store rather than anywhere in the repository.
AC-2: unsafe content and cross-tenant recovery paths are refused by name - bad
content type, bad checksum and oversize leave nothing behind, another tenant's
attachment is a 404, a revoked consent closes the file, and a deletion is soft,
audited and idempotent.
"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.consent import record_consent
from modules.crm.attachments import register_attachment_routes
from modules.identity.main import (
    AuthenticatedIdentity,
    _insert_audit,
    _membership_scope,
    _require_permission,
    create_app,
    require_identity,
)
from test_clients import _client, _salon, _teardown

# A synthetic payload: it only has to be bytes, never a real photograph.
PHOTO = b"\x89PNG\r\n\x1a\n" + b"synthetic visit photo bytes" * 8
DOCUMENT = b"%PDF-1.4\n% synthetic visit document\n"
ONE_MB = 1024 * 1024


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL CRM integration")
    return url


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch) -> Path:
    """Point the private store at a temporary directory.

    The suite never writes into the working tree, and the store can be inspected
    directly - which is what makes "nothing was written" an assertion rather than
    a promise.
    """
    root = tmp_path / "attachments"
    monkeypatch.setenv("ATTACHMENT_STORE_DIR", str(root))
    return root


def _attachment_client(database_url: str, identity: AuthenticatedIdentity) -> TestClient:
    """The platform app with the attachment routes wired the way the orchestrator does.

    ``register_attachment_routes`` is deliberately called here rather than from
    identity/main.py, so this file owns exactly the surface it asserts on.
    """
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    register_attachment_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )
    app.dependency_overrides[require_identity] = lambda: identity
    return TestClient(app)


def _consented_client(client: TestClient, base: str, name="Consent Subject"):
    """A client with one verified-style contact, so a consent row can cite it."""
    created = client.post(f"{base}/clients", json={
        "name": name,
        "contacts": [{"kind": "email", "value": "subject@example.com", "is_primary": True}],
    })
    assert created.status_code == 201, created.text
    body = created.json()
    contact = next(item for item in body["contacts"] if item["kind"] == "email")
    return body["id"], contact["id"]


def _consent(engine, tenant_id: str, client_id: str, contact_id: str, *,
             purpose: str = "attachments", status: str = "granted",
             expires_at: datetime | None = None) -> str:
    """Write a real consent history row through the communications consent module."""
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                           {"t": tenant_id})
        row = record_consent(connection, tenant_id=tenant_id, client_id=client_id,
                             contact_id=contact_id, channel="email", purpose=purpose,
                             source="staff", wording_version="v1", status=status,
                             expires_at=expires_at)
    return str(row["id"])


def _upload(attach: TestClient, base: str, client_id: str, consent_id: str, *,
            content: bytes = PHOTO, content_type: str = "image/png",
            checksum: str | None = None, key: str | None = None,
            purpose: str = "visit_photo") -> object:
    return attach.post(f"{base}/clients/{client_id}/attachments", json={
        "consent_id": consent_id,
        "purpose": purpose,
        "content_type": content_type,
        "content_base64": base64.b64encode(content).decode("ascii"),
        "checksum_sha256": checksum or hashlib.sha256(content).hexdigest(),
        "idempotency_key": key or ("att-" + uuid.uuid4().hex),
    })


def _force_retention_past(engine, tenant_id: str, attachment_id: str) -> None:
    """Wind an attachment's retention deadline into the past."""
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                           {"t": tenant_id})
        connection.execute(
            text("""UPDATE attachments SET retention_until = now() - interval '1 day'
                    WHERE tenant_id=:t AND id=:id"""),
            {"t": tenant_id, "id": attachment_id})


def _audit_actions(engine, tenant_id: str, attachment_id: str) -> list[str]:
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                           {"t": tenant_id})
        return connection.execute(
            text("""SELECT action FROM tenant_audit_events
                    WHERE tenant_id=:t AND resource_type='attachment' AND resource_id=:id
                    ORDER BY occurred_at, id"""),
            {"t": tenant_id, "id": attachment_id}).scalars().all()


def _stored_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file())


# AC-1: an upload under an active consent is downloadable, and nothing else is.


def test_consented_upload_downloads_exact_bytes_from_private_store(database_url, store):
    """AC-1: a consented upload stores the object by hash and serves the exact bytes."""
    issuer = "https://identity.example.invalid/att-1"
    engine, owner, tenant = _salon(database_url, issuer)
    setup = _client(database_url, owner)
    attach = _attachment_client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        client_id, contact_id = _consented_client(setup, base)
        consent_id = _consent(engine, tenant["tenant_id"], client_id, contact_id)

        key = "att-" + uuid.uuid4().hex
        uploaded = _upload(attach, base, client_id, consent_id, key=key)
        assert uploaded.status_code == 201, uploaded.text
        attachment_id = uploaded.json()["attachment_id"]

        # The object is named by its sha256 and lives only in the private store.
        digest = hashlib.sha256(PHOTO).hexdigest()
        assert (store / tenant["tenant_id"] / digest).read_bytes() == PHOTO
        assert not list((store / tenant["tenant_id"]).glob(".*partial"))

        downloaded = attach.get(f"{base}/attachments/{attachment_id}")
        assert downloaded.status_code == 200, downloaded.text
        assert downloaded.content == PHOTO
        assert downloaded.headers["content-type"] == "image/png"

        # The listing is metadata only: never the bytes.
        listing = attach.get(f"{base}/clients/{client_id}/attachments")
        assert listing.status_code == 200, listing.text
        assert listing.json()["total"] == 1
        item = listing.json()["items"][0]
        assert item["purpose"] == "visit_photo"
        assert item["content_type"] == "image/png"
        assert item["size_bytes"] == len(PHOTO)
        assert item["state"] == "active"
        assert item["retention_until"] > datetime.now(timezone.utc).isoformat()
        assert "content_base64" not in listing.text

        # The same key replays the same attachment instead of storing a second copy.
        replay = _upload(attach, base, client_id, consent_id,
                         key=key)
        assert replay.status_code == 200, replay.text
        assert replay.json()["attachment_id"] == attachment_id
        assert replay.json()["replayed"] is True
        assert attach.get(f"{base}/clients/{client_id}/attachments").json()["total"] == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


# AC-1: no consent, no upload; a revoked consent refuses both upload and download.


def test_upload_requires_active_consent_covering_attachments(database_url, store):
    """AC-1: consent absence, wrong purpose, revocation and expiry are named refusals."""
    issuer = "https://identity.example.invalid/att-2"
    engine, owner, tenant = _salon(database_url, issuer)
    setup = _client(database_url, owner)
    attach = _attachment_client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        client_id, contact_id = _consented_client(setup, base)
        tenant_id = tenant["tenant_id"]

        # No consent at all: an unknown id is not a consent.
        missing = _upload(attach, base, client_id, str(uuid.uuid4()))
        assert missing.status_code == 404
        assert missing.json()["detail"]["code"] == "consent_not_found"

        # A grant for another purpose does not cover attachments.
        marketing = _consent(engine, tenant_id, client_id, contact_id, purpose="marketing")
        wrong_purpose = _upload(attach, base, client_id, marketing)
        assert wrong_purpose.status_code == 409
        assert wrong_purpose.json()["detail"]["code"] == "consent_purpose_mismatch"

        # A grant that already expired is not an active grant.
        stale = _consent(engine, tenant_id, client_id, contact_id,
                         expires_at=datetime.now(timezone.utc) - timedelta(days=1))
        expired = _upload(attach, base, client_id, stale)
        assert expired.status_code == 409
        assert expired.json()["detail"]["code"] == "consent_expired"

        # A revoked grant: history is append-only, so the revocation is its own row.
        granted = _consent(engine, tenant_id, client_id, contact_id)
        _consent(engine, tenant_id, client_id, contact_id, status="revoked")
        revoked = _upload(attach, base, client_id, granted)
        assert revoked.status_code == 409
        assert revoked.json()["detail"]["code"] == "consent_revoked"

        # A live grant still works, and revoking it afterwards closes the file.
        live = _consent(engine, tenant_id, client_id, contact_id)
        uploaded = _upload(attach, base, client_id, live)
        assert uploaded.status_code == 201, uploaded.text
        attachment_id = uploaded.json()["attachment_id"]
        assert attach.get(f"{base}/attachments/{attachment_id}").status_code == 200

        _consent(engine, tenant_id, client_id, contact_id, status="revoked")
        refused = attach.get(f"{base}/attachments/{attachment_id}")
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == "consent_revoked"

        # Nothing was refused into the store, and the row survives for audit.
        assert _stored_files(store) == [store / tenant_id / hashlib.sha256(PHOTO).hexdigest()]
        assert attach.get(f"{base}/clients/{client_id}/attachments").json()["total"] == 1
        assert _audit_actions(engine, tenant_id, attachment_id) == ["attachment.uploaded", "attachment.downloaded"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_retention_deadline_refuses_download(database_url, store):
    """AC-1: an attachment outlives its retention deadline only as an audit row."""
    issuer = "https://identity.example.invalid/att-3"
    engine, owner, tenant = _salon(database_url, issuer)
    setup = _client(database_url, owner)
    attach = _attachment_client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        client_id, contact_id = _consented_client(setup, base)
        consent_id = _consent(engine, tenant["tenant_id"], client_id, contact_id)
        attachment_id = _upload(attach, base, client_id, consent_id).json()["attachment_id"]

        _force_retention_past(engine, tenant["tenant_id"], attachment_id)
        expired = attach.get(f"{base}/attachments/{attachment_id}")
        assert expired.status_code == 409
        assert expired.json()["detail"]["code"] == "retention_expired"

        # The row stays: a retention sweep needs to see what it still holds.
        listing = attach.get(f"{base}/clients/{client_id}/attachments").json()
        assert listing["total"] == 1 and listing["items"][0]["state"] == "active"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


# AC-2: unsafe content is refused before anything is written.


def test_content_policy_refuses_type_size_and_checksum(database_url, store):
    """AC-2: disallowed type, oversize and checksum mismatch leave no file behind."""
    issuer = "https://identity.example.invalid/att-4"
    engine, owner, tenant = _salon(database_url, issuer)
    setup = _client(database_url, owner)
    attach = _attachment_client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        client_id, contact_id = _consented_client(setup, base)
        consent_id = _consent(engine, tenant["tenant_id"], client_id, contact_id)

        disallowed = _upload(attach, base, client_id, consent_id,
                             content_type="application/x-msdownload")
        assert disallowed.status_code == 415
        assert disallowed.json()["detail"]["code"] == "content_type_not_allowed"

        mismatch = _upload(attach, base, client_id, consent_id, checksum="0" * 64)
        assert mismatch.status_code == 422
        assert mismatch.json()["detail"]["code"] == "checksum_mismatch"

        oversize = _upload(attach, base, client_id, consent_id,
                           content=b"\x00" * (5 * ONE_MB + 1))
        assert oversize.status_code == 413
        assert oversize.json()["detail"]["code"] == "attachment_too_large"

        # A body that is not base64 at all is refused before it is decoded.
        broken = attach.post(f"{base}/clients/{client_id}/attachments", json={
            "consent_id": consent_id, "purpose": "visit_photo",
            "content_type": "image/png", "content_base64": "!!!not base64!!!",
            "checksum_sha256": hashlib.sha256(PHOTO).hexdigest(),
            "idempotency_key": "att-" + uuid.uuid4().hex})
        assert broken.status_code == 422
        assert broken.json()["detail"]["code"] == "content_not_base64"

        # Nothing was written to the store, and no row was created for any refusal.
        assert _stored_files(store) == []
        assert attach.get(f"{base}/clients/{client_id}/attachments").json()["total"] == 0

        # The same consent can still store a file that does pass the policy.
        accepted = _upload(attach, base, client_id, consent_id, content=DOCUMENT,
                           content_type="application/pdf")
        assert accepted.status_code == 201, accepted.text
        assert _stored_files(store) == [
            store / tenant["tenant_id"] / hashlib.sha256(DOCUMENT).hexdigest()]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_declared_image_type_must_match_file_signature(database_url, store):
    """A declared image MIME type cannot make arbitrary PDF bytes an image upload."""
    issuer = "https://identity.example.invalid/att-signature"
    engine, owner, tenant = _salon(database_url, issuer)
    setup = _client(database_url, owner)
    attach = _attachment_client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        client_id, contact_id = _consented_client(setup, base)
        consent_id = _consent(engine, tenant["tenant_id"], client_id, contact_id)

        spoofed = _upload(attach, base, client_id, consent_id,
                          content=DOCUMENT, content_type="image/png")
        assert spoofed.status_code == 415, spoofed.text
        assert spoofed.json()["detail"]["code"] == "content_signature_mismatch"
        assert _stored_files(store) == []
        assert attach.get(f"{base}/clients/{client_id}/attachments").json()["total"] == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


# AC-2: another tenant's attachment is indistinguishable from a row that never was.


def test_another_tenants_attachment_is_not_found(database_url, store):
    """AC-2: cross-tenant download and deletion are 404s, never a 200 or a 403."""
    issuer_a = "https://identity.example.invalid/att-5a"
    issuer_b = "https://identity.example.invalid/att-5b"
    engine_a, owner_a, tenant_a = _salon(database_url, issuer_a)
    engine_b, owner_b, tenant_b = _salon(database_url, issuer_b)
    setup_a = _client(database_url, owner_a)
    setup_b = _client(database_url, owner_b)
    attach_a = _attachment_client(database_url, owner_a)
    attach_b = _attachment_client(database_url, owner_b)
    base_a = f"/v1/tenants/{tenant_a['tenant_id']}"
    base_b = f"/v1/tenants/{tenant_b['tenant_id']}"
    try:
        client_a, contact_a = _consented_client(setup_a, base_a, "Tenant A Subject")
        consent_a = _consent(engine_a, tenant_a["tenant_id"], client_a, contact_a)
        attachment_id = _upload(attach_a, base_a, client_a, consent_a).json()["attachment_id"]

        client_b, contact_b = _consented_client(setup_b, base_b, "Tenant B Subject")
        consent_b = _consent(engine_b, tenant_b["tenant_id"], client_b, contact_b)

        # Download from the other tenant is a miss, and so is deletion.
        seen = attach_b.get(f"{base_b}/attachments/{attachment_id}")
        assert seen.status_code == 404
        assert seen.json()["detail"]["code"] == "attachment_not_found"
        deleted = attach_b.request("DELETE", f"{base_b}/attachments/{attachment_id}", json={
            "idempotency_key": "att-" + uuid.uuid4().hex, "reason": "cross tenant attempt"})
        assert deleted.status_code == 404
        assert deleted.json()["detail"]["code"] == "attachment_not_found"

        # The owning tenant still holds it, untouched.
        assert attach_a.get(f"{base_a}/attachments/{attachment_id}").status_code == 200
        assert attach_b.get(f"{base_b}/clients/{client_b}/attachments").json()["total"] == 0
    finally:
        _teardown(engine_a, [tenant_a["tenant_id"]], issuer_a)
        _teardown(engine_b, [tenant_b["tenant_id"]], issuer_b)


# AC-2: deletion is soft, audited and idempotent.


def test_deletion_is_soft_audited_and_idempotent(database_url, store):
    """AC-2: a deleted attachment is a 410 on download, replayed on retry, one audit row."""
    issuer = "https://identity.example.invalid/att-6"
    engine, owner, tenant = _salon(database_url, issuer)
    setup = _client(database_url, owner)
    attach = _attachment_client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        client_id, contact_id = _consented_client(setup, base)
        consent_id = _consent(engine, tenant["tenant_id"], client_id, contact_id)
        attachment_id = _upload(attach, base, client_id, consent_id).json()["attachment_id"]
        stored = store / tenant["tenant_id"] / hashlib.sha256(PHOTO).hexdigest()
        assert stored.exists()

        key = "att-delete-" + uuid.uuid4().hex
        first = attach.request("DELETE", f"{base}/attachments/{attachment_id}",
                              json={"idempotency_key": key, "reason": "client asked us to remove it"})
        assert first.status_code == 200, first.text
        assert first.json()["state"] == "deleted"
        assert first.json()["replayed"] is False

        # The bytes are gone from the store; the row is not.
        assert not stored.exists()
        listing = attach.get(f"{base}/clients/{client_id}/attachments").json()
        assert listing["total"] == 1
        assert listing["items"][0]["state"] == "deleted"
        assert listing["items"][0]["deleted_at"] is not None

        # Download is refused with its own named code, not a 500 or a 404.
        gone = attach.get(f"{base}/attachments/{attachment_id}")
        assert gone.status_code == 410
        assert gone.json()["detail"]["code"] == "attachment_deleted"

        # Replays return the same state and write nothing further.
        replay = attach.request("DELETE", f"{base}/attachments/{attachment_id}",
                               json={"idempotency_key": key, "reason": "client asked us to remove it"})
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True
        assert replay.json()["state"] == "deleted"
        other_key = attach.request("DELETE", f"{base}/attachments/{attachment_id}",
                                  json={"idempotency_key": "att-" + uuid.uuid4().hex,
                                        "reason": "retry with a fresh key"})
        assert other_key.status_code == 200 and other_key.json()["replayed"] is True

        # Exactly one audit row for the upload and one for the deletion.
        assert _audit_actions(engine, tenant["tenant_id"], attachment_id) == [
            "attachment.uploaded", "attachment.deleted"]
        # The store is empty again: no object outlives the row that held it.
        assert _stored_files(store) == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
