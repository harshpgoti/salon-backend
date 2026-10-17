"""Consent-controlled visit photos and attachments (FR-017, TASK-068).

This module was deferred behind gate G-ATTACHMENT-SCOPE. The product owner has now
explicitly approved building it, with a **synthetic local content-addressed file
store (no cloud storage)**: the bytes live in ``backend/.data/attachments`` (git
ignored), named by their sha256, and are reachable only through the row family
created by ``db/migrations/068_attachments.sql``. No file is ever served by path,
and no file ever lives in the repository.

Four properties are enforced here rather than promised in the UI:

- **Purpose, consent and retention travel with every upload.** An upload requires
  an ACTIVE client consent whose purpose covers attachments; the stored row then
  carries its own ``purpose`` and ``retention_until``, because the consent it cites
  is append-only history and cannot be re-read as a policy later.
- **The bytes are policy-checked before they exist.** Declared content type,
  basic allowlisted file signature, size and checksum are validated in memory;
  a refused upload writes nothing. Signature checks are not malware scanning or
  proof of a well-formed/safe file; production activation remains gated.
- **Consent revocation closes the file, not the record.** A revoked consent makes
  an attachment undownloadable (``409 consent_revoked``) while the row stays for
  audit. Retention expiry and soft deletion close it the same way, each with its
  own named code.
- **Cross-tenant reads are indistinguishable from misses.** Another tenant's
  attachment is ``404 attachment_not_found``, exactly like a row that never
  existed (FR-018, and the same choice holds.py makes for tokens).

Two deliberate asymmetries, both recorded so a reader does not have to re-derive
them:

- *A consent that lapses after upload does not retroactively delete the record,
  but a revocation always does.* Expiry is a forward-looking gate on sending; an
  object already lawfully stored under a grant that later lapsed stays until its
  own retention deadline, while revocation is a withdrawal of the grant itself.
- *Soft deletion leaves the object in place, and a reference check decides
  whether to unlink it.* Because the store is content-addressed, one object may
  back several rows; unlinking on the first delete would silently break another
  download. The delete endpoint therefore purges the object only when no active row
  in the tenant still cites that hash, and does so after commit so a rollback can
  never delete bytes a surviving row still needs.

Permissions are the operational client grants that actually exist in
``ROLE_PERMISSIONS`` (there is no ``clients.manage`` grant): uploads and deletions
require ``clients.operational.write``, downloads and listings require
``clients.operational.read``. Both are held by owner and manager.

Dependencies are injected for the same reason the sibling CRM modules inject them:
this module owns its domain and never imports the identity app.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import uuid
from collections.abc import Callable
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

# The closed content allowlist (the same three types the DB CHECK enforces). A new
# type is a new storage review, not a configuration change.
ALLOWED_CONTENT_TYPES = ("image/jpeg", "image/png", "application/pdf")
# Minimal signatures confirm that the body at least matches its declared format.
# They are not full parsers and do not establish that the file is safe.
CONTENT_SIGNATURES = {
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "application/pdf": (b"%PDF-",),
}
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
# base64 is 4 characters per 3 bytes, so this bound is the encoded form of the
# size limit. It is checked BEFORE decoding, so an absurd payload is refused with
# the named code instead of being expanded into memory.
MAX_BASE64_CHARS = 7_000_000
# The consent purpose that authorises storing a file for a client. Marketing and
# booking grants do not cover photographs of a person.
CONSENT_PURPOSE = "attachments"
ATTACHMENT_PURPOSES = ("visit_photo", "visit_document", "consent_document")
# One year by default: long enough to outlive a rebooking cycle, short enough that
# a photo of a client is not kept indefinitely by accident.
DEFAULT_RETENTION_DAYS = 365
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650
# The private store. Outside every tracked source path; overridable so a test never
# writes into the working tree.
STORE_ENV_VAR = "ATTACHMENT_STORE_DIR"
DEFAULT_STORE_DIR = Path(__file__).resolve().parents[3] / ".data" / "attachments"


def _store_root() -> Path:
    """The private store root. Resolved per call so the env override is honoured."""
    override = os.getenv(STORE_ENV_VAR)
    root = Path(override).expanduser() if override else DEFAULT_STORE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _tenant_dir(tenant_id: uuid.UUID | str) -> Path:
    """One directory per tenant inside the store.

    The object name is the content hash, so it is identical for identical bytes in
    any tenant. A tenant segment is not a security boundary (RLS is), but it keeps
    one salon's objects out of another's directory tree and makes purging a single
    tenant's store possible without consulting the database.
    """
    path = _store_root() / str(tenant_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _object_path(tenant_id: uuid.UUID | str, storage_key: str) -> Path:
    return _tenant_dir(tenant_id) / storage_key


def _write_object(path: Path, content: bytes) -> None:
    """Store the bytes under their hash, atomically and only once.

    The name is the digest, so an object that already exists is byte-for-byte the
    same content and must not be rewritten. A partial write is made invisible by
    writing a temporary sibling and renaming it into place, so a download can never
    read half a file.
    """
    if path.exists():
        return
    partial = path.with_name(f".{path.name}.{uuid.uuid4().hex}.partial")
    partial.write_bytes(content)
    os.replace(partial, path)


def _decoded_content(payload: "AttachmentIn") -> bytes:
    """Apply the content policy in memory and return the accepted bytes.

    Every refusal here happens before the caller has opened a transaction, let
    alone written a file: a disallowed type, an oversized payload or a digest that
    does not match the bytes is refused without leaving anything behind.
    """
    if payload.content_type not in ALLOWED_CONTENT_TYPES:
        raise HTTPException(status_code=415, detail={
            "code": "content_type_not_allowed", "allowed": list(ALLOWED_CONTENT_TYPES)})
    if len(payload.content_base64) > MAX_BASE64_CHARS:
        raise HTTPException(status_code=413, detail={
            "code": "attachment_too_large", "max_bytes": MAX_ATTACHMENT_BYTES})
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"code": "content_not_base64"}) from exc
    if not content:
        raise HTTPException(status_code=422, detail={"code": "attachment_empty"})
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise HTTPException(status_code=413, detail={
            "code": "attachment_too_large", "max_bytes": MAX_ATTACHMENT_BYTES,
            "size_bytes": len(content)})
    if not any(content.startswith(signature)
               for signature in CONTENT_SIGNATURES[payload.content_type]):
        raise HTTPException(status_code=415, detail={
            "code": "content_signature_mismatch", "content_type": payload.content_type})
    digest = hashlib.sha256(content).hexdigest()
    if digest != payload.checksum_sha256.lower():
        # A mismatch means the caller and the store disagree about what the object
        # is. Storing anyway would make the stored name a lie.
        raise HTTPException(status_code=422, detail={
            "code": "checksum_mismatch", "declared": payload.checksum_sha256,
            "computed": digest})
    return content


class AttachmentIn(BaseModel):
    consent_id: uuid.UUID
    purpose: str = Field(pattern="^(" + "|".join(ATTACHMENT_PURPOSES) + ")$")
    appointment_id: uuid.UUID | None = None
    content_type: str = Field(max_length=100)
    content_base64: str = Field(min_length=1, max_length=32_000_000)
    checksum_sha256: str = Field(pattern="^[0-9a-fA-F]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=64)
    retention_days: int | None = Field(default=None, ge=MIN_RETENTION_DAYS,
                                       le=MAX_RETENTION_DAYS)


class AttachmentDeleteIn(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=64)
    reason: str = Field(min_length=1, max_length=240)


def register_attachment_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    def client_or_404(connection, tenant_id: uuid.UUID, client_id: uuid.UUID):
        row = connection.execute(
            text("""SELECT id::text AS id FROM clients
                    WHERE tenant_id=:tenant AND id=:id AND deleted_at IS NULL"""),
            {"tenant": str(tenant_id), "id": str(client_id)}).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "client_not_found"})
        return row

    def attachment_or_404(connection, tenant_id: uuid.UUID, attachment_id: uuid.UUID):
        """Tenant-scoped lookup: another tenant's row is indistinguishable from a miss."""
        row = connection.execute(
            text("""SELECT id::text AS id, client_id::text AS client_id,
                           consent_id::text AS consent_id, appointment_id::text AS appointment_id,
                           purpose, content_type, size_bytes, checksum_sha256, storage_key,
                           retention_until, state, deleted_at, created_at,
                           now() AS db_now
                    FROM attachments WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": str(tenant_id), "id": str(attachment_id)}).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "attachment_not_found"})
        return row

    def latest_consent(connection, tenant_id: uuid.UUID, client_id: uuid.UUID, purpose: str):
        """The consent history row that currently decides this client and purpose.

        History is append-only (communications/consent.py), so the latest row wins:
        a revocation recorded after a grant always beats that grant.
        """
        return connection.execute(
            text("""SELECT id::text AS id, status, revoked_at, expires_at
                    FROM consents
                    WHERE tenant_id=:tenant AND client_id=:client AND purpose=:purpose
                    ORDER BY granted_at DESC, ctid DESC LIMIT 1"""),
            {"tenant": str(tenant_id), "client": str(client_id),
             "purpose": purpose}).mappings().one_or_none()

    def consent_gate(connection, tenant_id: uuid.UUID, client_id: uuid.UUID,
                     consent_id: uuid.UUID) -> None:
        """Refuse the upload unless a live consent for this client covers attachments."""
        cited = connection.execute(
            text("""SELECT id::text AS id, client_id::text AS client_id, purpose,
                           status, revoked_at, expires_at
                    FROM consents WHERE tenant_id=:tenant AND id=:consent"""),
            {"tenant": str(tenant_id), "consent": str(consent_id)}).mappings().one_or_none()
        # A consent belonging to another client is not this client's consent.
        if cited is None or cited["client_id"] != str(client_id):
            raise HTTPException(status_code=404, detail={"code": "consent_not_found"})
        if cited["purpose"] != CONSENT_PURPOSE:
            raise HTTPException(status_code=409, detail={
                "code": "consent_purpose_mismatch", "consent_purpose": cited["purpose"],
                "required_purpose": CONSENT_PURPOSE})
        if cited["status"] == "revoked" or cited["revoked_at"] is not None:
            raise HTTPException(status_code=409, detail={"code": "consent_revoked",
                                                         "consent_id": cited["id"]})
        current = latest_consent(connection, tenant_id, client_id, CONSENT_PURPOSE)
        if current is None or current["status"] != "granted" or current["revoked_at"] is not None:
            # A later revocation supersedes the row that was cited.
            raise HTTPException(status_code=409, detail={
                "code": "consent_revoked",
                "consent_id": current["id"] if current else cited["id"]})
        db_now = connection.execute(text("SELECT now() AS now")).mappings().one()["now"]
        # A grant with an expiry in the past is not an active grant. Both the cited
        # row and the row that currently decides the purpose are checked.
        expiries = [row["expires_at"] for row in (cited, current)
                    if row["expires_at"] is not None]
        if expiries and min(expiries) <= db_now:
            raise HTTPException(status_code=409, detail={
                "code": "consent_expired", "expires_at": min(expiries).isoformat()})

    def consent_live_for_download(connection, tenant_id: uuid.UUID, row) -> None:
        """Re-check the grant at download time, not at upload time.

        Revocation withdraws the grant, so the file it authorised becomes
        undownloadable; the row stays because it is the audit trail. A consent that
        merely lapsed is not a withdrawal and does not close a record that was
        stored while the grant was live.
        """
        cited = connection.execute(
            text("""SELECT id::text AS id, status, revoked_at FROM consents
                    WHERE tenant_id=:tenant AND id=:consent"""),
            {"tenant": str(tenant_id), "consent": row["consent_id"]}).mappings().one_or_none()
        if cited is None or cited["status"] != "granted" or cited["revoked_at"] is not None:
            raise HTTPException(status_code=409, detail={"code": "consent_revoked",
                                                         "consent_id": row["consent_id"]})
        current = latest_consent(connection, tenant_id, uuid.UUID(row["client_id"]),
                                 CONSENT_PURPOSE)
        if current is None or current["status"] != "granted" or current["revoked_at"] is not None:
            raise HTTPException(status_code=409, detail={"code": "consent_revoked",
                                                         "consent_id": row["consent_id"]})

    @app.post("/v1/tenants/{tenant_id}/clients/{client_id}/attachments",
              tags=["crm"], status_code=201)
    def create_attachment(tenant_id: uuid.UUID, client_id: uuid.UUID, payload: AttachmentIn,
                          request: Request, identity=Depends(require_identity)) -> Response:
        engine = app.state.engine_factory()
        attachment_id = uuid.uuid4()
        # The whole content policy runs before any transaction is opened, so a
        # refused payload cannot leave a row, a lock or a file behind.
        content = _decoded_content(payload)
        digest = hashlib.sha256(content).hexdigest()
        retention_days = payload.retention_days or DEFAULT_RETENTION_DAYS
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                client_or_404(connection, tenant_id, client_id)
                consent_gate(connection, tenant_id, client_id, payload.consent_id)
                if payload.appointment_id is not None:
                    owned = connection.execute(
                        text("""SELECT 1 FROM appointments
                                WHERE tenant_id=:tenant AND id=:appointment AND client_id=:client"""),
                        {"tenant": str(tenant_id), "appointment": str(payload.appointment_id),
                         "client": str(client_id)}).scalar_one_or_none()
                    if owned is None:
                        raise HTTPException(status_code=404,
                                            detail={"code": "appointment_not_found"})
                # Serialize concurrent creates on one key: the advisory lock is taken
                # BEFORE the replay SELECT, so racers queue here instead of racing
                # past a missed SELECT into competing INSERTs. Namespaced to this
                # tenant (tenants never share idempotency keys); xact scope releases
                # on COMMIT.
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:tenant), hashtext(:key))"),
                    {"tenant": str(tenant_id), "key": payload.idempotency_key})
                # Re-read under the lock: the winner may have committed while we queued.
                existing = connection.execute(
                    text("""SELECT id::text AS id, checksum_sha256, consent_id::text AS consent_id,
                                   client_id::text AS client_id
                            FROM attachments WHERE tenant_id=:tenant AND idempotency_key=:key
                            FOR UPDATE"""),
                    {"tenant": str(tenant_id), "key": payload.idempotency_key}
                ).mappings().one_or_none()
                if existing is not None:
                    if (existing["checksum_sha256"] != digest
                            or existing["client_id"] != str(client_id)
                            or existing["consent_id"] != str(payload.consent_id)):
                        # Same key, different object: a caller bug, never a silent swap.
                        raise HTTPException(status_code=409,
                                            detail={"code": "idempotency_conflict"})
                    return JSONResponse(status_code=200, headers={
                        "X-Request-ID": str(request.state.request_id)},
                        content={"attachment_id": existing["id"], "replayed": True})
                connection.execute(
                    text("""INSERT INTO attachments
                            (tenant_id,id,client_id,consent_id,appointment_id,idempotency_key,
                             storage_key,purpose,content_type,size_bytes,checksum_sha256,
                             retention_until,uploaded_by_issuer,uploaded_by_subject)
                            VALUES (:tenant,:id,:client,:consent,:appointment,:key,
                                    :storage,:purpose,:content_type,:size,:checksum,
                                    (SELECT now() + (:days || ' days')::interval),
                                    :issuer,:subject)"""),
                    {"tenant": str(tenant_id), "id": str(attachment_id), "client": str(client_id),
                     "consent": str(payload.consent_id),
                     "appointment": str(payload.appointment_id) if payload.appointment_id else None,
                     "key": payload.idempotency_key, "storage": digest, "purpose": payload.purpose,
                     "content_type": payload.content_type, "size": len(content),
                     "checksum": digest, "days": retention_days,
                     "issuer": identity.issuer, "subject": identity.subject})
                # The object is written before commit, so a committed row can never
                # point at bytes that do not exist. A rollback may leave an orphan
                # object behind, which a content-addressed store can drop safely.
                _write_object(_object_path(tenant_id, digest), content)
                insert_audit(connection, tenant_id, identity, action="attachment.uploaded",
                             resource_type="attachment", resource_id=attachment_id,
                             reason=f"{payload.purpose} uploaded under a consented purpose",
                             request_id=request.state.request_id,
                             changed_fields=["purpose", "content_type", "size_bytes",
                                             "retention_until"])
            return JSONResponse(status_code=201, content={"attachment_id": str(attachment_id)},
                                 headers={"X-Request-ID": str(request.state.request_id)})
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/attachments/{attachment_id}", tags=["crm"])
    def get_attachment(tenant_id: uuid.UUID, attachment_id: uuid.UUID, request: Request,
                       identity=Depends(require_identity)) -> Response:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.read")
                row = attachment_or_404(connection, tenant_id, attachment_id)
                if row["state"] == "deleted":
                    raise HTTPException(status_code=410,
                                        detail={"code": "attachment_deleted"})
                consent_live_for_download(connection, tenant_id, row)
                if row["retention_until"] <= row["db_now"]:
                    raise HTTPException(status_code=409, detail={
                        "code": "retention_expired",
                        "retention_until": row["retention_until"].isoformat()})
                path = _object_path(tenant_id, row["storage_key"])
                if not path.exists():
                    # The row survives but the object does not. Saying so is better
                    # than a 500, and it is still a refusal to serve bytes.
                    raise HTTPException(status_code=410,
                                        detail={"code": "attachment_content_missing"})
                content = path.read_bytes()
                # A private photograph of a client is read: that read is itself an
                # auditable event, not just the upload.
                insert_audit(connection, tenant_id, identity, action="attachment.downloaded",
                             resource_type="attachment", resource_id=attachment_id,
                             reason=f"{row['purpose']} downloaded under consent",
                             request_id=request.state.request_id,
                             changed_fields=["state"])
            return Response(content=content, media_type=row["content_type"], headers={
                "X-Request-ID": str(request.state.request_id),
                "X-Content-SHA256": row["checksum_sha256"],
                "X-Content-Type-Options": "nosniff"})
        finally:
            engine.dispose()

    @app.delete("/v1/tenants/{tenant_id}/attachments/{attachment_id}", tags=["crm"])
    def delete_attachment(tenant_id: uuid.UUID, attachment_id: uuid.UUID,
                          payload: AttachmentDeleteIn, request: Request,
                          identity=Depends(require_identity)) -> Response:
        engine = app.state.engine_factory()
        purge: Path | None = None
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:tenant), hashtext(:key))"),
                    {"tenant": str(tenant_id), "key": payload.idempotency_key})
                row = connection.execute(
                    text("""SELECT id::text AS id, storage_key, state
                            FROM attachments WHERE tenant_id=:tenant AND id=:id FOR UPDATE"""),
                    {"tenant": str(tenant_id), "id": str(attachment_id)}
                ).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404,
                                        detail={"code": "attachment_not_found"})
                if row["state"] == "deleted":
                    # Idempotent replay: the same key on an already-deleted attachment
                    # reports the state it is in and writes nothing - no second audit
                    # row, no second purge.
                    return JSONResponse(status_code=200, headers={
                        "X-Request-ID": str(request.state.request_id)},
                        content={"attachment_id": row["id"], "state": "deleted",
                                 "replayed": True})
                connection.execute(
                    text("""UPDATE attachments SET state='deleted', deleted_at=now()
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(attachment_id)})
                insert_audit(connection, tenant_id, identity, action="attachment.deleted",
                             resource_type="attachment", resource_id=attachment_id,
                             reason=payload.reason, request_id=request.state.request_id,
                             changed_fields=["state", "deleted_at"])
                # The object is unlinked only when no active row still cites it: the
                # store is content-addressed, so another row may legitimately share
                # these exact bytes (see the module docstring).
                still_cited = connection.execute(
                    text("""SELECT 1 FROM attachments
                            WHERE tenant_id=:tenant AND storage_key=:key AND state='active'"""),
                    {"tenant": str(tenant_id), "key": row["storage_key"]}
                ).scalar_one_or_none()
                if still_cited is None:
                    purge = _object_path(tenant_id, row["storage_key"])
            # After commit: a rollback must never remove bytes a surviving row needs.
            if purge is not None:
                try:
                    purge.unlink()
                except OSError:
                    # A leftover object is a storage problem, not a failed delete;
                    # the row already records the deletion.
                    pass
            return JSONResponse(status_code=200, headers={
                "X-Request-ID": str(request.state.request_id)},
                content={"attachment_id": row["id"], "state": "deleted", "replayed": False})
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/clients/{client_id}/attachments", tags=["crm"])
    def list_attachments(tenant_id: uuid.UUID, client_id: uuid.UUID, request: Request,
                         identity=Depends(require_identity),
                         limit: int = 20, offset: int = 0) -> dict:
        engine = app.state.engine_factory()
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail={"code": "invalid_limit"})
        if offset < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_offset"})
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.read")
                client_or_404(connection, tenant_id, client_id)
                # Metadata only: the listing never returns bytes, and state is
                # included so a deleted attachment is visible as an audit fact
                # rather than silently missing from the page.
                total = connection.execute(
                    text("SELECT count(*) FROM attachments WHERE tenant_id=:tenant AND client_id=:client"),
                    {"tenant": str(tenant_id), "client": str(client_id)}).scalar_one()
                rows = connection.execute(
                    text("""SELECT id::text AS id, consent_id::text AS consent_id,
                                   appointment_id::text AS appointment_id, purpose, content_type,
                                   size_bytes, checksum_sha256, retention_until, state,
                                   deleted_at, created_at
                            FROM attachments WHERE tenant_id=:tenant AND client_id=:client
                            ORDER BY created_at DESC, id LIMIT :limit OFFSET :offset"""),
                    {"tenant": str(tenant_id), "client": str(client_id),
                     "limit": limit, "offset": offset}).mappings().all()
            return {"items": [_metadata(row) for row in rows], "total": total,
                    "limit": limit, "offset": offset}
        finally:
            engine.dispose()


def _metadata(row) -> dict:
    return {"id": row["id"], "consent_id": row["consent_id"],
            "appointment_id": row["appointment_id"], "purpose": row["purpose"],
            "content_type": row["content_type"], "size_bytes": row["size_bytes"],
            "checksum_sha256": row["checksum_sha256"],
            "retention_until": row["retention_until"].isoformat(),
            "state": row["state"],
            "deleted_at": row["deleted_at"].isoformat() if row["deleted_at"] else None,
            "created_at": row["created_at"].isoformat()}
