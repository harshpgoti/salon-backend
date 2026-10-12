"""Verified client export, correction and deletion (FR-016, TASK-014).

The workflow mirrors operations.md:71 exactly: a request is received and verified, a legal
hold is inspected, the canonical records are exported/corrected/deleted, and a
non-identifying tombstone is written so a later restore cannot resurrect the subject.

Three properties are enforced here rather than promised in the UI:

- **No deletion without verification.** A deletion request with an empty verification is
  refused at the schema boundary, and a deletion while an unreleased retention hold exists
  is refused with a named conflict, not performed quietly.
- **A tombstone never identifies the subject.** It stores a hash, so replaying it against a
  restored backup neither needs the original id nor leaks one.
- **Deletion is a tombstone plus a purge, not just a soft flag.** The client is retired,
  its contacts and dependent rows removed, and the tombstone recorded so restore knows.

Dependencies are injected for the same reason the sibling CRM modules inject them.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

# Rows that carry client data and are removed on an approved deletion, in FK-safe order.
_PURGE_ORDER = (
    ("client_field_values", "entity_id"),
    ("visit_notes", "client_id"),
    ("client_preferences", "client_id"),
    ("client_contacts", "client_id"),
    ("import_rows", "client_id"),
)
# The retention window proposed by operations.md:52, used to set a request deadline.
RESPONSE_TARGET_DAYS = 20


def subject_hash(tenant_id: str, client_id: str) -> str:
    """A stable, non-identifying handle for a subject within one tenant."""
    return hashlib.sha256(f"{tenant_id}:{client_id}".encode("utf-8")).hexdigest()


class PrivacyRequestIn(BaseModel):
    client_id: uuid.UUID
    request_type: str = Field(pattern="^(access|export|correction|deletion)$")
    # Free-text description of how authority was checked (e.g. "in-person photo ID").
    verification: str = Field(min_length=1, max_length=240)


class CorrectionIn(BaseModel):
    request_id: uuid.UUID
    field: str = Field(min_length=1, max_length=40)
    value: str = Field(max_length=320)


class DeletionIn(BaseModel):
    request_id: uuid.UUID
    # An operator may not delete a held subject unless they say why the hold no longer applies.
    hold_override_reason: str | None = Field(default=None, max_length=240)
    idempotency_key: str = Field(min_length=8, max_length=64)


class HoldIn(BaseModel):
    client_id: uuid.UUID
    basis: str = Field(min_length=1, max_length=240)


def register_privacy_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    def request_or_404(connection, tenant_id: uuid.UUID, request_id: uuid.UUID):
        row = connection.execute(
            text("""SELECT * FROM privacy_requests WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": str(tenant_id), "id": str(request_id)}).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "privacy_request_not_found"})
        return row

    def active_hold(connection, tenant_id: uuid.UUID, client_id: uuid.UUID):
        return connection.execute(
            text("""SELECT id::text AS id, basis FROM retention_holds
                    WHERE tenant_id=:tenant AND client_id=:client AND released_at IS NULL
                    ORDER BY created_at LIMIT 1"""),
            {"tenant": str(tenant_id), "client": str(client_id)}).mappings().one_or_none()

    def collect_export(connection, tenant_id: uuid.UUID, client_id: uuid.UUID) -> dict:
        """The canonical, tenant-scoped records for one client. No other tenant is reachable."""
        client = connection.execute(
            text("""SELECT id::text AS id, name, preferred_name, locale, lifecycle, source,
                           version, created_at
                    FROM clients WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": str(tenant_id), "id": str(client_id)}).mappings().one_or_none()
        if client is None:
            raise HTTPException(status_code=404, detail={"code": "client_not_found"})
        contacts = connection.execute(
            text("""SELECT kind, display_value, verification, is_primary, source
                    FROM client_contacts WHERE tenant_id=:tenant AND client_id=:client"""),
            {"tenant": str(tenant_id), "client": str(client_id)}).mappings().all()
        notes = connection.execute(
            text("""SELECT class, visibility, body, created_at FROM visit_notes
                    WHERE tenant_id=:tenant AND client_id=:client ORDER BY created_at"""),
            {"tenant": str(tenant_id), "client": str(client_id)}).mappings().all()
        prefs = connection.execute(
            text("""SELECT preference_type, value_json, source FROM client_preferences
                    WHERE tenant_id=:tenant AND client_id=:client"""),
            {"tenant": str(tenant_id), "client": str(client_id)}).mappings().all()
        return {
            "client": {**dict(client), "created_at": client["created_at"].isoformat()},
            "contacts": [dict(c) for c in contacts],
            "visit_notes": [{**dict(n), "created_at": n["created_at"].isoformat()} for n in notes],
            "preferences": [dict(p) for p in prefs],
        }

    @app.post("/v1/tenants/{tenant_id}/privacy-requests", tags=["crm"], status_code=201)
    def create_privacy_request(tenant_id: uuid.UUID, payload: PrivacyRequestIn, request: Request,
                              identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        request_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.privacy.manage")
                exists = connection.execute(
                    text("SELECT 1 FROM clients WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(payload.client_id)}).scalar_one_or_none()
                if exists is None:
                    raise HTTPException(status_code=404, detail={"code": "client_not_found"})
                connection.execute(
                    text("""INSERT INTO privacy_requests
                            (tenant_id,id,client_id,request_type,verification,deadline,
                             requested_by_issuer,requested_by_subject)
                            VALUES (:tenant,:id,:client,:type,:verification,
                                    (now() + (:days || ' days')::interval)::date,:issuer,:subject)"""),
                    {"tenant": str(tenant_id), "id": str(request_id), "client": str(payload.client_id),
                     "type": payload.request_type, "verification": payload.verification,
                     "days": RESPONSE_TARGET_DAYS,
                     "issuer": identity.issuer, "subject": identity.subject})
                insert_audit(connection, tenant_id, identity, action="privacy_request.received",
                             resource_type="privacy_request", resource_id=request_id,
                             reason=f"{payload.request_type} request received",
                             request_id=request.state.request_id,
                             changed_fields=["request_type", "verification"])
                row = request_or_404(connection, tenant_id, request_id)
            return {**dict(row), "id": str(row["id"]), "client_id": str(row["client_id"]),
                    "deadline": row["deadline"].isoformat() if row["deadline"] else None,
                    "created_at": row["created_at"].isoformat()}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/privacy-requests", tags=["crm"])
    def list_privacy_requests(tenant_id: uuid.UUID, identity=Depends(require_identity),
                              limit: int = 20, offset: int = 0) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.privacy.manage")
                total = connection.execute(
                    text("SELECT count(*) FROM privacy_requests WHERE tenant_id=:tenant"),
                    {"tenant": str(tenant_id)}).scalar_one()
                rows = connection.execute(
                    text("""SELECT id::text AS id, client_id::text AS client_id, request_type,
                                   status, deadline, created_at FROM privacy_requests
                            WHERE tenant_id=:tenant ORDER BY created_at DESC
                            LIMIT :limit OFFSET :offset"""),
                    {"tenant": str(tenant_id), "limit": limit, "offset": offset}).mappings().all()
            return {"items": [{**dict(r), "deadline": r["deadline"].isoformat() if r["deadline"] else None,
                               "created_at": r["created_at"].isoformat()} for r in rows],
                    "total": total, "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/privacy-requests/export", tags=["crm"])
    def run_export(tenant_id: uuid.UUID, payload: PrivacyRequestIn, request: Request,
                   identity=Depends(require_identity)) -> dict:
        """Produce the client's records for a verified access/export request."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.privacy.manage")
                data = collect_export(connection, tenant_id, payload.client_id)
                insert_audit(connection, tenant_id, identity, action="client.exported",
                             resource_type="client", resource_id=payload.client_id,
                             reason="client data exported for a verified request",
                             request_id=request.state.request_id,
                             changed_fields=["export"])
            return {"client_id": str(payload.client_id), "data": data}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/privacy-requests/correction", tags=["crm"])
    def run_correction(tenant_id: uuid.UUID, payload: CorrectionIn, request: Request,
                       identity=Depends(require_identity)) -> dict:
        """Apply a verified correction to a canonical field and close the request."""
        engine = app.state.engine_factory()
        allowed = {"name", "preferred_name", "locale"}
        if payload.field not in allowed:
            raise HTTPException(status_code=422, detail={"code": "field_not_correctable",
                                                         "allowed": sorted(allowed)})
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.privacy.manage")
                record = request_or_404(connection, tenant_id, payload.request_id)
                if record["request_type"] != "correction":
                    raise HTTPException(status_code=422, detail={"code": "not_a_correction_request"})
                updated = connection.execute(
                    text(f"""UPDATE clients SET {payload.field}=:value, version=version+1
                             WHERE tenant_id=:tenant AND id=:client RETURNING *"""),
                    {"value": payload.value, "tenant": str(tenant_id),
                     "client": str(record["client_id"])}).mappings().one_or_none()
                if updated is None:
                    raise HTTPException(status_code=404, detail={"code": "client_not_found"})
                connection.execute(
                    text("""UPDATE privacy_requests SET status='completed', completed_at=now()
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(payload.request_id)})
                insert_audit(connection, tenant_id, identity, action="client.corrected",
                             resource_type="client", resource_id=record["client_id"],
                             reason=f"correction of {payload.field} for a verified request",
                             request_id=request.state.request_id, changed_fields=[payload.field])
            return {"request_id": str(payload.request_id), "status": "completed", "field": payload.field}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/privacy-requests/deletion", tags=["crm"])
    def run_deletion(tenant_id: uuid.UUID, payload: DeletionIn, request: Request,
                     identity=Depends(require_identity)) -> dict:
        """Delete a verified subject: purge canonical rows and write a non-identifying tombstone."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.privacy.manage")
                record = request_or_404(connection, tenant_id, payload.request_id)
                if record["request_type"] != "deletion":
                    raise HTTPException(status_code=422, detail={"code": "not_a_deletion_request"})
                client_id = record["client_id"]
                if record["status"] == "completed":
                    return {"request_id": str(payload.request_id), "status": "completed", "replayed": True}

                # A legal hold blocks deletion until it is released or explicitly overridden
                # with a reason (operations.md:71). Silence is not an override.
                hold = active_hold(connection, tenant_id, client_id)
                if hold is not None and not payload.hold_override_reason:
                    raise HTTPException(status_code=409, detail={
                        "code": "retention_hold_active", "hold_id": hold["id"], "basis": hold["basis"]})

                hashed = subject_hash(str(tenant_id), str(client_id))
                for table, column in _PURGE_ORDER:
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id=:tenant AND {column}=:client"),
                        {"tenant": str(tenant_id), "client": str(client_id)})
                connection.execute(
                    text("""UPDATE client_duplicate_candidates SET resolution='merged', resolved_at=now()
                            WHERE tenant_id=:tenant AND resolved_at IS NULL
                              AND (client_id=:client OR other_client_id=:client)"""),
                    {"tenant": str(tenant_id), "client": str(client_id)})
                # The client row itself is retired rather than removed, so foreign keys and
                # the audit trail stay intact; the tombstone is what survives a restore.
                connection.execute(
                    text("""UPDATE clients SET deleted_at=now(), version=version+1,
                            name='Deleted client', preferred_name=NULL
                            WHERE tenant_id=:tenant AND id=:client"""),
                    {"tenant": str(tenant_id), "client": str(client_id)})
                connection.execute(
                    text("""INSERT INTO deletion_tombstones
                            (tenant_id,id,subject_hash,resource_type)
                            VALUES (:tenant,:id,:hash,'client')"""),
                    {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "hash": hashed})
                connection.execute(
                    text("""UPDATE privacy_requests SET status='completed', completed_at=now()
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(payload.request_id)})
                insert_audit(connection, tenant_id, identity, action="client.deleted",
                             resource_type="client", resource_id=client_id,
                             reason="client data deleted for a verified request",
                             request_id=request.state.request_id,
                             changed_fields=["deleted_at", "tombstone"])
            return {"request_id": str(payload.request_id), "status": "completed",
                    "subject_hash": hashed, "replayed": False}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/retention-holds", tags=["crm"], status_code=201)
    def create_hold(tenant_id: uuid.UUID, payload: HoldIn, request: Request,
                    identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        hold_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.privacy.manage")
                exists = connection.execute(
                    text("SELECT 1 FROM clients WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(payload.client_id)}).scalar_one_or_none()
                if exists is None:
                    raise HTTPException(status_code=404, detail={"code": "client_not_found"})
                connection.execute(
                    text("""INSERT INTO retention_holds
                            (tenant_id,id,client_id,basis,held_by_issuer,held_by_subject)
                            VALUES (:tenant,:id,:client,:basis,:issuer,:subject)"""),
                    {"tenant": str(tenant_id), "id": str(hold_id), "client": str(payload.client_id),
                     "basis": payload.basis, "issuer": identity.issuer, "subject": identity.subject})
                insert_audit(connection, tenant_id, identity, action="retention_hold.created",
                             resource_type="retention_hold", resource_id=hold_id,
                             reason="retention hold placed",
                             request_id=request.state.request_id, changed_fields=["basis"])
            return {"id": str(hold_id), "client_id": str(payload.client_id), "basis": payload.basis}
        finally:
            engine.dispose()