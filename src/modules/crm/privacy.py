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
import base64
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

# Rows that carry client data and are removed on an approved deletion, in FK-safe order.
_PURGE_ORDER = (
    ("client_field_values", "entity_id"),
    ("attachments", "client_id"),
    ("visit_services", "visit_id"),
    ("visits", "client_id"),
    ("salon_receipt_entries", "client_id"),
    ("closure_impacts", "client_id"),
    ("offer_redemptions", "client_id"),
    ("campaign_sends", "client_id"),
    ("client_tags", "client_id"),
    ("waitlist_entries", "client_id"),
    ("client_rebooking_overrides", "client_id"),
    ("rebooking_cycles", "client_id"),
    ("client_channel_prefs", "client_id"),
    ("consents", "client_id"),
    ("suppressions", "client_id"),
    ("communications", "client_id"),
    ("appointments", "client_id"),
    ("recurring_series", "client_id"),
    ("follow_up_tasks", "client_id"),
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


class PrivacyActionIn(BaseModel):
    request_id: uuid.UUID


class DeletionIn(BaseModel):
    request_id: uuid.UUID
    idempotency_key: str = Field(min_length=8, max_length=64)


class HoldIn(BaseModel):
    client_id: uuid.UUID
    basis: str = Field(min_length=1, max_length=240)


class HoldReleaseIn(BaseModel):
    reason: str = Field(min_length=1, max_length=240)


class _RetryDeletionCleanup(Exception):
    def __init__(self, subject_hash_value: str):
        self.subject_hash_value = subject_hash_value


def register_privacy_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    def request_or_404(connection, tenant_id: uuid.UUID, request_id: uuid.UUID, lock: bool = True):
        row = connection.execute(
            text("""SELECT * FROM privacy_requests WHERE tenant_id=:tenant AND id=:id""" + (" FOR UPDATE" if lock else "")),
            {"tenant": str(tenant_id), "id": str(request_id)}).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "privacy_request_not_found"})
        return row

    def active_hold(connection, tenant_id: uuid.UUID, client_id: uuid.UUID):
        return connection.execute(
            text("""SELECT id::text AS id, basis FROM retention_holds
                    WHERE tenant_id=:tenant AND client_id=:client AND released_at IS NULL
                    ORDER BY created_at LIMIT 1 FOR UPDATE"""),
            {"tenant": str(tenant_id), "client": str(client_id)}).mappings().one_or_none()

    def finish_file_purge(engine, tenant_id: uuid.UUID, request_id: uuid.UUID) -> None:
        """Drain durable object cleanup; a retry can finish after a process interruption."""
        from .attachments import _object_path
        failed = False
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": str(tenant_id)})
            jobs = connection.execute(text("""SELECT id, storage_key FROM privacy_file_purge_jobs
                WHERE tenant_id=:tenant AND privacy_request_id=:request AND status='pending'
                ORDER BY created_at FOR UPDATE"""),
                {"tenant": str(tenant_id), "request": str(request_id)}).mappings().all()
            for job in jobs:
                referenced = connection.execute(text("""SELECT 1 FROM attachments
                    WHERE tenant_id=:tenant AND storage_key=:key AND state='active' LIMIT 1"""),
                    {"tenant": str(tenant_id), "key": job["storage_key"]}).scalar_one_or_none()
                try:
                    if referenced is None:
                        _object_path(tenant_id, job["storage_key"]).unlink(missing_ok=True)
                except OSError:
                    connection.execute(text("""UPDATE privacy_file_purge_jobs SET attempts=attempts+1,
                        last_error='object_unlink_failed' WHERE tenant_id=:tenant AND id=:id"""),
                        {"tenant": str(tenant_id), "id": str(job["id"])})
                    failed = True
                    continue
                connection.execute(text("""UPDATE privacy_file_purge_jobs SET status='completed',
                    completed_at=now(), last_error=NULL WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(job["id"])})
        if failed:
            raise HTTPException(status_code=503, detail={
                "code": "client_file_purge_pending", "request_id": str(request_id),
                "retryable": True})

    def complete_deletion_request(engine, tenant_id: uuid.UUID, request_id: uuid.UUID) -> None:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": str(tenant_id)})
            pending = connection.execute(text("""SELECT 1 FROM privacy_file_purge_jobs
                WHERE tenant_id=:tenant AND privacy_request_id=:request AND status='pending' LIMIT 1"""),
                {"tenant": str(tenant_id), "request": str(request_id)}).scalar_one_or_none()
            if pending is not None:
                raise HTTPException(status_code=503, detail={
                    "code": "client_file_purge_pending", "request_id": str(request_id),
                    "retryable": True})
            connection.execute(text("""UPDATE privacy_requests SET status='completed', completed_at=now()
                WHERE tenant_id=:tenant AND id=:id AND status='in_progress'"""),
                {"tenant": str(tenant_id), "id": str(request_id)})

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
        def rows(sql: str) -> list[dict]:
            return [json_safe(dict(row)) for row in connection.execute(text(sql), {
                "tenant": str(tenant_id), "client": str(client_id)}).mappings().all()]

        attachments = rows("""SELECT id::text AS id, purpose, content_type, size_bytes,
                    retention_until, state, storage_key, created_at FROM attachments
                WHERE tenant_id=:tenant AND client_id=:client ORDER BY created_at""")
        if attachments:
            from .attachments import _object_path
            for attachment in attachments:
                storage_key = attachment.pop("storage_key")
                attachment["content_base64"] = None
                if attachment["state"] == "active":
                    try:
                        attachment["content_base64"] = base64.b64encode(
                            _object_path(tenant_id, storage_key).read_bytes()).decode("ascii")
                    except OSError as exc:
                        raise HTTPException(status_code=503, detail={
                            "code": "client_export_attachment_unavailable",
                            "attachment_id": attachment["id"]}) from exc

        return {
            "client": json_safe(dict(client)),
            "contacts": [json_safe(dict(c)) for c in contacts],
            "visit_notes": [json_safe(dict(n)) for n in notes],
            "preferences": [json_safe(dict(p)) for p in prefs],
            "custom_fields": rows("""SELECT d.key, d.label, d.type, d.sensitivity,
                    v.value_text, v.value_number, v.value_date, v.value_boolean, v.updated_at
                FROM client_field_values v JOIN client_field_definitions d
                  ON d.tenant_id=v.tenant_id AND d.id=v.definition_id
                WHERE v.tenant_id=:tenant AND v.entity_type='client' AND v.entity_id=:client
                ORDER BY d.key"""),
            "appointments": rows("""SELECT id::text AS id, location_id::text AS location_id,
                    staff_id::text AS staff_id, starts_at, ends_at, local_timezone, status,
                    source, cancellation_reason, created_at, updated_at
                FROM appointments WHERE tenant_id=:tenant AND client_id=:client ORDER BY starts_at"""),
            "appointment_services": rows("""SELECT aps.appointment_id::text AS appointment_id,
                    aps.sequence, aps.service_name, aps.staff_id::text AS staff_id,
                    aps.price_minor, aps.currency, aps.duration_minutes, aps.pre_buffer_minutes,
                    aps.post_buffer_minutes, aps.starts_at, aps.ends_at
                FROM appointment_services aps JOIN appointments a
                  ON a.tenant_id=aps.tenant_id AND a.id=aps.appointment_id
                WHERE a.tenant_id=:tenant AND a.client_id=:client ORDER BY a.starts_at, aps.sequence"""),
            "visits": rows("""SELECT id::text AS id, appointment_id::text AS appointment_id,
                    completed_at, created_at FROM visits
                WHERE tenant_id=:tenant AND client_id=:client ORDER BY completed_at"""),
            "visit_services": rows("""SELECT vs.visit_id::text AS visit_id, vs.sequence,
                    vs.service_name, vs.staff_id::text AS staff_id, vs.price_minor, vs.currency,
                    vs.duration_minutes, vs.pre_buffer_minutes, vs.post_buffer_minutes,
                    vs.starts_at, vs.ends_at
                FROM visit_services vs JOIN visits v ON v.tenant_id=vs.tenant_id AND v.id=vs.visit_id
                WHERE v.tenant_id=:tenant AND v.client_id=:client ORDER BY v.completed_at, vs.sequence"""),
            "receipts": rows("""SELECT id::text AS id, appointment_id::text AS appointment_id,
                    amount_minor, currency, method, kind, note, posted_at, created_at
                FROM salon_receipt_entries WHERE tenant_id=:tenant AND client_id=:client
                ORDER BY posted_at"""),
            "communications": rows("""SELECT id::text AS id, contact_value, channel, purpose,
                    state, source_type, source_id::text AS source_id, scheduled_at, expires_at,
                    dispatched_at, created_at
                FROM communications WHERE tenant_id=:tenant AND client_id=:client ORDER BY created_at"""),
            "consents": rows("""SELECT id::text AS id, channel, purpose, status, source,
                    wording_version, proof_reference, granted_at, revoked_at, expires_at, created_at FROM consents
                WHERE tenant_id=:tenant AND client_id=:client ORDER BY created_at"""),
            "suppressions": rows("""SELECT id::text AS id, contact_value, channel, reason, suppressed_at, created_at
                FROM suppressions WHERE tenant_id=:tenant AND client_id=:client ORDER BY created_at"""),
            "tags": rows("""SELECT tag, created_at FROM client_tags
                WHERE tenant_id=:tenant AND client_id=:client ORDER BY tag"""),
            "client_channel_preferences": rows("""SELECT purpose, primary_channel, fallback_channel,
                    created_at, updated_at FROM client_channel_prefs
                WHERE tenant_id=:tenant AND client_id=:client ORDER BY purpose"""),
            "rebooking_overrides": rows("""SELECT service_id::text AS service_id, interval_days,
                    created_at, updated_at FROM client_rebooking_overrides
                WHERE tenant_id=:tenant AND client_id=:client ORDER BY service_id"""),
            "rebooking_cycles": rows("""SELECT id::text AS id, service_id::text AS service_id,
                    service_version, visit_id::text AS visit_id, source_appointment_id::text AS source_appointment_id,
                    interval_days, due_date, status, created_at, updated_at FROM rebooking_cycles
                WHERE tenant_id=:tenant AND client_id=:client ORDER BY created_at"""),
            "follow_up_tasks": rows("""SELECT id::text AS id, kind, title, detail, source_type,
                    status, resolved_at, resolution_note, created_at, updated_at FROM follow_up_tasks
                WHERE tenant_id=:tenant AND client_id=:client ORDER BY created_at"""),
            "waitlist_entries": rows("""SELECT id::text AS id, location_id::text AS location_id,
                    service_id::text AS service_id, staff_id::text AS staff_id, date_from, date_to,
                    state, created_at, updated_at FROM waitlist_entries
                WHERE tenant_id=:tenant AND client_id=:client ORDER BY created_at"""),
            "waitlist_offers": rows("""SELECT o.id::text AS id, e.id::text AS waitlist_entry_id,
                    o.location_id::text AS location_id, o.staff_id::text AS staff_id,
                    o.service_id::text AS service_id, o.starts_at, o.ends_at, o.local_timezone,
                    o.state, o.expires_at, o.created_at FROM slot_offers o
                JOIN waitlist_entries e ON e.tenant_id=o.tenant_id AND e.id=o.entry_id
                WHERE e.tenant_id=:tenant AND e.client_id=:client ORDER BY o.created_at"""),
            "recurring_series": rows("""SELECT id::text AS id, staff_id::text AS staff_id,
                    service_id::text AS service_id, location_id::text AS location_id,
                    local_time, frequency, interval_weeks, byweekday, start_date, end_rule,
                    occurrence_count, until_date, state, version, created_at, updated_at
                FROM recurring_series WHERE tenant_id=:tenant AND client_id=:client ORDER BY created_at"""),
            "import_provenance": rows("""SELECT i.id::text AS import_id, i.filename,
                    i.status AS import_status, i.created_at AS imported_at, r.row_number,
                    r.raw, r.normalized, r.consent_proof, r.marketing_eligible, r.status AS row_status
                FROM import_rows r JOIN imports i ON i.tenant_id=r.tenant_id AND i.id=r.import_id
                WHERE r.tenant_id=:tenant AND r.client_id=:client ORDER BY i.created_at, r.row_number"""),
            "closure_impacts": rows("""SELECT id::text AS id, location_id::text AS location_id,
                    appointment_id::text AS appointment_id, exception_date, starts_at, ends_at,
                    status, resolution, resolved_at, resolution_reason, created_at
                FROM closure_impacts WHERE tenant_id=:tenant AND client_id=:client ORDER BY created_at"""),
            "campaign_sends": rows("""SELECT id::text AS id, campaign_id::text AS campaign_id,
                    communication_id::text AS communication_id, state, created_at, updated_at
                FROM campaign_sends WHERE tenant_id=:tenant AND client_id=:client ORDER BY created_at"""),
            "offer_redemptions": rows("""SELECT id::text AS id, campaign_id::text AS campaign_id,
                    appointment_id::text AS appointment_id, communication_id::text AS communication_id,
                    code, redeemed_at, created_at FROM offer_redemptions
                WHERE tenant_id=:tenant AND client_id=:client ORDER BY created_at"""),
            "attachments": attachments,
        }

    def json_safe(value):
        if isinstance(value, dict):
            return {key: json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(item) for item in value]
        if isinstance(value, (datetime, date)):
            return value.isoformat()
        if isinstance(value, Decimal):
            return str(value)
        if isinstance(value, uuid.UUID):
            return str(value)
        return value

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
                                   status, deadline, created_at,
                                   status='in_progress' AND EXISTS (
                                     SELECT 1 FROM privacy_file_purge_jobs j
                                     WHERE j.tenant_id=privacy_requests.tenant_id
                                       AND j.privacy_request_id=privacy_requests.id AND j.status='pending'
                                   ) AS cleanup_pending,
                                   CASE WHEN status='in_progress' AND EXISTS (
                                     SELECT 1 FROM privacy_file_purge_jobs j
                                     WHERE j.tenant_id=privacy_requests.tenant_id
                                       AND j.privacy_request_id=privacy_requests.id AND j.status='pending'
                                   ) THEN deletion_idempotency_key END AS cleanup_idempotency_key
                            FROM privacy_requests
                            WHERE tenant_id=:tenant ORDER BY created_at DESC
                            LIMIT :limit OFFSET :offset"""),
                    {"tenant": str(tenant_id), "limit": limit, "offset": offset}).mappings().all()
            return {"items": [{**dict(r), "deadline": r["deadline"].isoformat() if r["deadline"] else None,
                               "created_at": r["created_at"].isoformat()} for r in rows],
                    "total": total, "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/privacy-requests/export", tags=["crm"])
    def run_export(tenant_id: uuid.UUID, payload: PrivacyActionIn, request: Request,
                   identity=Depends(require_identity)) -> dict:
        """Produce the client's records for a verified access/export request."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.privacy.manage")
                record = request_or_404(connection, tenant_id, payload.request_id)
                if record["request_type"] not in {"access", "export"}:
                    raise HTTPException(status_code=422, detail={"code": "not_an_export_request"})
                if record["status"] == "completed":
                    raise HTTPException(status_code=409, detail={"code": "privacy_request_completed"})
                data = collect_export(connection, tenant_id, record["client_id"])
                connection.execute(text("""UPDATE privacy_requests SET status='completed', completed_at=now()
                    WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(payload.request_id)})
                insert_audit(connection, tenant_id, identity, action="client.exported",
                             resource_type="client", resource_id=record["client_id"],
                             reason="client data exported for a verified request",
                             request_id=request.state.request_id,
                             changed_fields=["export"])
            return {"request_id": str(payload.request_id), "client_id": str(record["client_id"]), "data": data}
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
                if record["status"] == "completed":
                    raise HTTPException(status_code=409, detail={"code": "privacy_request_completed"})
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
        attachment_keys: list[str] = []
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.privacy.manage")
                # Cleanup uses a second transaction and must not hold this row lock while
                # finalizing the request. Queue-row locks serialize concurrent retries.
                record = request_or_404(connection, tenant_id, payload.request_id)
                if record["request_type"] != "deletion":
                    raise HTTPException(status_code=422, detail={"code": "not_a_deletion_request"})
                stored_key = record.get("deletion_idempotency_key")
                if stored_key is not None and stored_key != payload.idempotency_key:
                    raise HTTPException(status_code=409, detail={"code": "idempotency_key_conflict"})
                if record["status"] == "completed":
                    finish_file_purge(engine, tenant_id, payload.request_id)
                    return {"request_id": str(payload.request_id), "status": "completed", "replayed": True}
                if record["status"] == "in_progress" and record["client_id"] is None and record["subject_hash"]:
                    raise _RetryDeletionCleanup(record["subject_hash"])
                client_id = record["client_id"]

                # Serialize hold checks with hold creation by locking the subject row.
                connection.execute(text("""SELECT id FROM clients
                    WHERE tenant_id=:tenant AND id=:client FOR UPDATE"""),
                    {"tenant": str(tenant_id), "client": str(client_id)}).one_or_none()
                hold = active_hold(connection, tenant_id, client_id)
                if hold is not None:
                    raise HTTPException(status_code=409, detail={
                        "code": "retention_hold_active", "hold_id": hold["id"], "basis": hold["basis"]})

                connection.execute(text("""UPDATE privacy_requests
                    SET deletion_idempotency_key=COALESCE(deletion_idempotency_key,:key)
                    WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(payload.request_id), "key": payload.idempotency_key})

                hashed = subject_hash(str(tenant_id), str(client_id))
                attachment_keys = [row["storage_key"] for row in connection.execute(text("""
                    SELECT storage_key FROM attachments
                    WHERE tenant_id=:tenant AND client_id=:client"""),
                    {"tenant": str(tenant_id), "client": str(client_id)}).mappings().all()]
                for storage_key in set(attachment_keys):
                    connection.execute(text("""INSERT INTO privacy_file_purge_jobs
                        (tenant_id, privacy_request_id, storage_key) VALUES (:tenant,:request,:key)
                        ON CONFLICT (tenant_id, privacy_request_id, storage_key) DO NOTHING"""),
                        {"tenant": str(tenant_id), "request": str(payload.request_id), "key": storage_key})
                for table, column in _PURGE_ORDER:
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE tenant_id=:tenant AND {column}=:client"),
                        {"tenant": str(tenant_id), "client": str(client_id)})
                connection.execute(text("""DELETE FROM client_duplicate_candidates
                    WHERE tenant_id=:tenant AND (client_id=:client OR other_client_id=:client)"""),
                    {"tenant": str(tenant_id), "client": str(client_id)})
                connection.execute(text("""DELETE FROM merge_records
                    WHERE tenant_id=:tenant AND (survivor_client_id=:client OR merged_client_id=:client)"""),
                    {"tenant": str(tenant_id), "client": str(client_id)})
                connection.execute(text("""UPDATE offer_campaigns
                    SET audience_snapshot=COALESCE((
                        SELECT jsonb_agg(item.value) FROM jsonb_array_elements_text(audience_snapshot) AS item(value)
                        WHERE item.value <> CAST(:client AS text)), '[]'::jsonb), version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND jsonb_exists(audience_snapshot, CAST(:client AS text))"""),
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
                connection.execute(text("""UPDATE privacy_requests
                    SET client_id=NULL, verification=NULL, subject_hash=:hash
                    WHERE tenant_id=:tenant AND client_id=:client"""),
                    {"tenant": str(tenant_id), "client": str(client_id), "hash": hashed})
                connection.execute(text("""UPDATE privacy_requests SET status='in_progress', client_id=NULL,
                    verification=NULL, subject_hash=:hash WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(payload.request_id), "hash": hashed})
                insert_audit(connection, tenant_id, identity, action="client.deleted",
                             resource_type="client", resource_id=client_id,
                             reason="client data deleted for a verified request",
                             request_id=request.state.request_id,
                             changed_fields=["deleted_at", "tombstone"])
            finish_file_purge(engine, tenant_id, payload.request_id)
            complete_deletion_request(engine, tenant_id, payload.request_id)
            return {"request_id": str(payload.request_id), "status": "completed",
                    "subject_hash": hashed, "replayed": False}
        except _RetryDeletionCleanup as retry:
            finish_file_purge(engine, tenant_id, payload.request_id)
            complete_deletion_request(engine, tenant_id, payload.request_id)
            return {"request_id": str(payload.request_id), "status": "completed",
                    "subject_hash": retry.subject_hash_value, "replayed": True}
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
                    text("SELECT 1 FROM clients WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
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

    @app.get("/v1/tenants/{tenant_id}/retention-holds", tags=["crm"])
    def list_holds(tenant_id: uuid.UUID, identity=Depends(require_identity),
                   client_id: uuid.UUID | None = None, limit: int = 20, offset: int = 0) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.privacy.manage")
                rows = connection.execute(text("""SELECT h.id::text AS id,
                    h.client_id::text AS client_id, c.name AS client_name, h.basis,
                    h.held_by_issuer, h.held_by_subject, h.created_at, h.released_at,
                    h.released_by_issuer, h.released_by_subject, h.release_reason
                    FROM retention_holds h JOIN clients c
                      ON c.tenant_id=h.tenant_id AND c.id=h.client_id
                    WHERE h.tenant_id=:tenant AND (CAST(:client AS uuid) IS NULL OR h.client_id=CAST(:client AS uuid))
                    ORDER BY h.created_at DESC LIMIT :limit OFFSET :offset"""), {
                    "tenant": str(tenant_id), "client": str(client_id) if client_id else None,
                    "limit": max(1, min(limit, 100)), "offset": max(0, offset)}).mappings().all()
            return {"items": [json_safe(dict(row)) for row in rows], "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/retention-holds/{hold_id}/release", tags=["crm"])
    def release_hold(tenant_id: uuid.UUID, hold_id: uuid.UUID, payload: HoldReleaseIn,
                     request: Request, identity=Depends(require_identity)) -> dict:
        if not payload.reason.strip():
            raise HTTPException(status_code=422, detail={"code": "release_reason_required"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.privacy.manage")
                row = connection.execute(text("""SELECT id, released_at FROM retention_holds
                    WHERE tenant_id=:tenant AND id=:id FOR UPDATE"""),
                    {"tenant": str(tenant_id), "id": str(hold_id)}).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={"code": "retention_hold_not_found"})
                if row["released_at"] is not None:
                    raise HTTPException(status_code=409, detail={"code": "retention_hold_already_released"})
                connection.execute(text("""UPDATE retention_holds SET released_at=now(),
                    released_by_issuer=:issuer, released_by_subject=:subject, release_reason=:reason
                    WHERE tenant_id=:tenant AND id=:id"""), {
                    "issuer": identity.issuer, "subject": identity.subject, "reason": payload.reason,
                    "tenant": str(tenant_id), "id": str(hold_id)})
                insert_audit(connection, tenant_id, identity, action="retention_hold.released",
                    resource_type="retention_hold", resource_id=hold_id,
                    reason=payload.reason, request_id=request.state.request_id,
                    changed_fields=["released_at", "release_reason"])
            return {"id": str(hold_id), "status": "released"}
        finally:
            engine.dispose()
