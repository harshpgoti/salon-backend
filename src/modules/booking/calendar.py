"""Receptionist calendar and manual bookings (FR-030, FR-034; TASK-022).

Two authenticated, tenant-scoped endpoints on top of TASK-020's reservation engine:

- `GET /v1/tenants/{tenant}/appointments` — the day/week/list view. Every filter is
  allowlisted and bounded; the response carries `items/total/limit/offset` like the CRM
  search, so the UI pager is truthful. The read is RLS-scoped like every other.
- `POST /v1/tenants/{tenant}/appointments` — a manual or walk-in booking. It reuses the
  same `reservations` EXCLUDE constraint a public hold uses, so a receptionist cannot
  double-book a slot any more than a client can. A lost race is a 409 `slot_unavailable`,
  and the operator's other input is preserved, not silently merged.

A create has no prior version to conflict with; the operator-overwrite risk on a create
is two people booking one slot, which the exclusion constraint refuses. Editing an
existing booking's version (reschedule/transition) is TASK-023/024.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, date, time, timezone
from types import SimpleNamespace

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from modules.company import assert_bookings_allowed
from modules.communications.outbox import record_appointment_email_update
from modules.booking.holds import hash_token
from modules.catalogue.scheduling_policy import (
    lock_resource_schedule,
    insert_staff_reservations,
    plan_processing_phases,
    required_resource_units,
    validate_staff_slot,
)
from salon_platform.authorization import require_assigned_location

APPOINTMENT_STATUSES = ("pending_approval", "confirmed", "checked_in", "in_service",
                        "completed", "cancelled", "no_show", "rejected")
BOOKING_SOURCES = ("staff", "walk_in")


class ManualBookingIn(BaseModel):
    location_id: uuid.UUID | None = None
    client_id: uuid.UUID
    staff_id: uuid.UUID
    service_id: uuid.UUID
    starts_at: str = Field(min_length=16, max_length=40)
    source: str = "staff"
    idempotency_key: str = Field(min_length=8, max_length=64)
    # Tracked rebooking/offer token (FR-052); unknown tokens change nothing.
    attribution_token: str | None = Field(default=None, max_length=64)


def _parse_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "invalid_start_time"})
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail={"code": "start_time_requires_timezone"})
    return parsed.astimezone(timezone.utc)


def _command_receipt(connection, tenant_id, identity, key, action, facts):
    """Serialize one authorized request key and compare the original normalized facts."""
    fingerprint = hashlib.sha256(json.dumps(
        {"tenant": str(tenant_id), "actor": [identity.issuer, identity.subject],
         "action": action, "facts": facts}, sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()
    connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:tenant), hashtext(:key))"),
                       {"tenant": str(tenant_id), "key": "calendar-command:" + key})
    saved = connection.execute(text(
        "SELECT request_hash,response FROM calendar_commands WHERE tenant_id=:tenant AND idempotency_key=:key"),
        {"tenant": str(tenant_id), "key": key}).mappings().one_or_none()
    if saved is not None:
        if saved["request_hash"] != fingerprint:
            raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
        return fingerprint, {**saved["response"], "replayed": True}
    return fingerprint, None


def _save_receipt(connection, tenant_id, key, fingerprint, response):
    connection.execute(text("""INSERT INTO calendar_commands
        (tenant_id,idempotency_key,appointment_id,request_hash,response)
        VALUES (:tenant,:key,:appointment,:fingerprint,CAST(:response AS jsonb))"""),
        {"tenant": str(tenant_id), "key": key, "appointment": response["appointment_id"],
         "fingerprint": fingerprint, "response": json.dumps(response)})


class CancelIn(BaseModel):
    expected_version: int
    reason: str | None = Field(default=None, max_length=240)
    idempotency_key: str = Field(min_length=8, max_length=64)


class RescheduleIn(BaseModel):
    starts_at: str = Field(min_length=16, max_length=40)
    expected_version: int
    idempotency_key: str = Field(min_length=8, max_length=64)


def _public_management_actor(token: str):
    return SimpleNamespace(issuer="public-management", subject=hashlib.sha256(token.encode()).hexdigest())


def _require_public_booking_policy(connection, tenant_id: str, appointment) -> None:
    if appointment["status"] not in ("pending_approval", "confirmed") or appointment["starts_at"] <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail={"code": "appointment_not_modifiable"})
    policy = connection.execute(text(
        "SELECT settings,booking_policy_version FROM tenants WHERE id=:tenant FOR SHARE"),
        {"tenant": tenant_id}).mappings().one()
    if appointment["policy_version"] != policy["booking_policy_version"]:
        raise HTTPException(status_code=409, detail={"code": "booking_policy_changed"})
    if str((policy["settings"] or {}).get("cancellation_policy") or "").strip():
        # Free-text terms cannot be safely parsed into a cutoff. Let salon staff handle
        # custom terms; the public default is self-service until the appointment starts.
        raise HTTPException(status_code=409, detail={"code": "public_management_requires_salon"})


def _cancel_command(connection, tenant_id, appointment_id, payload, request, identity,
                    insert_audit, *, public=False):
    facts = {"appointment_id": str(appointment_id),
             **payload.model_dump(mode="json", exclude={"idempotency_key"})}
    fingerprint, replay = _command_receipt(
        connection, tenant_id, identity, payload.idempotency_key, "cancel", facts)
    if replay is not None:
        return replay
    appt = connection.execute(text(
        "SELECT status,version,starts_at,policy_version FROM appointments WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
        {"tenant": str(tenant_id), "id": str(appointment_id)}).mappings().one_or_none()
    if appt is None:
        raise HTTPException(status_code=404, detail={"code": "appointment_not_found"})
    if appt["version"] != payload.expected_version:
        raise HTTPException(status_code=409, detail={"code": "appointment_version_conflict"})
    if appt["status"] == "cancelled":
        response = {"appointment_id": str(appointment_id), "status": "cancelled", "replayed": True}
        _save_receipt(connection, tenant_id, payload.idempotency_key, fingerprint, response)
        return response
    if public:
        _require_public_booking_policy(connection, str(tenant_id), appt)
    elif appt["status"] not in ("pending_approval", "confirmed", "checked_in"):
        raise HTTPException(status_code=409, detail={"code": "appointment_not_cancellable"})
    reason = "client self-service cancellation" if public else (payload.reason or "").strip()
    if not public and not reason:
        raise HTTPException(status_code=422, detail={"code": "appointment_reason_required"})
    connection.execute(text("""UPDATE appointments SET status='cancelled', cancellation_reason=:reason,
        version=version+1, updated_at=now() WHERE tenant_id=:tenant AND id=:id"""),
        {"tenant": str(tenant_id), "id": str(appointment_id), "reason": reason})
    connection.execute(text("UPDATE reservations SET state='released' WHERE tenant_id=:tenant AND appointment_id=:id AND state='active'"),
        {"tenant": str(tenant_id), "id": str(appointment_id)})
    connection.execute(text("""INSERT INTO appointment_transitions
        (tenant_id,id,appointment_id,from_status,to_status,actor_issuer,actor_subject,reason)
        VALUES (:tenant,:id,:appointment,:from,'cancelled',:issuer,:subject,:reason)"""),
        {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "appointment": str(appointment_id),
         "from": appt["status"], "issuer": identity.issuer, "subject": identity.subject, "reason": reason})
    insert_audit(connection, tenant_id, identity, action="appointment.cancelled", resource_type="appointment",
        resource_id=appointment_id, reason=reason, request_id=request.state.request_id, changed_fields=["status"])
    record_appointment_email_update(
        connection, tenant_id=str(tenant_id), appointment_id=str(appointment_id),
        purpose="booking_cancelled", trace_id=request.state.trace_id)
    response = {"appointment_id": str(appointment_id), "status": "cancelled", "replayed": False}
    _save_receipt(connection, tenant_id, payload.idempotency_key, fingerprint, response)
    return response


def _reschedule_command(connection, tenant_id, appointment_id, payload, request, identity,
                        insert_audit, *, public=False):
    starts_at = _parse_instant(payload.starts_at)
    if public and starts_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail={"code": "appointment_not_modifiable"})
    facts = {"appointment_id": str(appointment_id),
             **payload.model_dump(mode="json", exclude={"idempotency_key"})}
    facts["starts_at"] = starts_at.isoformat()
    fingerprint, replay = _command_receipt(
        connection, tenant_id, identity, payload.idempotency_key, "reschedule", facts)
    if replay is not None:
        return replay
    appt = connection.execute(text("""SELECT status,version,staff_id::text AS staff_id,
        starts_at,ends_at,lower(occupied_range) AS occupied_from,upper(occupied_range) AS occupied_to,
        policy_version FROM appointments WHERE tenant_id=:tenant AND id=:id FOR UPDATE"""),
        {"tenant": str(tenant_id), "id": str(appointment_id)}).mappings().one_or_none()
    if appt is None:
        raise HTTPException(status_code=404, detail={"code": "appointment_not_found"})
    if appt["version"] != payload.expected_version:
        raise HTTPException(status_code=409, detail={"code": "appointment_version_conflict"})
    if public:
        _require_public_booking_policy(connection, str(tenant_id), appt)
    elif appt["status"] not in ("pending_approval", "confirmed", "checked_in"):
        raise HTTPException(status_code=409, detail={"code": "appointment_not_reschedulable"})
    snapshot = connection.execute(text("""SELECT service_id::text AS service_id,duration_minutes,
        pre_buffer_minutes,post_buffer_minutes FROM appointment_services
        WHERE tenant_id=:tenant AND appointment_id=:id ORDER BY sequence LIMIT 1"""),
        {"tenant": str(tenant_id), "id": str(appointment_id)}).mappings().one_or_none()
    if snapshot is None:
        raise HTTPException(status_code=409, detail={"code": "appointment_service_missing"})
    location_id = connection.execute(text(
        "SELECT location_id::text FROM appointments WHERE tenant_id=:tenant AND id=:id"),
        {"tenant": str(tenant_id), "id": str(appointment_id)}).scalar_one()
    validate_staff_slot(connection, tenant_id=str(tenant_id), staff_id=appt["staff_id"],
        service_id=snapshot["service_id"], location_id=location_id, starts_at=starts_at,
        duration_minutes=snapshot["duration_minutes"], pre_buffer_minutes=snapshot["pre_buffer_minutes"],
        post_buffer_minutes=snapshot["post_buffer_minutes"])
    resource_ids = connection.execute(text("""SELECT resource_id::text FROM reservations
        WHERE tenant_id=:tenant AND appointment_id=:appointment AND state='active'
          AND resource_id IS NOT NULL ORDER BY resource_id"""),
        {"tenant": str(tenant_id), "appointment": str(appointment_id)}).scalars().all()
    for resource_id in resource_ids:
        lock_resource_schedule(connection, str(tenant_id), resource_id)
    if resource_ids:
        active = connection.execute(text("""SELECT id::text FROM bookable_resources WHERE tenant_id=:tenant
            AND location_id=:location AND active AND id=ANY(CAST(:ids AS uuid[]))"""),
            {"tenant": str(tenant_id), "location": location_id, "ids": resource_ids}).scalars().all()
        if set(active) != set(resource_ids):
            raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
    delta = starts_at - appt["starts_at"].astimezone(timezone.utc)
    shift = f"{((delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds)} microseconds"
    ends_at = appt["ends_at"].astimezone(timezone.utc) + delta
    occupied_from = appt["occupied_from"].astimezone(timezone.utc) + delta
    occupied_to = appt["occupied_to"].astimezone(timezone.utc) + delta
    try:
        connection.execute(text("""UPDATE reservations SET occupied_range=tstzrange(
            lower(occupied_range)+CAST(:shift AS interval),upper(occupied_range)+CAST(:shift AS interval),'[)')
            WHERE tenant_id=:tenant AND appointment_id=:id AND state='active'"""),
            {"tenant": str(tenant_id), "id": str(appointment_id), "shift": shift})
        connection.execute(text("""UPDATE appointment_services SET starts_at=starts_at+CAST(:shift AS interval),
            ends_at=ends_at+CAST(:shift AS interval) WHERE tenant_id=:tenant AND appointment_id=:id"""),
            {"tenant": str(tenant_id), "id": str(appointment_id), "shift": shift})
        connection.execute(text("""UPDATE appointments SET starts_at=:starts,ends_at=:ends,
            occupied_range=tstzrange(:from,:to,'[)'),version=version+1,updated_at=now()
            WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": str(tenant_id), "id": str(appointment_id), "starts": starts_at.isoformat(),
             "ends": ends_at.isoformat(), "from": occupied_from.isoformat(), "to": occupied_to.isoformat()})
    except IntegrityError as exc:
        if "reservations_no_staff_overlap" in str(exc.orig) or "reservations_no_resource_overlap" in str(exc.orig):
            raise HTTPException(status_code=409, detail={"code": "slot_unavailable"}) from exc
        raise
    connection.execute(text("""INSERT INTO appointment_transitions
        (tenant_id,id,appointment_id,from_status,to_status,actor_issuer,actor_subject,reason)
        VALUES (:tenant,:id,:appointment,:from,:to,:issuer,:subject,'rescheduled')"""),
        {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "appointment": str(appointment_id),
         "from": appt["status"], "to": appt["status"], "issuer": identity.issuer, "subject": identity.subject})
    insert_audit(connection, tenant_id, identity, action="appointment.rescheduled", resource_type="appointment",
        resource_id=appointment_id, reason="rescheduled", request_id=request.state.request_id,
        changed_fields=["starts_at", "version"])
    record_appointment_email_update(
        connection, tenant_id=str(tenant_id), appointment_id=str(appointment_id),
        purpose="booking_rescheduled", trace_id=request.state.trace_id)
    response = {"appointment_id": str(appointment_id), "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(), "replayed": False}
    _save_receipt(connection, tenant_id, payload.idempotency_key, fingerprint, response)
    return response


def register_calendar_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
    resolve_public_location: Callable,
) -> None:
    def public_management_context(connection, slug, appointment_id, authorization):
        location = resolve_public_location(connection, slug)
        token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        row = connection.execute(text("""SELECT management_token_hash,management_token_expires_at
            FROM appointments WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": location["tenant_id"], "id": str(appointment_id)}).mappings().one_or_none()
        if (row is None or not row["management_token_hash"] or not token
                or not hmac.compare_digest(row["management_token_hash"], hash_token(token))):
            raise HTTPException(status_code=404, detail={"code": "appointment_not_found"})
        if row["management_token_expires_at"] <= datetime.now(timezone.utc):
            raise HTTPException(status_code=409, detail={"code": "management_link_expired"})
        return location["tenant_id"], _public_management_actor(token)

    @app.get("/v1/tenants/{tenant_id}/appointments", tags=["booking"])
    def list_appointments(tenant_id: uuid.UUID, request: Request,
                          identity=Depends(require_identity),
                          location_id: uuid.UUID | None = None,
                          staff_id: uuid.UUID | None = None,
                          date_from: date | None = None,
                          date_to: date | None = None,
                          status: str | None = None,
                          source: str | None = None,
                          limit: int = Query(default=100, ge=1, le=100),
                          offset: int = Query(default=0, ge=0, le=10000)) -> dict:
        engine = app.state.engine_factory()
        if status is not None and status not in APPOINTMENT_STATUSES:
            raise HTTPException(status_code=422, detail={"code": "invalid_status"})
        if source is not None and source not in ("public_booking",) + BOOKING_SOURCES:
            raise HTTPException(status_code=422, detail={"code": "invalid_source"})
        if date_from is not None and date_to is not None and date_to < date_from:
            raise HTTPException(status_code=422, detail={"code": "invalid_date_range"})
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.read")
                where = ["a.tenant_id=:tenant"]
                params = {"tenant": str(tenant_id)}
                if location_id is not None:
                    require_assigned_location(role, assigned, location_id)
                    where.append("a.location_id=:location")
                    params["location"] = str(location_id)
                elif role != "owner":
                    where.append("a.location_id=ANY(:locations)")
                    params["locations"] = assigned
                if staff_id is not None:
                    where.append("a.staff_id=:staff")
                    params["staff"] = str(staff_id)
                if date_from is not None:
                    # A calendar date belongs to the appointment's saved salon zone.
                    # Keep an indexed conservative UTC bound around the exact local
                    # predicate so all IANA offsets are covered without scanning the
                    # tenant's entire appointment history.
                    where.extend([
                        "a.starts_at >= :utc_from",
                        "(a.starts_at AT TIME ZONE a.local_timezone)::date >= :date_from",
                    ])
                    params["utc_from"] = (datetime.combine(date_from, time.min, timezone.utc)
                                          - timedelta(hours=14)).isoformat()
                    params["date_from"] = date_from
                if date_to is not None:
                    end = date_to + timedelta(days=1)
                    where.extend([
                        "a.starts_at < :utc_to",
                        "(a.starts_at AT TIME ZONE a.local_timezone)::date < :date_to_exclusive",
                    ])
                    params["utc_to"] = (datetime.combine(end, time.min, timezone.utc)
                                        + timedelta(hours=14)).isoformat()
                    params["date_to_exclusive"] = end
                if status is not None:
                    where.append("a.status=:status")
                    params["status"] = status
                if source is not None:
                    where.append("a.source=:source")
                    params["source"] = source
                where_sql = " AND ".join(where)
                rows = connection.execute(
                    text(f"""SELECT a.id::text AS id, a.status, a.source, a.starts_at, a.ends_at,
                                    a.local_timezone, a.location_id::text AS location_id,
                                    a.client_id::text AS client_id,
                                    a.staff_id::text AS staff_id, a.version,
                                    a.series_id::text AS series_id,a.occurrence_index,
                                    rs.version AS series_version,
                                    s.service_id::text AS service_id,s.service_name, s.price_minor, s.currency, s.service_version,
                                    s.duration_minutes, s.pre_buffer_minutes, s.post_buffer_minutes
                             FROM appointments a
                             JOIN appointment_services s
                               ON s.tenant_id=a.tenant_id AND s.appointment_id=a.id AND s.sequence=1
                             LEFT JOIN recurring_series rs
                               ON rs.tenant_id=a.tenant_id AND rs.id=a.series_id
                             WHERE {where_sql}
                             ORDER BY a.starts_at, a.id
                             LIMIT :limit OFFSET :offset"""),
                    {**params, "limit": limit, "offset": offset}).mappings().all()
                total = connection.execute(
                    text(f"SELECT count(*) FROM appointments a WHERE {where_sql}"),
                    params).scalar_one()
            items = [{**dict(r),
                      "starts_at": r["starts_at"].isoformat(),
                      "ends_at": r["ends_at"].isoformat()} for r in rows]
            return {"items": items, "total": total, "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/appointments", tags=["booking"], status_code=201)
    def create_manual_booking(tenant_id: uuid.UUID, payload: ManualBookingIn, request: Request,
                              identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        if payload.source not in BOOKING_SOURCES:
            raise HTTPException(status_code=422, detail={"code": "invalid_source"})
        starts_at = _parse_instant(payload.starts_at)
        appointment_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                location_id = payload.location_id
                if role != "owner" and location_id is None:
                    if len(assigned) != 1:
                        raise HTTPException(status_code=422, detail={"code": "location_required"})
                    location_id = assigned[0]
                if location_id is not None:
                    require_assigned_location(role, assigned, location_id)
                facts = payload.model_dump(mode="json", exclude={"idempotency_key"})
                facts["location_id"] = str(location_id) if location_id else None
                facts["starts_at"] = starts_at.isoformat()
                fingerprint, replay = _command_receipt(
                    connection, tenant_id, identity, payload.idempotency_key, "create", facts)
                if replay is not None:
                    return replay
                # FR-068: restricted/suspended salons take no new bookings.
                assert_bookings_allowed(connection, tenant_id=str(tenant_id))
                # Older bookings do not carry an original request fingerprint.
                existing = connection.execute(
                    text("SELECT id::text AS id FROM appointments WHERE tenant_id=:tenant AND idempotency_key=:key"),
                    {"tenant": str(tenant_id), "key": payload.idempotency_key}).scalar_one_or_none()
                if existing is not None:
                    raise HTTPException(status_code=409, detail={"code": "legacy_idempotency_key"})
                service = connection.execute(
                    text("""SELECT id, name, duration_minutes, pre_buffer_minutes, post_buffer_minutes,
                                   price_minor, currency, version, requires_resource
                            FROM services WHERE tenant_id=:tenant AND id=:id AND archived_at IS NULL"""),
                    {"tenant": str(tenant_id), "id": str(payload.service_id)}).mappings().one_or_none()
                if service is None:
                    raise HTTPException(status_code=404, detail={"code": "service_not_found"})
                staff_ok = connection.execute(
                    text("""SELECT 1 FROM staff_service_qualifications
                            WHERE tenant_id=:tenant AND staff_id=:staff AND service_id=:service"""),
                    {"tenant": str(tenant_id), "staff": str(payload.staff_id),
                     "service": str(payload.service_id)}).scalar_one_or_none()
                if staff_ok is None:
                    raise HTTPException(status_code=409, detail={"code": "staff_not_qualified"})
                client_ok = connection.execute(
                    text("SELECT 1 FROM clients WHERE tenant_id=:tenant AND id=:id AND deleted_at IS NULL"),
                    {"tenant": str(tenant_id), "id": str(payload.client_id)}).scalar_one_or_none()
                if client_ok is None:
                    raise HTTPException(status_code=404, detail={"code": "client_not_found"})
                location = connection.execute(
                    text("""SELECT l.id::text AS id, l.timezone FROM locations l
                            JOIN staff_locations sl ON sl.tenant_id=l.tenant_id AND sl.location_id=l.id
                            WHERE l.tenant_id=:tenant AND sl.staff_id=:staff"""
                         + (" AND l.id=:location" if location_id else "") + " ORDER BY l.id LIMIT 1"),
                    {"tenant": str(tenant_id), "staff": str(payload.staff_id),
                     "location": str(location_id) if location_id else None}).mappings().one_or_none()
                if location is None:
                    raise HTTPException(status_code=409, detail={"code": "staff_has_no_location"})

                duration = service["duration_minutes"]
                pre = service["pre_buffer_minutes"]
                post = service["post_buffer_minutes"]
                ends_at = starts_at + timedelta(minutes=duration)
                occ_from = starts_at - timedelta(minutes=pre)
                occ_to = starts_at + timedelta(minutes=duration + post)
                phase_plan = plan_processing_phases(
                    connection, tenant_id=str(tenant_id), service_id=str(payload.service_id),
                    location_id=location["id"], starts_at=starts_at,
                    preferred_staff_id=str(payload.staff_id), service_duration=duration,
                    pre_buffer_minutes=pre, post_buffer_minutes=post)
                if phase_plan is None:
                    validate_staff_slot(connection, tenant_id=str(tenant_id), staff_id=str(payload.staff_id),
                                        service_id=str(payload.service_id), location_id=location["id"],
                                        starts_at=starts_at, duration_minutes=duration,
                                        pre_buffer_minutes=pre, post_buffer_minutes=post)
                resource_units = required_resource_units(
                    connection, tenant_id=str(tenant_id), service_id=str(payload.service_id),
                    location_id=location["id"],
                    service_requires_resource=bool(service["requires_resource"]),
                    occupied_from=occ_from, occupied_to=occ_to)
                try:
                    connection.execute(
                        text("""INSERT INTO appointments
                                (tenant_id,id,location_id,client_id,staff_id,starts_at,ends_at,local_timezone,
                                 status,source,occupied_range,policy_version,idempotency_key,
                                 attribution_token)
                                VALUES (:tenant,:id,:location,:client,:staff,:starts,:ends,:tz,
                                        'confirmed',:source,tstzrange(:occ_from,:occ_to,'[)'),1,:key,
                                        :attribution)"""),
                        {"tenant": str(tenant_id), "id": str(appointment_id), "location": location["id"],
                         "client": str(payload.client_id), "staff": str(payload.staff_id),
                         "starts": starts_at.isoformat(), "ends": ends_at.isoformat(),
                         "tz": location["timezone"], "source": payload.source,
                         "occ_from": occ_from.isoformat(), "occ_to": occ_to.isoformat(),
                         "key": payload.idempotency_key,
                         "attribution": payload.attribution_token})
                    connection.execute(
                        text("""INSERT INTO appointment_services
                                (tenant_id,id,appointment_id,sequence,service_id,service_version,service_name,
                                 staff_id,price_minor,currency,duration_minutes,pre_buffer_minutes,
                                 post_buffer_minutes,starts_at,ends_at,processing_phases)
                                VALUES (:tenant,:id,:appointment,1,:service,:version,:name,:staff,:price,
                                        :currency,:duration,:pre,:post,:starts,:ends,CAST(:phases AS jsonb))"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "appointment": str(appointment_id),
                         "service": str(payload.service_id), "version": service["version"],
                         "name": service["name"], "staff": str(payload.staff_id),
                         "price": service["price_minor"], "currency": service["currency"],
                         "duration": duration, "pre": pre, "post": post,
                         "starts": starts_at.isoformat(), "ends": ends_at.isoformat(),
                         "phases": json.dumps(phase_plan or [])})
                    insert_staff_reservations(
                        connection, tenant_id=str(tenant_id), staff_id=str(payload.staff_id),
                        appointment_id=str(appointment_id), phases=phase_plan,
                        occupied_from=occ_from, occupied_to=occ_to)
                    for resource_id, resource_unit in resource_units:
                        connection.execute(
                            text("""INSERT INTO reservations
                                    (tenant_id,id,appointment_id,resource_id,resource_unit,occupied_range,state)
                                    VALUES (:tenant,:id,:appointment,:resource,:unit,
                                            tstzrange(:occ_from,:occ_to,'[)'),'active')"""),
                            {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                             "appointment": str(appointment_id), "resource": resource_id,
                             "unit": resource_unit,
                             "occ_from": occ_from.isoformat(), "occ_to": occ_to.isoformat()})
                    connection.execute(
                        text("""INSERT INTO appointment_transitions
                                (tenant_id,id,appointment_id,from_status,to_status,actor_issuer,actor_subject,reason)
                                VALUES (:tenant,:id,:appointment,NULL,'confirmed',:issuer,:subject,:reason)"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "appointment": str(appointment_id),
                         "issuer": identity.issuer, "subject": identity.subject,
                         "reason": f"manual booking ({payload.source})"})
                    insert_audit(connection, tenant_id, identity, action="appointment.created",
                                 resource_type="appointment", resource_id=appointment_id,
                                 reason=f"manual booking ({payload.source})",
                                 request_id=request.state.request_id,
                                 changed_fields=["status", "starts_at", "staff_id"])
                    # FR-052: a tracked prompt link closes its cycle atomically here.
                    from modules.retention import attribute_booking
                    attribute_booking(connection, tenant_id=str(tenant_id),
                                      attribution_token=payload.attribution_token,
                                      appointment_id=str(appointment_id))
                    # FR-056: the same token attributes a campaign send in-window.
                    from modules.attribution import attribute_campaign_booking
                    attribute_campaign_booking(connection, tenant_id=str(tenant_id),
                                               attribution_token=payload.attribution_token,
                                               appointment_id=str(appointment_id))
                    record_appointment_email_update(
                        connection, tenant_id=str(tenant_id), appointment_id=str(appointment_id),
                        purpose="booking_confirmation", trace_id=request.state.trace_id)
                except IntegrityError as exc:
                    raise HTTPException(status_code=409, detail={"code": "slot_unavailable"}) from exc
                response = {"appointment_id": str(appointment_id), "replayed": False}
                _save_receipt(connection, tenant_id, payload.idempotency_key, fingerprint, response)
            return response
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/appointments/{appointment_id}/cancel", tags=["booking"])
    def cancel_appointment(tenant_id: uuid.UUID, appointment_id: uuid.UUID, payload: CancelIn,
                          request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                target = connection.execute(text(
                    "SELECT location_id FROM appointments WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(appointment_id)}).scalar_one_or_none()
                if target is None:
                    raise HTTPException(status_code=404, detail={"code": "appointment_not_found"})
                require_assigned_location(role, assigned, target)
                return _cancel_command(connection, tenant_id, appointment_id, payload, request, identity, insert_audit)
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/appointments/{appointment_id}/reschedule", tags=["booking"])
    def reschedule_appointment(tenant_id: uuid.UUID, appointment_id: uuid.UUID, payload: RescheduleIn,
                               request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                target = connection.execute(text(
                    "SELECT location_id FROM appointments WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(appointment_id)}).scalar_one_or_none()
                if target is None:
                    raise HTTPException(status_code=404, detail={"code": "appointment_not_found"})
                require_assigned_location(role, assigned, target)
                return _reschedule_command(connection, tenant_id, appointment_id, payload, request, identity, insert_audit)
        finally:
            engine.dispose()

    @app.post("/v1/public/salons/{booking_slug}/manage/{appointment_id}/cancel", tags=["booking"])
    def public_cancel(booking_slug: str, appointment_id: uuid.UUID, payload: CancelIn, request: Request,
                      response: Response,
                      authorization: str | None = Header(default=None)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                tenant_id, actor = public_management_context(connection, booking_slug, appointment_id, authorization)
                return _cancel_command(connection, tenant_id, appointment_id, payload, request, actor,
                                       insert_audit, public=True)
        finally:
            engine.dispose()

    @app.post("/v1/public/salons/{booking_slug}/manage/{appointment_id}/reschedule", tags=["booking"])
    def public_reschedule(booking_slug: str, appointment_id: uuid.UUID, payload: RescheduleIn, request: Request,
                          response: Response,
                          authorization: str | None = Header(default=None)) -> dict:
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                tenant_id, actor = public_management_context(connection, booking_slug, appointment_id, authorization)
                return _reschedule_command(connection, tenant_id, appointment_id, payload, request, actor,
                                           insert_audit, public=True)
        finally:
            engine.dispose()

