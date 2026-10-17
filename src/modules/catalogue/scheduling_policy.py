"""Shared staff eligibility checks for availability and booking commits.

Configuration mutations and booking commits take the same transaction-scoped advisory
lock for a staff member, so a commit cannot pass validation while a leave or
deactivation concurrently becomes effective.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import text


def lock_staff_schedule(connection, tenant_id: str, staff_id: str) -> None:
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:tenant), hashtext(:staff))"),
        {"tenant": str(tenant_id), "staff": "staff-schedule:" + str(staff_id)},
    )


def lock_service_resource_policy(connection, tenant_id: str, service_id: str, location_id: str) -> None:
    """Serialize requirement replacement with holds using that service/location policy."""
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:tenant), hashtext(:policy))"),
        {"tenant": str(tenant_id), "policy": f"resource-policy:{service_id}:{location_id}"},
    )


def lock_resource_schedule(connection, tenant_id: str, resource_id: str) -> None:
    """Serialize resource activation with a transaction that is reserving the resource."""
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:tenant), hashtext(:resource))"),
        {"tenant": str(tenant_id), "resource": "resource-schedule:" + str(resource_id)},
    )


def lock_closure_schedule(connection, tenant_id: str, location_id: str, work_day) -> None:
    """Serialize location closure publication with commits on that local date."""
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:tenant), hashtext(:closure))"),
        {"tenant": str(tenant_id),
         "closure": f"location-closure:{location_id}:{work_day.isoformat()}"},
    )


def required_resource_ids(connection, *, tenant_id: str, service_id: str,
                          location_id: str, service_requires_resource: bool) -> list[str]:
    """Return the active, location-matched resources under the shared booking guards."""
    tenant_id = str(tenant_id)
    service_id = str(service_id)
    location_id = str(location_id)
    lock_service_resource_policy(connection, tenant_id, service_id, location_id)
    required = connection.execute(
        text("""SELECT resource_id::text FROM service_resource_requirements
                WHERE tenant_id=:tenant AND service_id=:service AND location_id=:location
                ORDER BY resource_id"""),
        {"tenant": tenant_id, "service": service_id, "location": location_id},
    ).scalars().all()
    for resource_id in sorted(required):
        lock_resource_schedule(connection, tenant_id, resource_id)
    if service_requires_resource and not required:
        raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
    if required:
        active_ids = connection.execute(
            text("""SELECT id::text FROM bookable_resources
                    WHERE tenant_id=:tenant AND location_id=:location AND active
                      AND id=ANY(CAST(:ids AS uuid[])) ORDER BY id"""),
            {"tenant": tenant_id, "location": location_id, "ids": required},
        ).scalars().all()
        if set(active_ids) != set(required):
            raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
    return list(required)


def required_resource_units(connection, *, tenant_id: str, service_id: str,
                            location_id: str, service_requires_resource: bool,
                            occupied_from: datetime, occupied_to: datetime,
                            exclude_hold_id: str | None = None) -> list[tuple[str, int]]:
    """Allocate distinct units from every required pool under shared resource locks.

    The database exclusion on (tenant, resource, unit, range) remains the final
    concurrency guarantee. Advisory locks make the availability read and selected
    units deterministic when two bookings compete for a pool.
    """
    tenant, service, location = str(tenant_id), str(service_id), str(location_id)
    ids = required_resource_ids(
        connection, tenant_id=tenant, service_id=service, location_id=location,
        service_requires_resource=service_requires_resource)
    if not ids:
        return []
    policies = connection.execute(
        text("""SELECT q.resource_id::text AS resource_id, q.quantity, r.capacity
                FROM service_resource_requirements q
                JOIN bookable_resources r
                  ON r.tenant_id=q.tenant_id AND r.id=q.resource_id
                WHERE q.tenant_id=:tenant AND q.service_id=:service AND q.location_id=:location
                ORDER BY q.resource_id"""),
        {"tenant": tenant, "service": service, "location": location}).mappings().all()
    result: list[tuple[str, int]] = []
    for policy in policies:
        busy = connection.execute(
            text("""SELECT resource_unit FROM reservations
                    WHERE tenant_id=:tenant AND resource_id=:resource AND state='active'
                      AND occupied_range && tstzrange(:from_ts,:to_ts,'[)')
                      AND (CAST(:exclude_hold AS uuid) IS NULL
                           OR hold_id IS DISTINCT FROM CAST(:exclude_hold AS uuid))"""),
            {"tenant": tenant, "resource": policy["resource_id"],
             "from_ts": occupied_from.isoformat(), "to_ts": occupied_to.isoformat(),
             "exclude_hold": exclude_hold_id}).scalars().all()
        free = [unit for unit in range(1, policy["capacity"] + 1) if unit not in busy]
        if len(free) < policy["quantity"]:
            raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
        result.extend((policy["resource_id"], unit) for unit in free[:policy["quantity"]])
    return result


def plan_processing_phases(connection, *, tenant_id: str, service_id: str,
                           location_id: str, starts_at: datetime, preferred_staff_id: str,
                           service_duration: int, pre_buffer_minutes: int = 0,
                           post_buffer_minutes: int = 0,
                           exclude_hold_id: str | None = None) -> list[dict] | None:
    """Resolve configured attended phases to qualified, currently free staff.

    A missing phase configuration returns ``None`` so legacy services keep their
    established whole-duration single-staff reservation. Returned phases contain a
    stable staff assignment and UTC boundaries suitable for a reservation snapshot.
    """
    rows = connection.execute(
        text("""SELECT sequence,duration_minutes,mode,staff_count
                FROM service_processing_phases WHERE tenant_id=:tenant AND service_id=:service
                ORDER BY sequence"""),
        {"tenant": str(tenant_id), "service": str(service_id)}).mappings().all()
    if not rows:
        return None
    if sum(row["duration_minutes"] for row in rows) != service_duration:
        raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
    attended = [index for index, row in enumerate(rows) if row["mode"] == "attended"]
    first_attended, last_attended = attended[0], attended[-1]
    result = []
    offset = 0
    for index, phase in enumerate(rows):
        phase_start = starts_at + timedelta(minutes=offset)
        phase_end = phase_start + timedelta(minutes=phase["duration_minutes"])
        assigned: list[str] = []
        if phase["mode"] == "attended":
            candidates = connection.execute(
                text("""SELECT s.id::text FROM staff_profiles s
                        JOIN staff_locations sl ON sl.tenant_id=s.tenant_id AND sl.staff_id=s.id
                        JOIN staff_service_qualifications q
                          ON q.tenant_id=s.tenant_id AND q.staff_id=s.id AND q.service_id=:service
                        WHERE s.tenant_id=:tenant AND sl.location_id=:location AND s.is_bookable
                        ORDER BY CASE WHEN s.id=CAST(:preferred AS uuid) THEN 0 ELSE 1 END,
                                 s.display_name,s.id"""),
                {"tenant": str(tenant_id), "location": str(location_id),
                 "service": str(service_id), "preferred": str(preferred_staff_id)}).scalars().all()
            for candidate in candidates:
                if candidate in assigned:
                    continue
                pre = pre_buffer_minutes if index == first_attended else 0
                post = post_buffer_minutes if index == last_attended else 0
                try:
                    validate_staff_slot(
                        connection, tenant_id=tenant_id, staff_id=candidate,
                        service_id=service_id, location_id=location_id, starts_at=phase_start,
                        duration_minutes=phase["duration_minutes"], pre_buffer_minutes=pre,
                        post_buffer_minutes=post)
                except HTTPException:
                    continue
                occupied_from = phase_start - timedelta(minutes=pre)
                occupied_to = phase_end + timedelta(minutes=post)
                busy = connection.execute(
                    text("""SELECT 1 FROM reservations WHERE tenant_id=:tenant AND staff_id=:staff
                            AND state='active' AND occupied_range && tstzrange(:from_ts,:to_ts,'[)')
                            AND (CAST(:exclude_hold AS uuid) IS NULL
                                 OR hold_id IS DISTINCT FROM CAST(:exclude_hold AS uuid)) LIMIT 1"""),
                    {"tenant": str(tenant_id), "staff": candidate,
                     "from_ts": occupied_from.isoformat(), "to_ts": occupied_to.isoformat(),
                     "exclude_hold": exclude_hold_id}).scalar_one_or_none()
                if busy is None:
                    assigned.append(candidate)
                    if len(assigned) == phase["staff_count"]:
                        break
            if len(assigned) != phase["staff_count"]:
                raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
        first_pre = pre_buffer_minutes if index == first_attended else 0
        last_post = post_buffer_minutes if index == last_attended else 0
        result.append({"sequence": phase["sequence"], "duration_minutes": phase["duration_minutes"],
                       "staff_count": phase["staff_count"],
                       "mode": phase["mode"], "staff_ids": assigned,
                       "starts_at": phase_start.astimezone(timezone.utc).isoformat(),
                       "ends_at": phase_end.astimezone(timezone.utc).isoformat(),
                       "occupied_from": (phase_start-timedelta(minutes=first_pre)).astimezone(timezone.utc).isoformat(),
                       "occupied_to": (phase_end+timedelta(minutes=last_post)).astimezone(timezone.utc).isoformat()})
        offset += phase["duration_minutes"]
    return result


def insert_staff_reservations(connection, *, tenant_id: str, staff_id: str,
                              appointment_id: str, phases: list[dict] | None,
                              occupied_from: datetime, occupied_to: datetime) -> None:
    """Persist the planner's attended-phase reservations or legacy whole-service row."""
    assignments = ([(phase["sequence"], staff, phase["occupied_from"], phase["occupied_to"])
                    for phase in phases for staff in phase["staff_ids"]]
                   if phases is not None else
                   [(None, staff_id, occupied_from.isoformat(), occupied_to.isoformat())])
    for sequence, staff, starts, ends in assignments:
        connection.execute(text("""INSERT INTO reservations
            (tenant_id,id,appointment_id,staff_id,phase_sequence,occupied_range,state)
            VALUES (:tenant,:id,:appointment,:staff,:sequence,tstzrange(:starts,:ends,'[)'),'active')"""),
            {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
             "appointment": str(appointment_id), "staff": staff, "sequence": sequence,
             "starts": starts, "ends": ends})


