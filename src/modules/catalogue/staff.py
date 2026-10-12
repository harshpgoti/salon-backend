"""Staff profiles, qualifications, shifts, leave and locations (FR-020, TASK-016).

Two invariants matter here and are enforced at the boundary:

- **A shift is a valid local interval.** Breaks must fall inside the shift and not overlap
  one another; a break outside the shift is rejected rather than silently ignored, because
  availability computed from a nonsense shift would be quietly wrong.
- **Deactivation is a date and a preview, not a delete.** `deactivate` reports the future
  appointments that the deactivation would affect, so an operator resolves them explicitly
  (FR-020). History and any still-future booking keep resolving the staff member.

Dependencies are injected for the same reason the sibling modules inject them.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import time

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text


class StaffIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    user_ref: uuid.UUID | None = None
    is_bookable: bool = True


class QualificationIn(BaseModel):
    service_ids: list[uuid.UUID] = Field(default_factory=list)


class StaffLocationsIn(BaseModel):
    location_ids: list[uuid.UUID] = Field(default_factory=list)


class BreakInterval(BaseModel):
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")


class ShiftIn(BaseModel):
    location_id: uuid.UUID
    weekday: int = Field(ge=0, le=6)
    starts_at: str = Field(pattern=r"^\d{2}:\d{2}$")
    ends_at: str = Field(pattern=r"^\d{2}:\d{2}$")
    breaks: list[BreakInterval] = Field(default_factory=list)
    effective_from: str | None = None
    effective_to: str | None = None


class LeaveIn(BaseModel):
    starts_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    ends_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    reason: str | None = Field(default=None, max_length=240)
    approved: bool = False


class DeactivateIn(BaseModel):
    deactivated_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


def _minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def validate_breaks(starts_at: str, ends_at: str, breaks: list[BreakInterval]) -> list[dict]:
    """Reject a break outside the shift, or breaks that overlap each other."""
    shift_start, shift_end = _minutes(starts_at), _minutes(ends_at)
    if shift_end <= shift_start:
        raise HTTPException(status_code=422, detail={"code": "shift_ends_before_start"})
    normalized = []
    for interval in breaks:
        start, end = _minutes(interval.start), _minutes(interval.end)
        if end <= start:
            raise HTTPException(status_code=422, detail={"code": "break_ends_before_start"})
        if start < shift_start or end > shift_end:
            raise HTTPException(status_code=422, detail={"code": "break_outside_shift"})
        normalized.append({"start": interval.start, "end": interval.end})
    ordered = sorted(normalized, key=lambda item: item["start"])
    for previous, current in zip(ordered, ordered[1:]):
        if _minutes(current["start"]) < _minutes(previous["end"]):
            raise HTTPException(status_code=422, detail={"code": "breaks_overlap"})
    return ordered


def _validate_leave_range(starts_on: str, ends_on: str) -> None:
    # The database enforces this too, but a constraint violation surfaces as a 500. A
    # reversed range is caller error and must be named as such.
    if ends_on < starts_on:
        raise HTTPException(status_code=422, detail={"code": "leave_ends_before_start"})


def register_staff_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    def staff_or_404(connection, tenant_id: uuid.UUID, staff_id: uuid.UUID):
        row = connection.execute(
            text("""SELECT * FROM staff_profiles WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": str(tenant_id), "id": str(staff_id)}).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "staff_not_found"})
        return row

    def serialize(row) -> dict:
        data = dict(row)
        for key in ("created_at", "updated_at"):
            if data.get(key) is not None:
                data[key] = data[key].isoformat()
        for key in ("id", "user_ref"):
            if data.get(key) is not None:
                data[key] = str(data[key])
        if data.get("deactivated_on") is not None:
            data["deactivated_on"] = data["deactivated_on"].isoformat()
        return data

    @app.post("/v1/tenants/{tenant_id}/staff", tags=["catalogue"], status_code=201)
    def create_staff(tenant_id: uuid.UUID, payload: StaffIn, request: Request,
                     identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        staff_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "staff.manage")
                connection.execute(
                    text("""INSERT INTO staff_profiles (tenant_id,id,display_name,user_ref,is_bookable)
                            VALUES (:tenant,:id,:name,:user,:bookable)"""),
                    {"tenant": str(tenant_id), "id": str(staff_id), "name": payload.display_name,
                     "user": str(payload.user_ref) if payload.user_ref else None,
                     "bookable": payload.is_bookable})
                insert_audit(connection, tenant_id, identity, action="staff.created",
                             resource_type="staff", resource_id=staff_id,
                             reason=f"staff profile {payload.display_name} created",
                             request_id=request.state.request_id, changed_fields=["display_name"])
                row = staff_or_404(connection, tenant_id, staff_id)
            return serialize(row)
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/staff", tags=["catalogue"])
    def list_staff(tenant_id: uuid.UUID, identity=Depends(require_identity),
                   include_deactivated: bool = False) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "staff.read")
                where = "tenant_id=:tenant" if include_deactivated else \
                    "tenant_id=:tenant AND (deactivated_on IS NULL OR deactivated_on > CURRENT_DATE)"
                rows = connection.execute(
                    text(f"""SELECT id::text AS id, display_name, user_ref::text AS user_ref,
                                    is_bookable, deactivated_on, version
                             FROM staff_profiles WHERE {where} ORDER BY display_name"""),
                    {"tenant": str(tenant_id)}).mappings().all()
            return {"items": [serialize(r) for r in rows]}
        finally:
            engine.dispose()

    @app.put("/v1/tenants/{tenant_id}/staff/{staff_id}/qualifications", tags=["catalogue"])
    def set_qualifications(tenant_id: uuid.UUID, staff_id: uuid.UUID, payload: QualificationIn,
                           request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "staff.manage")
                staff_or_404(connection, tenant_id, staff_id)
                # Every named service must belong to this tenant; a foreign id is a 404,
                # never a silently ignored line.
                for service_id in payload.service_ids:
                    owned = connection.execute(
                        text("SELECT 1 FROM services WHERE tenant_id=:tenant AND id=:id"),
                        {"tenant": str(tenant_id), "id": str(service_id)}).scalar_one_or_none()
                    if owned is None:
                        raise HTTPException(status_code=404, detail={"code": "service_not_found",
                                                                     "service_id": str(service_id)})
                connection.execute(
                    text("DELETE FROM staff_service_qualifications WHERE tenant_id=:tenant AND staff_id=:staff"),
                    {"tenant": str(tenant_id), "staff": str(staff_id)})
                for service_id in payload.service_ids:
                    connection.execute(
                        text("""INSERT INTO staff_service_qualifications (tenant_id,id,staff_id,service_id)
                                VALUES (:tenant,:id,:staff,:service)"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                         "staff": str(staff_id), "service": str(service_id)})
                insert_audit(connection, tenant_id, identity, action="staff.qualifications_set",
                             resource_type="staff", resource_id=staff_id,
                             reason="staff qualifications set", request_id=request.state.request_id,
                             changed_fields=["service_ids"])
            return {"staff_id": str(staff_id), "service_ids": [str(s) for s in payload.service_ids]}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/staff/{staff_id}/qualifications", tags=["catalogue"])
    def get_qualifications(tenant_id: uuid.UUID, staff_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "staff.read")
                staff_or_404(connection, tenant_id, staff_id)
                rows = connection.execute(
                    text("""SELECT service_id::text AS service_id FROM staff_service_qualifications
                            WHERE tenant_id=:tenant AND staff_id=:staff ORDER BY service_id"""),
                    {"tenant": str(tenant_id), "staff": str(staff_id)}).scalars().all()
            return {"staff_id": str(staff_id), "service_ids": list(rows)}
        finally:
            engine.dispose()

    @app.put("/v1/tenants/{tenant_id}/staff/{staff_id}/locations", tags=["catalogue"])
    def set_staff_locations(tenant_id: uuid.UUID, staff_id: uuid.UUID, payload: StaffLocationsIn,
                            request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "staff.manage")
                staff_or_404(connection, tenant_id, staff_id)
                for location_id in payload.location_ids:
                    owned = connection.execute(
                        text("SELECT 1 FROM locations WHERE tenant_id=:tenant AND id=:id"),
                        {"tenant": str(tenant_id), "id": str(location_id)}).scalar_one_or_none()
                    if owned is None:
                        raise HTTPException(status_code=404, detail={"code": "location_not_found"})
                connection.execute(
                    text("DELETE FROM staff_locations WHERE tenant_id=:tenant AND staff_id=:staff"),
                    {"tenant": str(tenant_id), "staff": str(staff_id)})
                for location_id in payload.location_ids:
                    connection.execute(
                        text("""INSERT INTO staff_locations (tenant_id,id,staff_id,location_id)
                                VALUES (:tenant,:id,:staff,:location)"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                         "staff": str(staff_id), "location": str(location_id)})
                insert_audit(connection, tenant_id, identity, action="staff.locations_set",
                             resource_type="staff", resource_id=staff_id,
                             reason="staff locations set", request_id=request.state.request_id,
                             changed_fields=["location_ids"])
            return {"staff_id": str(staff_id), "location_ids": [str(l) for l in payload.location_ids]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/staff/{staff_id}/shifts", tags=["catalogue"], status_code=201)
    def create_shift(tenant_id: uuid.UUID, staff_id: uuid.UUID, payload: ShiftIn, request: Request,
                     identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        shift_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "staff.manage")
                staff = staff_or_404(connection, tenant_id, staff_id)
                if not staff["is_bookable"]:
                    raise HTTPException(status_code=422, detail={"code": "staff_not_bookable"})
                # A shift at a location the staff member is not assigned to would create
                # availability nobody can fulfil, so it is refused here.
                assigned = connection.execute(
                    text("""SELECT 1 FROM staff_locations WHERE tenant_id=:tenant
                            AND staff_id=:staff AND location_id=:location"""),
                    {"tenant": str(tenant_id), "staff": str(staff_id),
                     "location": str(payload.location_id)}).scalar_one_or_none()
                if assigned is None:
                    raise HTTPException(status_code=422, detail={"code": "location_not_assigned"})
                breaks = validate_breaks(payload.starts_at, payload.ends_at, payload.breaks)
                connection.execute(
                    text("""INSERT INTO staff_shifts
                            (tenant_id,id,staff_id,location_id,weekday,starts_at,ends_at,breaks,
                             effective_from,effective_to)
                            VALUES (:tenant,:id,:staff,:location,:weekday,:starts,:ends,:breaks,
                                    COALESCE(:from_date, CURRENT_DATE), :to_date)"""),
                    {"tenant": str(tenant_id), "id": str(shift_id), "staff": str(staff_id),
                     "location": str(payload.location_id), "weekday": payload.weekday,
                     "starts": payload.starts_at, "ends": payload.ends_at,
                     "breaks": json.dumps(breaks), "from_date": payload.effective_from,
                     "to_date": payload.effective_to})
                insert_audit(connection, tenant_id, identity, action="staff_shift.created",
                             resource_type="staff_shift", resource_id=shift_id,
                             reason="staff shift created", request_id=request.state.request_id,
                             changed_fields=["weekday", "starts_at", "ends_at"])
            return {"id": str(shift_id), "staff_id": str(staff_id), "weekday": payload.weekday,
                    "starts_at": payload.starts_at, "ends_at": payload.ends_at, "breaks": breaks}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/staff/{staff_id}/shifts", tags=["catalogue"])
    def list_shifts(tenant_id: uuid.UUID, staff_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "staff.read")
                staff_or_404(connection, tenant_id, staff_id)
                rows = connection.execute(
                    text("""SELECT id::text AS id, location_id::text AS location_id, weekday,
                                   to_char(starts_at,'HH24:MI') AS starts_at,
                                   to_char(ends_at,'HH24:MI') AS ends_at, breaks,
                                   effective_from, effective_to, version
                            FROM staff_shifts WHERE tenant_id=:tenant AND staff_id=:staff
                            ORDER BY weekday, starts_at"""),
                    {"tenant": str(tenant_id), "staff": str(staff_id)}).mappings().all()
            return {"items": [serialize(r) for r in rows]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/staff/{staff_id}/leave", tags=["catalogue"], status_code=201)
    def create_leave(tenant_id: uuid.UUID, staff_id: uuid.UUID, payload: LeaveIn, request: Request,
                     identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        leave_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "staff.manage")
                staff_or_404(connection, tenant_id, staff_id)
                _validate_leave_range(payload.starts_on, payload.ends_on)
                connection.execute(
                    text("""INSERT INTO staff_leave
                            (tenant_id,id,staff_id,starts_on,ends_on,reason,approved)
                            VALUES (:tenant,:id,:staff,:starts,:ends,:reason,:approved)"""),
                    {"tenant": str(tenant_id), "id": str(leave_id), "staff": str(staff_id),
                     "starts": payload.starts_on, "ends": payload.ends_on,
                     "reason": payload.reason, "approved": payload.approved})
                insert_audit(connection, tenant_id, identity, action="staff_leave.created",
                             resource_type="staff_leave", resource_id=leave_id,
                             reason="staff leave recorded", request_id=request.state.request_id,
                             changed_fields=["starts_on", "ends_on", "approved"])
            return {"id": str(leave_id), "staff_id": str(staff_id), "starts_on": payload.starts_on,
                    "ends_on": payload.ends_on, "approved": payload.approved}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/staff/{staff_id}/leave", tags=["catalogue"])
    def list_leave(tenant_id: uuid.UUID, staff_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "staff.read")
                staff_or_404(connection, tenant_id, staff_id)
                rows = connection.execute(
                    text("""SELECT id::text AS id, starts_on, ends_on, reason, approved
                            FROM staff_leave WHERE tenant_id=:tenant AND staff_id=:staff
                            ORDER BY starts_on"""),
                    {"tenant": str(tenant_id), "staff": str(staff_id)}).mappings().all()
            return {"items": [serialize(r) for r in rows]}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/staff/{staff_id}/deactivation-preview", tags=["catalogue"])
    def deactivation_preview(tenant_id: uuid.UUID, staff_id: uuid.UUID, identity=Depends(require_identity),
                             on: str | None = None) -> dict:
        """Future appointments this staff member holds, so deactivation is an explicit choice."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "staff.read")
                staff_or_404(connection, tenant_id, staff_id)
                # appointments belongs to TASK-020; until it exists there is nothing to list,
                # and the response says so rather than inventing a result.
                has_appointments = connection.execute(
                    text("SELECT to_regclass('public.appointments') IS NOT NULL")).scalar_one()
                affected = []
                if has_appointments:
                    affected = connection.execute(
                        text("""SELECT id::text AS id, starts_at FROM appointments
                                WHERE tenant_id=:tenant AND staff_id=:staff
                                  AND starts_at >= now() AND status NOT IN ('cancelled','completed')
                                ORDER BY starts_at"""),
                        {"tenant": str(tenant_id), "staff": str(staff_id)}).mappings().all()
            return {"staff_id": str(staff_id), "on": on, "affected_appointments": [dict(a) for a in affected],
                    "requires_resolution": len(affected) > 0}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/staff/{staff_id}/deactivate", tags=["catalogue"])
    def deactivate_staff(tenant_id: uuid.UUID, staff_id: uuid.UUID, payload: DeactivateIn, request: Request,
                         identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "staff.manage")
                staff_or_404(connection, tenant_id, staff_id)
                has_appointments = connection.execute(
                    text("SELECT to_regclass('public.appointments') IS NOT NULL")).scalar_one()
                affected = 0
                if has_appointments:
                    affected = connection.execute(
                        text("""SELECT count(*) FROM appointments
                                WHERE tenant_id=:tenant AND staff_id=:staff
                                  AND starts_at >= :on AND status NOT IN ('cancelled','completed')"""),
                        {"tenant": str(tenant_id), "staff": str(staff_id),
                         "on": payload.deactivated_on}).scalar_one()
                # Deactivation is a date, not an immediate switch-off: a staff member deactivated from a
                # future date must keep being offered until that date, so `is_bookable` (the manual
                # toggle) is deliberately left alone here.
                updated = connection.execute(
                    text("""UPDATE staff_profiles SET deactivated_on=:on,
                            version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id RETURNING *"""),
                    {"on": payload.deactivated_on, "tenant": str(tenant_id),
                     "id": str(staff_id)}).mappings().one()
                insert_audit(connection, tenant_id, identity, action="staff.deactivated",
                             resource_type="staff", resource_id=staff_id,
                             reason=f"staff deactivated from {payload.deactivated_on}",
                             request_id=request.state.request_id,
                             changed_fields=["deactivated_on"])
            return {**serialize(updated), "affected_appointments": affected}
        finally:
            engine.dispose()