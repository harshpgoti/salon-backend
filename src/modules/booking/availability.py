"""Public availability and booking policy (FR-023, FR-027, FR-028; TASK-019).

Availability is an intersection, computed once, from rules that live in other modules:

    hours  ∧  staff shifts  ∧  service qualification  ∧  resource availability
           ∧  approved leave  ∧  closures  −  existing reservations
           ∧  minimum notice  ∧  booking horizon

Two properties are deliberate:

- **A slot is only offered when every constraint holds.** A slot is generated from the
  location's hours, then narrowed by the staff member's shift *and* their approved leave,
  then filtered by the reservations that already exist. Nothing is offered "probably".
- **The public view exposes only slots and service/staff names** (FR-028). No client name,
  appointment id or calendar internals ever leaves this endpoint.

Timezone handling: rules are stored as local wall-clock intervals in the location's IANA
timezone. Each candidate local time is localized through `ZoneInfo`, so a DST gap collapses
to no slot and a DST repeat resolves to the first occurrence rather than a wrong instant
(qa-plan QA-09).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from salon_platform.local_time import clock_minutes, local_instant as _local_instant
from modules.catalogue.scheduling_policy import plan_processing_phases, required_resource_units

# Proposed booking policy defaults (FR-027/FR-029). They are stored per tenant policy later;
# until a policy editor exists these are the documented defaults, not invented behaviour.
DEFAULT_MIN_NOTICE_MINUTES = 120
DEFAULT_HORIZON_DAYS = 60
SLOT_STEP_MINUTES = 15


class AvailabilityQuery(BaseModel):
    service_id: uuid.UUID
    date_from: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_to: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    staff_id: uuid.UUID | None = None


def resolve_public_location(connection, booking_slug: str) -> dict:
    """Resolve a public booking slug to its location, then scope the connection.

    Module-level so the hold and booking modules share one definition of how a stranger
    reaches a tenant, rather than each re-implementing the slug resolution.
    """
    found = connection.execute(
        text("SELECT tenant_id::text AS tenant_id, location_id::text AS id "
             "FROM public_booking_slug_lookup(:slug)"),
        {"slug": booking_slug}).mappings().one_or_none()
    if found is None:
        raise HTTPException(status_code=404, detail={"code": "salon_not_found"})
    connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                       {"t": found["tenant_id"]})
    row = connection.execute(
        text("""SELECT id::text AS id, display_name, timezone, booking_slug
                FROM locations WHERE tenant_id=:tenant AND id=:id"""),
        {"tenant": found["tenant_id"], "id": found["id"]}).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "salon_not_found"})
    return {**dict(row), "tenant_id": found["tenant_id"]}


def register_availability_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
) -> None:
    def location_by_booking_slug(connection, booking_slug: str):
        return resolve_public_location(connection, booking_slug)

    def compute_slots(connection, tenant_id: str, location, service, staff_id, day_from, day_to):
        zone = ZoneInfo(location["timezone"])
        now = datetime.now(tz=zone)
        earliest = now.astimezone(timezone.utc) + timedelta(minutes=DEFAULT_MIN_NOTICE_MINUTES)
        latest = (now + timedelta(days=DEFAULT_HORIZON_DAYS)).astimezone(timezone.utc)

        # A required resource is conjunctive (all quantity-one requirements are needed).
        # Misconfigured or inactive resources fail closed and are never named publicly.
        if service["requires_resource"]:
            resource_state = connection.execute(
                text("""SELECT count(*) AS required_count,
                               count(*) FILTER (WHERE r.active) AS active_count
                        FROM service_resource_requirements q
                        LEFT JOIN bookable_resources r
                          ON r.tenant_id=q.tenant_id AND r.id=q.resource_id
                         AND r.location_id=q.location_id
                        WHERE q.tenant_id=:tenant AND q.service_id=:service
                          AND q.location_id=:location"""),
                {"tenant": tenant_id, "service": service["id"],
                 "location": location["id"]}).mappings().one()
            if resource_state["required_count"] == 0 or resource_state["active_count"] != resource_state["required_count"]:
                return []

        # Staff who may perform this service at this location.
        candidates = connection.execute(
            text("""SELECT s.id::text AS id, s.display_name, s.deactivated_on FROM staff_profiles s
                    JOIN staff_service_qualifications q
                      ON q.tenant_id=s.tenant_id AND q.staff_id=s.id AND q.service_id=:service
                    JOIN staff_locations sl
                      ON sl.tenant_id=s.tenant_id AND sl.staff_id=s.id AND sl.location_id=:location
                    WHERE s.tenant_id=:tenant AND s.is_bookable
                      AND (CAST(:staff AS uuid) IS NULL OR s.id = CAST(:staff AS uuid))
                    ORDER BY s.display_name"""),
            {"tenant": tenant_id, "service": service["id"], "location": location["id"],
             "staff": staff_id}).mappings().all()
        if not candidates:
            return []

        weekly = connection.execute(
            text("""SELECT weekday, to_char(opens_at,'HH24:MI') AS opens_at,
                           to_char(closes_at,'HH24:MI') AS closes_at, effective_from, effective_to
                    FROM location_hours WHERE tenant_id=:tenant AND location_id=:location
                      AND superseded_at IS NULL AND effective_from<=:to_date
                      AND (effective_to IS NULL OR effective_to>=:from_date)"""),
            {"tenant": tenant_id, "location": location["id"],
             "from_date": day_from, "to_date": day_to}).mappings().all()
        hours_by_day: dict[int, list[dict]] = {}
        for row in weekly:
            hours_by_day.setdefault(row["weekday"], []).append(dict(row))

        exceptions = {}
        for row in connection.execute(
                text("""SELECT exception_date, kind, to_char(opens_at,'HH24:MI') AS opens_at,
                               to_char(closes_at,'HH24:MI') AS closes_at
                        FROM location_exceptions WHERE tenant_id=:tenant AND location_id=:location
                          AND published_at IS NOT NULL
                          AND exception_date BETWEEN :from_date AND :to_date"""),
                {"tenant": tenant_id, "location": location["id"],
                 "from_date": day_from, "to_date": day_to}).mappings().all():
            exceptions[row["exception_date"]] = row

        duration = service["duration_minutes"]
        pre = service["pre_buffer_minutes"]
        post = service["post_buffer_minutes"]
        has_processing_phases = connection.execute(
            text("SELECT EXISTS (SELECT 1 FROM service_processing_phases WHERE tenant_id=:tenant AND service_id=:service)"),
            {"tenant": tenant_id, "service": service["id"]}).scalar_one()
        slots = []
        seen = set()
        day = day_from
        while day <= day_to:
            # A published closure or custom-hours exception overrides the weekly pattern.
            exception = exceptions.get(day)
            if exception is not None:
                if exception["kind"] == "closed":
                    day += timedelta(days=1)
                    continue
                windows = [(_minutes(exception["opens_at"]), _minutes(exception["closes_at"]))]
            else:
                windows = [(_minutes(row["opens_at"]), _minutes(row["closes_at"]))
                           for row in hours_by_day.get(day.weekday(), [])
                           if row["effective_from"] <= day
                           and (row["effective_to"] is None or row["effective_to"] >= day)]

            if has_processing_phases:
                for window_start, window_end in windows:
                    last_start = window_end - duration - post
                    for service_start in range(window_start + pre, last_start + 1, SLOT_STEP_MINUTES):
                        starts_at = _local_instant(day, service_start, zone)
                        if starts_at is None:
                            continue
                        start_utc = starts_at.astimezone(timezone.utc)
                        end_utc = start_utc + timedelta(minutes=duration)
                        occupied_from, occupied_to = start_utc - timedelta(minutes=pre), end_utc + timedelta(minutes=post)
                        if occupied_from < earliest or start_utc > latest or end_utc.astimezone(zone).date() != day:
                            continue
                        for preferred in candidates:
                            if preferred["deactivated_on"] is not None and preferred["deactivated_on"] <= day:
                                continue
                            try:
                                plan_processing_phases(
                                    connection, tenant_id=tenant_id, service_id=service["id"],
                                    location_id=location["id"], starts_at=start_utc,
                                    preferred_staff_id=preferred["id"], service_duration=duration,
                                    pre_buffer_minutes=pre, post_buffer_minutes=post)
                                required_resource_units(
                                    connection, tenant_id=tenant_id, service_id=service["id"],
                                    location_id=location["id"],
                                    service_requires_resource=bool(service["requires_resource"]),
                                    occupied_from=occupied_from, occupied_to=occupied_to)
                            except HTTPException:
                                continue
                            key = (preferred["id"], start_utc)
                            if key in seen:
                                continue
                            seen.add(key)
                            slots.append({"starts_at": starts_at.isoformat(), "ends_at": end_utc.astimezone(zone).isoformat(),
                                "staff_id": preferred["id"], "staff_name": preferred["display_name"],
                                "local_date": day.isoformat(),
                                "local_time": f"{service_start // 60:02d}:{service_start % 60:02d}",
                                "timezone": location["timezone"]})
                day += timedelta(days=1)
                continue

            for staff in candidates:
                if staff["deactivated_on"] is not None and staff["deactivated_on"] <= day:
                    continue
                shifts = connection.execute(
                    text("""SELECT to_char(starts_at,'HH24:MI') AS starts_at,
                                   to_char(ends_at,'HH24:MI') AS ends_at, breaks, effective_from, effective_to
                            FROM staff_shifts WHERE tenant_id=:tenant AND staff_id=:staff
                              AND location_id=:location
                              AND weekday=:weekday
                              AND effective_from <= :day
                              AND (effective_to IS NULL OR effective_to >= :day)"""),
                    {"tenant": tenant_id, "staff": staff["id"], "location": location["id"], "weekday": day.weekday(),
                     "day": day}).mappings().all()
                on_leave = connection.execute(
                    text("""SELECT 1 FROM staff_leave WHERE tenant_id=:tenant AND staff_id=:staff
                            AND approved AND :day BETWEEN starts_on AND ends_on LIMIT 1"""),
                    {"tenant": tenant_id, "staff": staff["id"], "day": day}).scalar_one_or_none()
                if on_leave is not None:
                    continue

                for window_start, window_end in windows:
                    for shift in shifts:
                        shift_start = max(window_start, _minutes(shift["starts_at"]))
                        shift_end = min(window_end, _minutes(shift["ends_at"]))
                        opens = _local_instant(day, shift_start, zone)
                        closes = _local_instant(day, shift_end, zone)
                        if opens is None or closes is None:
                            continue
                        opens, closes = opens.astimezone(timezone.utc), closes.astimezone(timezone.utc)
                        # A boundary in a DST gap cannot describe a safe working interval.
                        occupied = []
                        invalid_break = False
                        for interval in shift["breaks"] or []:
                            try:
                                begins = _local_instant(day, clock_minutes(interval["start"]), zone)
                                ends = _local_instant(
                                    day, clock_minutes(interval["end"], allow_end_of_day=True), zone)
                            except ValueError:
                                begins = ends = None
                            if begins is None or ends is None:
                                invalid_break = True
                                break
                            occupied.append((begins.astimezone(timezone.utc), ends.astimezone(timezone.utc)))
                        if invalid_break:
                            continue
                        for service_start in range(shift_start + pre, shift_end, SLOT_STEP_MINUTES):
                            starts_at = _local_instant(day, service_start, zone)
                            if starts_at is None:
                                continue
                            start_utc = starts_at.astimezone(timezone.utc)
                            end_utc = start_utc + timedelta(minutes=duration)
                            occupies_from = start_utc - timedelta(minutes=pre)
                            occupies_to = end_utc + timedelta(minutes=post)
                            if occupies_from < opens or occupies_to > closes:
                                continue
                            if any(occupies_from < end and begin < occupies_to for begin, end in occupied):
                                continue
                            if start_utc < earliest or start_utc > latest:
                                continue
                            key = (staff["id"], start_utc)
                            if key in seen:
                                continue
                            ends_at = end_utc.astimezone(zone)
                            clash = connection.execute(
                                text("""SELECT 1 FROM reservations
                                        WHERE tenant_id=:tenant AND state='active' AND staff_id=:staff
                                          AND occupied_range && tstzrange(:from_ts,:to_ts,'[)') LIMIT 1"""),
                                {"tenant": tenant_id, "staff": staff["id"],
                                 "from_ts": occupies_from.isoformat(), "to_ts": occupies_to.isoformat()}
                            ).scalar_one_or_none()
                            if clash is not None:
                                continue
                            try:
                                required_resource_units(
                                    connection, tenant_id=tenant_id, service_id=service["id"],
                                    location_id=location["id"],
                                    service_requires_resource=bool(service["requires_resource"]),
                                    occupied_from=occupies_from, occupied_to=occupies_to)
                            except HTTPException:
                                continue
                            seen.add(key)
                            slots.append({
                                "starts_at": starts_at.isoformat(),
                                "ends_at": ends_at.isoformat(),
                                "staff_id": staff["id"],
                                "staff_name": staff["display_name"],
                                "local_date": day.isoformat(),
                                "local_time": f"{service_start // 60:02d}:{service_start % 60:02d}",
                                "timezone": location["timezone"],
                            })
            day += timedelta(days=1)
        return slots

    def _minutes(value: str) -> int:
        return clock_minutes(value, allow_end_of_day=True)

    @app.get("/v1/public/salons/{booking_slug}/availability", tags=["booking"])
    def public_availability(booking_slug: str, service_id: uuid.UUID, date_from: str, date_to: str,
                            staff_id: uuid.UUID | None = None) -> dict:
        """Unauthenticated availability for the public booking page (FR-027, FR-028)."""
        engine = app.state.engine_factory()
        try:
            if date_to < date_from:
                raise HTTPException(status_code=422, detail={"code": "date_range_inverted"})
            try:
                day_from = date.fromisoformat(date_from)
                day_to = date.fromisoformat(date_to)
            except ValueError:
                raise HTTPException(status_code=422, detail={"code": "invalid_date"})
            if (day_to - day_from).days > 31:
                raise HTTPException(status_code=422, detail={"code": "date_range_too_wide"})
            with engine.begin() as connection:
                location = location_by_booking_slug(connection, booking_slug)
                connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                                   {"t": location["tenant_id"]})
                service = connection.execute(
                    text("""SELECT id::text AS id, name, duration_minutes, pre_buffer_minutes,
                                   post_buffer_minutes, price_minor, currency, requires_resource
                            FROM services WHERE tenant_id=:tenant AND id=:id
                              AND archived_at IS NULL"""),
                    {"tenant": location["tenant_id"], "id": str(service_id)}).mappings().one_or_none()
                if service is None:
                    # FR-028: a generic externally-visible error, never an internal reason.
                    raise HTTPException(status_code=404, detail={"code": "availability_not_found"})
                slots = compute_slots(connection, location["tenant_id"], location, service,
                                      staff_id, day_from, day_to)
            # Only slot data and public staff names leave this endpoint (FR-028).
            return {"salon": location["display_name"], "timezone": location["timezone"],
                    "service": {"id": service["id"], "name": service["name"],
                                "duration_minutes": service["duration_minutes"],
                                "price_minor": service["price_minor"], "currency": service["currency"]},
                    "slots": slots}
        finally:
            engine.dispose()

    @app.get("/v1/public/salons/{booking_slug}/booking-profile", tags=["booking"])
    def public_salon_profile(booking_slug: str) -> dict:
        """Public hours and bookable services for the booking page (FR-022, FR-028).

        `/v1/public/salons/{slug}` (TASK-008) owns branding and setup state; this endpoint
        adds the booking-facing hours and service list without duplicating or shadowing it.
        """
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                location = location_by_booking_slug(connection, booking_slug)
                connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                                   {"t": location["tenant_id"]})
                weekly = connection.execute(
                    text("""SELECT weekday, to_char(opens_at,'HH24:MI') AS opens_at,
                                   to_char(closes_at,'HH24:MI') AS closes_at
                            FROM location_hours WHERE tenant_id=:tenant AND location_id=:location
                              AND superseded_at IS NULL ORDER BY weekday, opens_at"""),
                    {"tenant": location["tenant_id"], "location": location["id"]}).mappings().all()
                upcoming = connection.execute(
                    text("""SELECT exception_date, kind, to_char(opens_at,'HH24:MI') AS opens_at,
                                   to_char(closes_at,'HH24:MI') AS closes_at, label
                            FROM location_exceptions WHERE tenant_id=:tenant AND location_id=:location
                              AND published_at IS NOT NULL AND exception_date >= CURRENT_DATE
                            ORDER BY exception_date"""),
                    {"tenant": location["tenant_id"], "location": location["id"]}).mappings().all()
                services = connection.execute(
                            text("""SELECT s.id::text AS id, s.name, s.duration_minutes,
                                         s.price_minor, s.currency
                                    FROM services s
                                   WHERE s.tenant_id=:tenant AND s.archived_at IS NULL
                                     AND (NOT s.requires_resource OR (
                                          (SELECT count(*) FROM service_resource_requirements q
                                            JOIN bookable_resources r
                                              ON r.tenant_id=q.tenant_id AND r.id=q.resource_id
                                             AND r.location_id=q.location_id AND r.active
                                           WHERE q.tenant_id=s.tenant_id AND q.service_id=s.id
                                             AND q.location_id=:location) > 0
                                          AND (SELECT count(*) FROM service_resource_requirements q
                                                JOIN bookable_resources r
                                                  ON r.tenant_id=q.tenant_id AND r.id=q.resource_id
                                                 AND r.location_id=q.location_id AND r.active
                                               WHERE q.tenant_id=s.tenant_id AND q.service_id=s.id
                                                 AND q.location_id=:location)
                                              = (SELECT count(*) FROM service_resource_requirements q
                                                  WHERE q.tenant_id=s.tenant_id AND q.service_id=s.id
                                                    AND q.location_id=:location)))
                                   ORDER BY s.name"""),
                    {"tenant": location["tenant_id"], "location": location["id"]}).mappings().all()
            return {"salon": location["display_name"], "timezone": location["timezone"],
                    "weekly_hours": [dict(r) for r in weekly],
                    "upcoming_exceptions": [{**dict(r),
                                             "exception_date": r["exception_date"].isoformat()}
                                            for r in upcoming],
                    "services": [dict(r) for r in services]}
        finally:
            engine.dispose()
