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

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, date

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

APPOINTMENT_STATUSES = ("pending_approval", "confirmed", "checked_in", "in_service",
                        "completed", "cancelled", "no_show", "rejected")
BOOKING_SOURCES = ("staff", "walk_in")


class ManualBookingIn(BaseModel):
    client_id: uuid.UUID
    staff_id: uuid.UUID
    service_id: uuid.UUID
    starts_at: str = Field(min_length=16, max_length=40)
    source: str = "staff"
    idempotency_key: str = Field(min_length=8, max_length=64)


def _parse_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "invalid_start_time"})
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail={"code": "start_time_requires_timezone"})
    return parsed


class CancelIn(BaseModel):
    expected_version: int
    reason: str | None = Field(default=None, max_length=240)
    idempotency_key: str = Field(min_length=8, max_length=64)


class RescheduleIn(BaseModel):
    starts_at: str = Field(min_length=16, max_length=40)
    expected_version: int
    idempotency_key: str = Field(min_length=8, max_length=64)


def register_calendar_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    @app.get("/v1/tenants/{tenant_id}/appointments", tags=["booking"])
    def list_appointments(tenant_id: uuid.UUID, request: Request,
                          identity=Depends(require_identity),
                          location_id: uuid.UUID | None = None,
                          staff_id: uuid.UUID | None = None,
                          date_from: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                          date_to: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
                          status: str | None = None,
                          source: str | None = None,
                          limit: int = 100,
                          offset: int = 0) -> dict:
        engine = app.state.engine_factory()
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail={"code": "invalid_limit"})
        if offset < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_offset"})
        if status is not None and status not in APPOINTMENT_STATUSES:
            raise HTTPException(status_code=422, detail={"code": "invalid_status"})
        if source is not None and source not in ("public_booking",) + BOOKING_SOURCES:
            raise HTTPException(status_code=422, detail={"code": "invalid_source"})
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.read")
                where = ["a.tenant_id=:tenant"]
                params = {"tenant": str(tenant_id)}
                if location_id is not None:
                    where.append("a.location_id=:location")
                    params["location"] = str(location_id)
                if staff_id is not None:
                    where.append("a.staff_id=:staff")
                    params["staff"] = str(staff_id)
                if date_from is not None:
                    where.append("a.starts_at >= :date_from")
                    params["date_from"] = f"{date_from}T00:00:00+00:00"
                if date_to is not None:
                    # Inclusive of the whole end day: compute the next-day boundary in
                    # Python rather than `::date + 1` in SQL, which psycopg's text()
                    # parameter parser does not accept inline.
                    end = date.fromisoformat(date_to) + timedelta(days=1)
                    where.append("a.starts_at < :date_to")
                    params["date_to"] = f"{end.isoformat()}T00:00:00+00:00"
                if status is not None:
                    where.append("a.status=:status")
                    params["status"] = status
                if source is not None:
                    where.append("a.source=:source")
                    params["source"] = source
                where_sql = " AND ".join(where)
                rows = connection.execute(
                    text(f"""SELECT a.id::text AS id, a.status, a.source, a.starts_at, a.ends_at,
                                    a.local_timezone, a.client_id::text AS client_id,
                                    a.staff_id::text AS staff_id, a.version,
                                    s.service_name, s.price_minor, s.currency
                             FROM appointments a
                             JOIN appointment_services s
                               ON s.tenant_id=a.tenant_id AND s.appointment_id=a.id AND s.sequence=1
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
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                # Idempotent replay: the same key returns the booking it already created.
                existing = connection.execute(
                    text("SELECT id::text AS id FROM appointments WHERE tenant_id=:tenant AND idempotency_key=:key"),
                    {"tenant": str(tenant_id), "key": payload.idempotency_key}).scalar_one_or_none()
                if existing is not None:
                    return {"appointment_id": existing, "replayed": True}
                service = connection.execute(
                    text("""SELECT id, name, duration_minutes, pre_buffer_minutes, post_buffer_minutes,
                                   price_minor, currency, version
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
                            WHERE l.tenant_id=:tenant AND sl.staff_id=:staff LIMIT 1"""),
                    {"tenant": str(tenant_id), "staff": str(payload.staff_id)}).mappings().one_or_none()
                if location is None:
                    raise HTTPException(status_code=409, detail={"code": "staff_has_no_location"})

                duration = service["duration_minutes"]
                pre = service["pre_buffer_minutes"]
                post = service["post_buffer_minutes"]
                ends_at = starts_at + timedelta(minutes=duration)
                occ_from = starts_at - timedelta(minutes=pre)
                occ_to = starts_at + timedelta(minutes=duration + post)
                try:
                    connection.execute(
                        text("""INSERT INTO appointments
                                (tenant_id,id,location_id,client_id,staff_id,starts_at,ends_at,local_timezone,
                                 status,source,occupied_range,policy_version,idempotency_key)
                                VALUES (:tenant,:id,:location,:client,:staff,:starts,:ends,:tz,
                                        'confirmed',:source,tstzrange(:occ_from,:occ_to,'[)'),1,:key)"""),
                        {"tenant": str(tenant_id), "id": str(appointment_id), "location": location["id"],
                         "client": str(payload.client_id), "staff": str(payload.staff_id),
                         "starts": starts_at.isoformat(), "ends": ends_at.isoformat(),
                         "tz": location["timezone"], "source": payload.source,
                         "occ_from": occ_from.isoformat(), "occ_to": occ_to.isoformat(),
                         "key": payload.idempotency_key})
                    connection.execute(
                        text("""INSERT INTO appointment_services
                                (tenant_id,id,appointment_id,sequence,service_id,service_version,service_name,
                                 staff_id,price_minor,currency,duration_minutes,pre_buffer_minutes,
                                 post_buffer_minutes,starts_at,ends_at)
                                VALUES (:tenant,:id,:appointment,1,:service,:version,:name,:staff,:price,
                                        :currency,:duration,:pre,:post,:starts,:ends)"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "appointment": str(appointment_id),
                         "service": str(payload.service_id), "version": service["version"],
                         "name": service["name"], "staff": str(payload.staff_id),
                         "price": service["price_minor"], "currency": service["currency"],
                         "duration": duration, "pre": pre, "post": post,
                         "starts": starts_at.isoformat(), "ends": ends_at.isoformat()})
                    connection.execute(
                        text("""INSERT INTO reservations
                                (tenant_id,id,appointment_id,staff_id,occupied_range,state)
                                VALUES (:tenant,:id,:appointment,:staff,tstzrange(:occ_from,:occ_to,'[)'),'active')"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "appointment": str(appointment_id),
                         "staff": str(payload.staff_id),
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
                except IntegrityError as exc:
                    # The EXCLUDE constraint refuses an overlapping slot, or a racing
                    # replay claimed the idempotency key. Either way the winner stands and
                    # the loser is told the slot is taken — never a 500.
                    if "idempotency" in str(exc.orig):
                        return {"appointment_id": existing or str(appointment_id), "replayed": True}
                    raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
            return {"appointment_id": str(appointment_id), "replayed": False}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/appointments/{appointment_id}/cancel", tags=["booking"])
    def cancel_appointment(tenant_id: uuid.UUID, appointment_id: uuid.UUID, payload: CancelIn,
                           request: Request, identity=Depends(require_identity)) -> dict:
        """Cancel a booking and release its reservation atomically (FR-033/FR-035).

        `expected_version` is required (api-contracts If-Match): a stale read cannot
        silently cancel a booking another operator just changed. A repeated cancel is a
        harmless replay. Releasing the reservation (state='released') frees the slot,
        because availability only counts active reservations.
        """
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                appt = connection.execute(
                    text("SELECT status, version FROM appointments WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                    {"tenant": str(tenant_id), "id": str(appointment_id)}).mappings().one_or_none()
                if appt is None:
                    raise HTTPException(status_code=404, detail={"code": "appointment_not_found"})
                if appt["version"] != payload.expected_version:
                    raise HTTPException(status_code=409, detail={"code": "appointment_version_conflict"})
                if appt["status"] == "cancelled":
                    # Idempotent: a repeated cancel of an already-cancelled booking is a
                    # no-op that returns the current state, not an error.
                    return {"appointment_id": str(appointment_id), "status": "cancelled", "replayed": True}
                connection.execute(
                    text("""UPDATE appointments
                            SET status='cancelled', cancellation_reason=:reason,
                                version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(appointment_id),
                     "reason": payload.reason})
                connection.execute(
                    text("UPDATE reservations SET state='released' WHERE tenant_id=:tenant AND appointment_id=:id AND state='active'"),
                    {"tenant": str(tenant_id), "id": str(appointment_id)})
                connection.execute(
                    text("""INSERT INTO appointment_transitions
                            (tenant_id,id,appointment_id,from_status,to_status,actor_issuer,actor_subject,reason)
                            VALUES (:tenant,:id,:appointment,:from,'cancelled',:issuer,:subject,:reason)"""),
                    {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "appointment": str(appointment_id),
                     "from": appt["status"], "issuer": identity.issuer, "subject": identity.subject,
                     "reason": payload.reason or "cancelled"})
                insert_audit(connection, tenant_id, identity, action="appointment.cancelled",
                             resource_type="appointment", resource_id=appointment_id,
                             reason=payload.reason or "cancelled", request_id=request.state.request_id,
                             changed_fields=["status"])
            return {"appointment_id": str(appointment_id), "status": "cancelled", "replayed": False}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/appointments/{appointment_id}/reschedule", tags=["booking"])
    def reschedule_appointment(tenant_id: uuid.UUID, appointment_id: uuid.UUID, payload: RescheduleIn,
                               request: Request, identity=Depends(require_identity)) -> dict:
        """Move a booking to a new start atomically (FR-035).

        The new occupied range is written onto the reservation inside one transaction; the
        EXCLUDE constraint refuses a slot already taken, and the whole transaction rolls
        back, so the original booking stands untouched (AC-2). Bumping `version` is the
        signal the reminder scheduler keys off to invalidate obsolete jobs (TASK-027).
        """
        engine = app.state.engine_factory()
        starts_at = _parse_instant(payload.starts_at)
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                appt = connection.execute(
                    text("""SELECT status, version, staff_id::text AS staff_id,
                                   local_timezone, occupied_range
                            FROM appointments WHERE tenant_id=:tenant AND id=:id FOR UPDATE"""),
                    {"tenant": str(tenant_id), "id": str(appointment_id)}).mappings().one_or_none()
                if appt is None:
                    raise HTTPException(status_code=404, detail={"code": "appointment_not_found"})
                if appt["version"] != payload.expected_version:
                    raise HTTPException(status_code=409, detail={"code": "appointment_version_conflict"})
                if appt["status"] in ("cancelled", "completed", "no_show", "rejected"):
                    raise HTTPException(status_code=409, detail={"code": "appointment_not_reschedulable"})
                svc = connection.execute(
                    text("""SELECT duration_minutes, pre_buffer_minutes, post_buffer_minutes
                            FROM appointment_services
                            WHERE tenant_id=:tenant AND appointment_id=:id AND sequence=1"""),
                    {"tenant": str(tenant_id), "id": str(appointment_id)}).mappings().one()
                duration = svc["duration_minutes"]
                pre = svc["pre_buffer_minutes"]
                post = svc["post_buffer_minutes"]
                ends_at = starts_at + timedelta(minutes=duration)
                occ_from = starts_at - timedelta(minutes=pre)
                occ_to = starts_at + timedelta(minutes=duration + post)
                try:
                    # Re-point the reservation to the new range in one step. If the new
                    # range collides, the EXCLUDE constraint raises and the transaction
                    # aborts, leaving the original row exactly as it was.
                    connection.execute(
                        text("""UPDATE reservations
                                SET occupied_range=tstzrange(:occ_from,:occ_to,'[)')
                                WHERE tenant_id=:tenant AND appointment_id=:id AND state='active'"""),
                        {"tenant": str(tenant_id), "id": str(appointment_id),
                         "occ_from": occ_from.isoformat(), "occ_to": occ_to.isoformat()})
                    connection.execute(
                        text("""UPDATE appointments
                                SET starts_at=:starts, ends_at=:ends, occupied_range=tstzrange(:occ_from,:occ_to,'[)'),
                                    version=version+1, updated_at=now()
                                WHERE tenant_id=:tenant AND id=:id"""),
                        {"tenant": str(tenant_id), "id": str(appointment_id),
                         "starts": starts_at.isoformat(), "ends": ends_at.isoformat(),
                         "occ_from": occ_from.isoformat(), "occ_to": occ_to.isoformat()})
                except IntegrityError as exc:
                    if "reservations_no_staff_overlap" in str(exc.orig) or "reservations_no_resource_overlap" in str(exc.orig):
                        raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
                    raise
                connection.execute(
                    text("""INSERT INTO appointment_transitions
                            (tenant_id,id,appointment_id,from_status,to_status,actor_issuer,actor_subject,reason)
                            VALUES (:tenant,:id,:appointment,:from,:to,:issuer,:subject,'rescheduled')"""),
                    {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "appointment": str(appointment_id),
                     "from": appt["status"], "to": appt["status"], "issuer": identity.issuer,
                     "subject": identity.subject})
                insert_audit(connection, tenant_id, identity, action="appointment.rescheduled",
                             resource_type="appointment", resource_id=appointment_id,
                             reason="rescheduled", request_id=request.state.request_id,
                             changed_fields=["starts_at", "version"])
            return {"appointment_id": str(appointment_id), "starts_at": starts_at.isoformat(),
                    "ends_at": ends_at.isoformat(), "replayed": False}
        finally:
            engine.dispose()