def validate_staff_slot(
    connection,
    *,
    tenant_id: str,
    staff_id: str,
    service_id: str,
    location_id: str,
    starts_at: datetime,
    duration_minutes: int,
    pre_buffer_minutes: int = 0,
    post_buffer_minutes: int = 0,
) -> None:
    """Raise the stable booking conflict response unless this slot fits staff policy."""
    lock_staff_schedule(connection, tenant_id, staff_id)
    row = connection.execute(
        text("""SELECT s.is_bookable, s.deactivated_on, l.timezone
                FROM staff_profiles s
                JOIN staff_locations sl ON sl.tenant_id=s.tenant_id AND sl.staff_id=s.id
                JOIN locations l ON l.tenant_id=sl.tenant_id AND l.id=sl.location_id
                JOIN staff_service_qualifications q
                  ON q.tenant_id=s.tenant_id AND q.staff_id=s.id AND q.service_id=:service
                WHERE s.tenant_id=:tenant AND s.id=:staff AND l.id=:location"""),
        {"tenant": str(tenant_id), "staff": str(staff_id), "service": str(service_id),
         "location": str(location_id)},
    ).mappings().one_or_none()
    if row is None or not row["is_bookable"]:
        raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})

    zone = ZoneInfo(row["timezone"])
    service_start = starts_at.astimezone(zone)
    service_end = (starts_at.astimezone(timezone.utc) + timedelta(minutes=duration_minutes)).astimezone(zone)
    occupied_start = (starts_at.astimezone(timezone.utc)
                      - timedelta(minutes=pre_buffer_minutes)).astimezone(zone)
    occupied_end = (starts_at.astimezone(timezone.utc)
                    + timedelta(minutes=duration_minutes + post_buffer_minutes)).astimezone(zone)
    work_day = service_start.date()
    lock_closure_schedule(connection, tenant_id, location_id, work_day)
    exception = connection.execute(
        text("""SELECT kind, opens_at, closes_at FROM location_exceptions
                WHERE tenant_id=:tenant AND location_id=:location
                  AND exception_date=:day AND published_at IS NOT NULL"""),
        {"tenant": str(tenant_id), "location": str(location_id), "day": work_day},
    ).mappings().one_or_none()
    if exception is not None and exception["kind"] == "closed":
        raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
    if exception is not None and exception["kind"] == "custom":
        outside_hours = connection.execute(
            text("""SELECT CAST(:starts AS time) < opens_at
                           OR CAST(:ends AS time) > closes_at
                    FROM location_exceptions
                    WHERE tenant_id=:tenant AND location_id=:location
                      AND exception_date=:day AND published_at IS NOT NULL"""),
            {"starts": occupied_start.timetz().replace(tzinfo=None),
             "ends": occupied_end.timetz().replace(tzinfo=None),
             "tenant": str(tenant_id), "location": str(location_id), "day": work_day},
        ).scalar_one()
        if outside_hours:
            raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
    if exception is None:
        within_weekly_hours = connection.execute(
            text("""SELECT EXISTS (
                     SELECT 1 FROM location_hours
                     WHERE tenant_id=:tenant AND location_id=:location
                       AND weekday=:weekday AND superseded_at IS NULL
                       AND effective_from<=:day
                       AND (effective_to IS NULL OR effective_to>=:day)
                       AND CAST(:starts AS time)>=opens_at
                       AND CAST(:ends AS time)<=closes_at
                 )"""),
            {"tenant": str(tenant_id), "location": str(location_id),
             "weekday": work_day.weekday(), "day": work_day,
             "starts": occupied_start.timetz().replace(tzinfo=None),
             "ends": occupied_end.timetz().replace(tzinfo=None)},
        ).scalar_one()
        if not within_weekly_hours:
            raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
    if row["deactivated_on"] is not None and row["deactivated_on"] <= work_day:
        raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
    if service_end.date() != work_day or occupied_start.date() != work_day or occupied_end.date() != work_day:
        raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})

    shifts = connection.execute(
        text("""SELECT starts_at, ends_at, breaks FROM staff_shifts
                WHERE tenant_id=:tenant AND staff_id=:staff AND location_id=:location
                  AND weekday=:weekday AND effective_from<=:day
                  AND (effective_to IS NULL OR effective_to>=:day)
                ORDER BY starts_at"""),
        {"tenant": str(tenant_id), "staff": str(staff_id), "location": str(location_id),
         "weekday": work_day.weekday(), "day": work_day},
    ).mappings().all()
    on_leave = connection.execute(
        text("""SELECT 1 FROM staff_leave WHERE tenant_id=:tenant AND staff_id=:staff
                AND approved AND :day BETWEEN starts_on AND ends_on LIMIT 1"""),
        {"tenant": str(tenant_id), "staff": str(staff_id), "day": work_day},
    ).scalar_one_or_none()
    if not shifts or on_leave is not None:
        raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})

    def minutes(value) -> int:
        return value.hour * 60 + value.minute

    occupied_start_minute = minutes(occupied_start)
    occupied_end_minute = minutes(occupied_end)
    for shift in shifts:
        if minutes(shift["starts_at"]) > occupied_start_minute or minutes(shift["ends_at"]) < occupied_end_minute:
            continue
        overlaps_break = any(
            int(interval["start"][:2]) * 60 + int(interval["start"][3:]) < occupied_end_minute
            and occupied_start_minute < int(interval["end"][:2]) * 60 + int(interval["end"][3:])
            for interval in shift["breaks"] or []
        )
        if not overlaps_break:
            return
    raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})

