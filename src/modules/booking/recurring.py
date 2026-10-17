"""Tenant-scoped explicit recurring bookings (FR-037 / TASK-064).

Every occurrence is checked through the ordinary scheduling validator and is
committed with the same exclusion-backed reservations as a manual booking.
Wall-clock time is retained across DST changes; gaps and ambiguous wall times
are returned as per-occurrence conflicts rather than shifted silently.
"""
from __future__ import annotations

import hashlib
import json
import json
import re
import uuid
from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from modules.catalogue.scheduling_policy import (
    insert_staff_reservations, plan_processing_phases, required_resource_units,
    validate_staff_slot,
)
from modules.company import assert_bookings_allowed
from modules.communications.outbox import record_appointment_email_update


class SeriesInput(BaseModel):
    client_id: uuid.UUID
    staff_id: uuid.UUID
    service_id: uuid.UUID
    location_id: uuid.UUID
    frequency: str = "weekly"
    interval_weeks: int = Field(default=1, ge=1, le=12)
    local_time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    timezone: str | None = None
    start_date: date
    end_rule: str = "count"
    occurrence_count: int | None = Field(default=1, ge=1, le=52)
    until_date: date | None = None
    idempotency_key: str = Field(min_length=8, max_length=64)


def _local_start(day: date, local_time: str, zone: ZoneInfo) -> tuple[datetime | None, str | None]:
    wall = datetime.combine(day, time.fromisoformat(local_time))
    first = wall.replace(tzinfo=zone, fold=0)
    second = wall.replace(tzinfo=zone, fold=1)
    valid_first = first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == wall
    valid_second = second.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) == wall
    if not valid_first and not valid_second:
        return None, "dst_gap"
    if valid_first and valid_second and first.utcoffset() != second.utcoffset():
        return None, "dst_ambiguous"
    return (first if valid_first else second).astimezone(timezone.utc), None


def _occurrence_dates(payload: SeriesInput) -> list[date]:
    if payload.frequency != "weekly" or payload.end_rule not in {"count", "until_date"}:
        raise HTTPException(status_code=422, detail={"code": "invalid_series_rule"})
    if payload.end_rule == "count":
        if payload.occurrence_count is None:
            raise HTTPException(status_code=422, detail={"code": "occurrence_count_required"})
        result = [payload.start_date + timedelta(weeks=i * payload.interval_weeks)
                  for i in range(payload.occurrence_count)]
        if (result[-1] - result[0]).days > 365:
            raise HTTPException(status_code=422, detail={"code": "series_horizon_too_wide"})
        return result
    if payload.until_date is None or payload.until_date < payload.start_date:
        raise HTTPException(status_code=422, detail={"code": "invalid_until_date"})
    if (payload.until_date - payload.start_date).days > 365:
        raise HTTPException(status_code=422, detail={"code": "series_horizon_too_wide"})
    result, day = [], payload.start_date
    while day <= payload.until_date:
        result.append(day)
        if len(result) > 52:
            raise HTTPException(status_code=422, detail={"code": "series_occurrence_limit"})
        day += timedelta(weeks=payload.interval_weeks)
    return result


def register_recurring_routes(app: FastAPI, *, require_identity: Callable,
                              membership_scope: Callable,
                              require_permission: Callable[[str, str], None],
                              insert_audit: Callable) -> None:
    def authorize(connection, tenant_id: str, identity):
        role, _, assigned = membership_scope(connection, uuid.UUID(tenant_id), identity)
        require_permission(role, "appointments.manage")
        return role, assigned

    def load_context(connection, tenant_id: str, identity, payload: SeriesInput):
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        role, assigned = authorize(connection, tenant_id, identity)
        assert_bookings_allowed(connection, tenant_id=tenant_id)
        loc = connection.execute(text("""SELECT id::text AS id,timezone FROM locations
            WHERE tenant_id=:t AND id=:l"""), {"t": tenant_id, "l": str(payload.location_id)}).mappings().one_or_none()
        if loc is None or (role != "owner" and str(payload.location_id) not in {str(x) for x in assigned}):
            raise HTTPException(status_code=404, detail={"code": "location_not_found"})
        if payload.timezone and payload.timezone != loc["timezone"]:
            raise HTTPException(status_code=422, detail={"code": "timezone_mismatch"})
        client = connection.execute(text("SELECT 1 FROM clients WHERE tenant_id=:t AND id=:id AND deleted_at IS NULL"),
                                    {"t": tenant_id, "id": str(payload.client_id)}).scalar_one_or_none()
        if client is None:
            raise HTTPException(status_code=404, detail={"code": "client_not_found"})
        service = connection.execute(text("""SELECT id::text AS id,name,duration_minutes,pre_buffer_minutes,
            post_buffer_minutes,price_minor,currency,version,requires_resource FROM services
            WHERE tenant_id=:t AND id=:id AND archived_at IS NULL"""),
            {"t": tenant_id, "id": str(payload.service_id)}).mappings().one_or_none()
        if service is None:
            raise HTTPException(status_code=404, detail={"code": "service_not_found"})
        booking_policy_version = connection.execute(text("SELECT booking_policy_version FROM tenants WHERE id=:t"),
            {"t": tenant_id}).scalar_one()
        return dict(loc), dict(service), booking_policy_version

    def validate_occurrences(connection, tenant_id, payload, location, service):
        zone = ZoneInfo(location["timezone"])
        today = datetime.now(zone).date()
        if payload.start_date < today or payload.start_date > today + timedelta(days=365):
            raise HTTPException(status_code=422, detail={"code": "series_start_out_of_range"})
        results = []
        for index, day in enumerate(_occurrence_dates(payload)):
            starts, reason = _local_start(day, payload.local_time, zone)
            result = {"index": index, "local_date": day.isoformat(), "local_time": payload.local_time,
                      "timezone": location["timezone"], "valid": False, "reason": reason}
            if starts is not None:
                try:
                    phase_plan = plan_processing_phases(
                        connection, tenant_id=tenant_id, service_id=str(payload.service_id),
                        location_id=str(payload.location_id), starts_at=starts,
                        preferred_staff_id=str(payload.staff_id),
                        service_duration=service["duration_minutes"],
                        pre_buffer_minutes=service["pre_buffer_minutes"],
                        post_buffer_minutes=service["post_buffer_minutes"])
                    if phase_plan is None:
                        validate_staff_slot(connection, tenant_id=tenant_id, staff_id=str(payload.staff_id),
                            service_id=str(payload.service_id), location_id=str(payload.location_id), starts_at=starts,
                            duration_minutes=service["duration_minutes"],
                            pre_buffer_minutes=service["pre_buffer_minutes"],
                            post_buffer_minutes=service["post_buffer_minutes"])
                    occupied_from = starts - timedelta(minutes=service["pre_buffer_minutes"])
                    occupied_to = starts + timedelta(minutes=service["duration_minutes"] + service["post_buffer_minutes"])
                    resource_units = required_resource_units(
                        connection, tenant_id=tenant_id, service_id=str(payload.service_id),
                        location_id=str(payload.location_id),
                        service_requires_resource=bool(service["requires_resource"]),
                        occupied_from=occupied_from, occupied_to=occupied_to)
                    staff_conflict = connection.execute(text("""SELECT 1 FROM reservations
                        WHERE tenant_id=:t AND staff_id=:staff AND state='active'
                          AND occupied_range && tstzrange(:from,:to,'[)') LIMIT 1"""),
                        {"t": tenant_id, "staff": str(payload.staff_id), "from": occupied_from,
                         "to": occupied_to}).scalar_one_or_none() if phase_plan is None else None
                    if staff_conflict is not None:
                        raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
                    result.update(valid=True, reason=None, starts_at=starts.isoformat(),
                                  ends_at=(starts + timedelta(minutes=service["duration_minutes"])).isoformat(),
                                  processing_phases=phase_plan or [])
                except HTTPException as exc:
                    result["reason"] = exc.detail.get("code", "slot_unavailable") if isinstance(exc.detail, dict) else "slot_unavailable"
            results.append(result)
        return results

    def parse(payload: dict) -> SeriesInput:
        try:
            return SeriesInput.model_validate(payload)
        except (ValidationError, ValueError, TypeError) as exc:
            raise HTTPException(status_code=422, detail={"code": "invalid_series_input"}) from exc

    @app.post("/v1/tenants/{tenant_id}/recurring-series/preview", tags=["recurring"])
    def preview(tenant_id: uuid.UUID, body: dict, identity=Depends(require_identity)):
        tenant_id = str(tenant_id)
        payload = parse(body)
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                location, service, _ = load_context(connection, tenant_id, identity, payload)
                occurrences = validate_occurrences(connection, tenant_id, payload, location, service)
                return {"series_definition": payload.model_dump(mode="json", exclude={"idempotency_key", "timezone"}) |
                        {"timezone": location["timezone"]}, "occurrences": occurrences}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/recurring-series", status_code=201, tags=["recurring"])
    def create(tenant_id: uuid.UUID, body: dict, request: Request, identity=Depends(require_identity)):
        tenant_id = str(tenant_id)
        payload = parse(body)
        approved = body.get("approved_indices")
        if not isinstance(approved, list) or not approved or any(type(i) is not int for i in approved) or len(set(approved)) != len(approved):
            raise HTTPException(status_code=422, detail={"code": "invalid_approved_indices"})
        request_hash = hashlib.sha256(json.dumps(
            {"series": payload.model_dump(mode="json", exclude={"idempotency_key"}),
             "approved_indices": sorted(approved)}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                location, service, booking_policy_version = load_context(connection, tenant_id, identity, payload)
                connection.execute(text("SELECT pg_advisory_xact_lock(hashtext(:t),hashtext(:k))"),
                                   {"t": tenant_id, "k": "recurring:" + payload.idempotency_key})
                prior_row = connection.execute(text("SELECT id::text AS id,request_hash,version FROM recurring_series WHERE tenant_id=:t AND idempotency_key=:k"),
                    {"t": tenant_id, "k": payload.idempotency_key}).mappings().one_or_none()
                if prior_row:
                    if prior_row["request_hash"] != request_hash:
                        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
                    rows = connection.execute(text("SELECT id::text AS appointment_id,occurrence_index FROM appointments WHERE tenant_id=:t AND series_id=:s ORDER BY occurrence_index"),
                                              {"t": tenant_id, "s": prior_row["id"]}).mappings().all()
                    return {"series_id": prior_row["id"], "created_appointments": [dict(r) for r in rows],
                            "version": prior_row["version"], "replayed": True}
                occurrences = validate_occurrences(connection, tenant_id, payload, location, service)
                by_index = {item["index"]: item for item in occurrences}
                if any(i not in by_index for i in approved):
                    raise HTTPException(status_code=422, detail={"code": "approved_index_out_of_range"})
                invalid = [i for i in approved if not by_index[i]["valid"]]
                if invalid:
                    raise HTTPException(status_code=409, detail={"code": "approved_occurrence_unavailable", "indices": invalid})
                series_id = uuid.uuid4()
                dates = _occurrence_dates(payload)
                connection.execute(text("""INSERT INTO recurring_series
                    (tenant_id,id,idempotency_key,request_hash,client_id,staff_id,service_id,location_id,local_time,frequency,
                     interval_weeks,start_date,end_rule,occurrence_count,until_date,policy_duration_minutes,
                     policy_price_minor,policy_currency,policy_pre_buffer_minutes,policy_post_buffer_minutes,
                     policy_service_version,state,version)
                    VALUES (:t,:id,:key,:hash,:client,:staff,:service,:loc,:time,:frequency,:interval,:start,:rule,
                     :count,:until,:duration,:price,:currency,:pre,:post,:service_version,'active',1)"""),
                    {"t": tenant_id, "id": str(series_id), "key": payload.idempotency_key, "hash": request_hash,
                     "client": str(payload.client_id), "staff": str(payload.staff_id), "service": str(payload.service_id),
                     "loc": str(payload.location_id), "time": payload.local_time, "frequency": payload.frequency,
                     "interval": payload.interval_weeks, "start": payload.start_date,
                     "rule": payload.end_rule, "count": payload.occurrence_count, "until": payload.until_date,
                     "duration": service["duration_minutes"], "price": service["price_minor"],
                     "currency": service["currency"], "pre": service["pre_buffer_minutes"],
                     "post": service["post_buffer_minutes"], "service_version": service["version"]})
                created = []
                for index in approved:
                    occ = by_index[index]
                    starts = datetime.fromisoformat(occ["starts_at"])
                    ends = datetime.fromisoformat(occ["ends_at"])
                    occupied_from = starts - timedelta(minutes=service["pre_buffer_minutes"])
                    occupied_to = ends + timedelta(minutes=service["post_buffer_minutes"])
                    phase_plan = occ.get("processing_phases") or None
                    resource_units = required_resource_units(
                        connection, tenant_id=tenant_id, service_id=str(payload.service_id),
                        location_id=str(payload.location_id),
                        service_requires_resource=bool(service["requires_resource"]),
                        occupied_from=occupied_from, occupied_to=occupied_to)
                    appointment_id = uuid.uuid4()
                    key = hashlib.sha256(f"{payload.idempotency_key}:{index}".encode()).hexdigest()[:64]
                    connection.execute(text("""INSERT INTO appointments
                        (tenant_id,id,location_id,client_id,staff_id,starts_at,ends_at,local_timezone,status,source,
                         occupied_range,policy_version,idempotency_key,series_id,occurrence_index)
                        VALUES (:t,:id,:loc,:client,:staff,:starts,:ends,:tz,'confirmed','staff',
                         tstzrange(:from,:to,'[)'),:policy,:key,:series,:index)"""),
                        {"t": tenant_id, "id": str(appointment_id), "loc": str(payload.location_id),
                         "client": str(payload.client_id), "staff": str(payload.staff_id), "starts": starts,
                         "ends": ends, "tz": location["timezone"], "from": occupied_from, "to": occupied_to,
                         "policy": booking_policy_version, "key": key, "series": str(series_id), "index": index})
                    connection.execute(text("""INSERT INTO appointment_services
                        (tenant_id,id,appointment_id,sequence,service_id,service_version,service_name,staff_id,
                         price_minor,currency,duration_minutes,pre_buffer_minutes,post_buffer_minutes,starts_at,ends_at,
                         processing_phases)
                        VALUES (:t,:id,:appt,1,:service,:version,:name,:staff,:price,:currency,:duration,:pre,:post,:starts,:ends,
                                CAST(:phases AS jsonb))"""),
                        {"t": tenant_id, "id": str(uuid.uuid4()), "appt": str(appointment_id),
                         "service": str(payload.service_id), "version": service["version"], "name": service["name"],
                         "staff": str(payload.staff_id), "price": service["price_minor"], "currency": service["currency"],
                         "duration": service["duration_minutes"], "pre": service["pre_buffer_minutes"],
                         "post": service["post_buffer_minutes"], "starts": starts, "ends": ends,
                         "phases": json.dumps(phase_plan or [])})
                    insert_staff_reservations(
                        connection, tenant_id=tenant_id, staff_id=str(payload.staff_id),
                        appointment_id=str(appointment_id), phases=phase_plan,
                        occupied_from=occupied_from, occupied_to=occupied_to)
                    for resource_id, resource_unit in resource_units:
                        connection.execute(text("""INSERT INTO reservations
                            (tenant_id,id,appointment_id,resource_id,resource_unit,occupied_range,state)
                            VALUES (:t,:id,:appt,:resource,:unit,tstzrange(:from,:to,'[)'),'active')"""),
                            {"t": tenant_id, "id": str(uuid.uuid4()), "appt": str(appointment_id),
                         "resource": resource_id, "unit": resource_unit,
                         "from": occupied_from, "to": occupied_to})
                    connection.execute(text("""INSERT INTO appointment_transitions
                        (tenant_id,id,appointment_id,from_status,to_status,actor_issuer,actor_subject,reason)
                        VALUES (:t,:id,:appt,NULL,'confirmed',:issuer,:subject,'recurring series booking')"""),
                        {"t": tenant_id, "id": str(uuid.uuid4()), "appt": str(appointment_id),
                         "issuer": identity.issuer, "subject": identity.subject})
                    created.append({"appointment_id": str(appointment_id), "occurrence_index": index})
                insert_audit(connection, uuid.UUID(tenant_id), identity, action="recurring_series.created",
                    resource_type="recurring_series", resource_id=series_id, reason="approved occurrences booked",
                    request_id=request.state.request_id, changed_fields=["occurrences"])
                return {"series_id": str(series_id), "created_appointments": created, "version": 1, "replayed": False,
                        "skipped_indices": [item["index"] for item in occurrences if not item["valid"]]}
        except IntegrityError as exc:
            if "reservations_no_" in str(getattr(exc, "orig", exc)):
                raise HTTPException(status_code=409, detail={"code": "slot_unavailable"}) from exc
            raise
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/recurring-series/{series_id}/cancel", tags=["recurring"])
    def cancel(tenant_id: uuid.UUID, series_id: uuid.UUID, body: dict, request: Request, identity=Depends(require_identity)):
        tenant_id = str(tenant_id)
        mode = body.get("mode", "series")
        index = body.get("occurrence_index")
        expected = body.get("expected_version")
        if (mode not in {"occurrence", "from", "series"} or type(expected) is not int or expected < 1
                or (mode != "series" and (type(index) is not int or index < 0))):
            raise HTTPException(status_code=422, detail={"code": "invalid_cancel_scope"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                connection.execute(text("SELECT set_config('app.tenant_id',:t,true)"), {"t": tenant_id})
                role, assigned = authorize(connection, tenant_id, identity)
                series = connection.execute(text("SELECT version,state,location_id::text AS location_id FROM recurring_series WHERE tenant_id=:t AND id=:s FOR UPDATE"),
                    {"t": tenant_id, "s": str(series_id)}).mappings().one_or_none()
                if series is None:
                    raise HTTPException(status_code=404, detail={"code": "series_not_found"})
                if role != "owner" and series["location_id"] not in {str(value) for value in assigned}:
                    raise HTTPException(status_code=404, detail={"code": "series_not_found"})
                if expected != series["version"] or series["state"] != "active":
                    raise HTTPException(status_code=409, detail={"code": "series_version_conflict"})
                where = "tenant_id=:t AND series_id=:s AND status IN ('pending_approval','confirmed','checked_in')"
                params = {"t": tenant_id, "s": str(series_id)}
                if mode == "occurrence":
                    where += " AND occurrence_index=:i"; params["i"] = index
                elif mode == "from":
                    where += " AND occurrence_index>=:i"; params["i"] = index
                rows = connection.execute(text("SELECT id::text AS id,status FROM appointments WHERE " + where + " FOR UPDATE"), params).mappings().all()
                for row in rows:
                    connection.execute(text("UPDATE appointments SET status='cancelled',version=version+1,updated_at=now() WHERE tenant_id=:t AND id=:id"),
                                       {"t": tenant_id, "id": row["id"]})
                    connection.execute(text("UPDATE reservations SET state='released' WHERE tenant_id=:t AND appointment_id=:id AND state='active'"),
                                       {"t": tenant_id, "id": row["id"]})
                    connection.execute(text("""INSERT INTO appointment_transitions
                        (tenant_id,id,appointment_id,from_status,to_status,actor_issuer,actor_subject,reason)
                        VALUES (:t,:id,:appt,:old,'cancelled',:issuer,:subject,'recurring series cancellation')"""),
                        {"t": tenant_id, "id": str(uuid.uuid4()), "appt": row["id"], "old": row["status"],
                         "issuer": identity.issuer, "subject": identity.subject})
                    record_appointment_email_update(connection, tenant_id=tenant_id,
                        appointment_id=row["id"], purpose="booking_cancelled", trace_id=request.state.trace_id)
                state = "cancelled" if mode == "series" else series["state"]
                connection.execute(text("UPDATE recurring_series SET state=:state,version=version+1,updated_at=now() WHERE tenant_id=:t AND id=:s"),
                    {"state": state, "t": tenant_id, "s": str(series_id)})
                insert_audit(connection, uuid.UUID(tenant_id), identity, action="recurring_series.cancelled",
                    resource_type="recurring_series", resource_id=series_id,
                    reason="selected recurring occurrences cancelled", request_id=request.state.request_id,
                    changed_fields=["state", "appointments"])
                return {"series_id": str(series_id), "mode": mode, "cancelled_count": len(rows), "version": series["version"] + 1}
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/recurring-series/{series_id}", tags=["recurring"])
    def edit(tenant_id: uuid.UUID, series_id: uuid.UUID, body: dict, request: Request, identity=Depends(require_identity)):
        tenant_id = str(tenant_id)
        mode, index, expected, local_time = (body.get("mode"), body.get("occurrence_index"),
                                              body.get("expected_version"), body.get("local_time"))
        if (mode not in {"occurrence", "from"} or type(expected) is not int or expected < 1
                or type(index) is not int or index < 0
                or not isinstance(local_time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", local_time)):
            raise HTTPException(status_code=422, detail={"code": "invalid_edit_scope"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                connection.execute(text("SELECT set_config('app.tenant_id',:t,true)"), {"t": tenant_id})
                role, assigned = authorize(connection, tenant_id, identity)
                series = connection.execute(text("""SELECT s.*,l.timezone FROM recurring_series s
                    JOIN locations l ON l.tenant_id=s.tenant_id AND l.id=s.location_id
                    WHERE s.tenant_id=:t AND s.id=:s FOR UPDATE OF s"""),
                    {"t": tenant_id, "s": str(series_id)}).mappings().one_or_none()
                if series is None:
                    raise HTTPException(status_code=404, detail={"code": "series_not_found"})
                if role != "owner" and str(series["location_id"]) not in {str(value) for value in assigned}:
                    raise HTTPException(status_code=404, detail={"code": "series_not_found"})
                if series["version"] != expected or series["state"] != "active":
                    raise HTTPException(status_code=409, detail={"code": "series_version_conflict"})
                where = "tenant_id=:t AND series_id=:s AND starts_at>now() AND status IN ('pending_approval','confirmed','checked_in')"
                params = {"t": tenant_id, "s": str(series_id)}
                if mode == "occurrence":
                    where += " AND occurrence_index=:i"; params["i"] = index
                else:
                    where += " AND occurrence_index>=:i"; params["i"] = index
                rows = connection.execute(text("SELECT id::text AS id,staff_id::text AS staff_id,starts_at,ends_at,local_timezone,status,occurrence_index FROM appointments WHERE " + where + " ORDER BY occurrence_index FOR UPDATE"), params).mappings().all()
                if not rows:
                    raise HTTPException(status_code=404, detail={"code": "occurrence_not_found"})
                zone = ZoneInfo(series["timezone"])
                duration, pre, post = (series["policy_duration_minutes"], series["policy_pre_buffer_minutes"], series["policy_post_buffer_minutes"])
                service = {"requires_resource": False}
                # Resource requirements are resolved from the current service definition.
                service["requires_resource"] = bool(connection.execute(text("SELECT requires_resource FROM services WHERE tenant_id=:t AND id=:id"),
                    {"t": tenant_id, "id": str(series["service_id"])}).scalar_one())
                targets = []
                for row in rows:
                    local_day = row["starts_at"].astimezone(zone).date()
                    starts, reason = _local_start(local_day, local_time, zone)
                    if starts is None:
                        raise HTTPException(status_code=409, detail={"code": reason, "occurrence_index": row["occurrence_index"]})
                    targets.append((row, starts))
                for row, starts in targets:
                    connection.execute(text("UPDATE reservations SET state='released' WHERE tenant_id=:t AND appointment_id=:a AND state='active'"),
                        {"t": tenant_id, "a": row["id"]})
                    phase_plan = plan_processing_phases(
                        connection, tenant_id=tenant_id, service_id=str(series["service_id"]),
                        location_id=str(series["location_id"]), starts_at=starts,
                        preferred_staff_id=row["staff_id"], service_duration=duration,
                        pre_buffer_minutes=pre, post_buffer_minutes=post)
                    if phase_plan is None:
                        validate_staff_slot(connection, tenant_id=tenant_id, staff_id=row["staff_id"],
                            service_id=str(series["service_id"]), location_id=str(series["location_id"]),
                            starts_at=starts, duration_minutes=duration, pre_buffer_minutes=pre, post_buffer_minutes=post)
                    ends = starts + timedelta(minutes=duration)
                    occupied_from, occupied_to = starts - timedelta(minutes=pre), ends + timedelta(minutes=post)
                    resource_units = required_resource_units(
                        connection, tenant_id=tenant_id, service_id=str(series["service_id"]),
                        location_id=str(series["location_id"]),
                        service_requires_resource=service["requires_resource"],
                        occupied_from=occupied_from, occupied_to=occupied_to)
                    connection.execute(text("""UPDATE appointments SET starts_at=:starts,ends_at=:ends,
                        occupied_range=tstzrange(:from,:to,'[)'),version=version+1,updated_at=now()
                        WHERE tenant_id=:t AND id=:a"""),
                        {"starts": starts, "ends": ends, "from": occupied_from, "to": occupied_to,
                         "t": tenant_id, "a": row["id"]})
                    connection.execute(text("""UPDATE appointment_services SET starts_at=:starts,ends_at=:ends,
                        processing_phases=CAST(:phases AS jsonb) WHERE tenant_id=:t AND appointment_id=:a"""),
                        {"starts": starts, "ends": ends, "phases": json.dumps(phase_plan or []),
                         "t": tenant_id, "a": row["id"]})
                    insert_staff_reservations(connection, tenant_id=tenant_id, staff_id=row["staff_id"],
                        appointment_id=row["id"], phases=phase_plan,
                        occupied_from=occupied_from, occupied_to=occupied_to)
                    for resource_id, resource_unit in resource_units:
                        connection.execute(text("""INSERT INTO reservations
                            (tenant_id,id,appointment_id,resource_id,resource_unit,occupied_range,state)
                            VALUES (:t,:id,:a,:r,:unit,tstzrange(:from,:to,'[)'),'active')"""),
                            {"t": tenant_id, "id": str(uuid.uuid4()), "a": row["id"], "r": resource_id,
                             "unit": resource_unit,
                             "from": occupied_from, "to": occupied_to})
                    connection.execute(text("""INSERT INTO recurring_series_changes
                        (tenant_id,series_id,appointment_id,actor_issuer,actor_subject,previous_starts_at,next_starts_at)
                        VALUES (:t,:s,:a,:issuer,:subject,:old,:new)"""),
                        {"t": tenant_id, "s": str(series_id), "a": row["id"], "issuer": identity.issuer,
                         "subject": identity.subject, "old": row["starts_at"], "new": starts})
                    connection.execute(text("""INSERT INTO appointment_transitions
                        (tenant_id,id,appointment_id,from_status,to_status,actor_issuer,actor_subject,reason)
                        VALUES (:t,:id,:a,:status,:status,:issuer,:subject,'recurring series time changed')"""),
                        {"t": tenant_id, "id": str(uuid.uuid4()), "a": row["id"], "status": row["status"],
                         "issuer": identity.issuer, "subject": identity.subject})
                    record_appointment_email_update(connection, tenant_id=tenant_id,
                        appointment_id=row["id"], purpose="booking_rescheduled", trace_id=request.state.trace_id)
                if mode == "from":
                    connection.execute(text("UPDATE recurring_series SET local_time=:time,version=version+1,updated_at=now() WHERE tenant_id=:t AND id=:s"),
                        {"time": local_time, "t": tenant_id, "s": str(series_id)})
                else:
                    connection.execute(text("UPDATE recurring_series SET version=version+1,updated_at=now() WHERE tenant_id=:t AND id=:s"),
                        {"t": tenant_id, "s": str(series_id)})
                insert_audit(connection, uuid.UUID(tenant_id), identity, action="recurring_series.rescheduled",
                    resource_type="recurring_series", resource_id=series_id,
                    reason="selected recurring occurrences rescheduled", request_id=request.state.request_id,
                    changed_fields=["local_time", "occurrences"])
                return {"series_id": str(series_id), "updated_indices": [row["occurrence_index"] for row, _ in targets],
                        "version": expected + 1}
        except IntegrityError as exc:
            if "reservations_no_" in str(getattr(exc, "orig", exc)):
                raise HTTPException(status_code=409, detail={"code": "slot_unavailable"}) from exc
            raise
        finally:
            engine.dispose()
