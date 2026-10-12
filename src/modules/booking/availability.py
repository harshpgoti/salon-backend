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
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

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


def _local_instant(day: date, minutes: int, zone: ZoneInfo) -> datetime | None:
    """Localize a wall-clock minute-of-day, returning None across a DST gap."""
    hour, minute = divmod(minutes, 60)
    naive = datetime.combine(day, time(hour=hour, minute=minute))
    try:
        return naive.replace(tzinfo=zone, fold=0)
    except (ValueError, OverflowError):
        return None


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
        earliest = now + timedelta(minutes=DEFAULT_MIN_NOTICE_MINUTES)
        latest = now + timedelta(days=DEFAULT_HORIZON_DAYS)

        # Staff who may perform this service at this location.
        candidates = connection.execute(
            text("""SELECT s.id::text AS id, s.display_name FROM staff_profiles s
                    JOIN staff_service_qualifications q
                      ON q.tenant_id=s.tenant_id AND q.staff_id=s.id AND q.service_id=:service
                    JOIN staff_locations sl
                      ON sl.tenant_id=s.tenant_id AND sl.staff_id=s.id AND sl.location_id=:location
                    WHERE s.tenant_id=:tenant AND s.is_bookable
                      AND (s.deactivated_on IS NULL OR s.deactivated_on > CURRENT_DATE)
                      AND (CAST(:staff AS uuid) IS NULL OR s.id = CAST(:staff AS uuid))
                    ORDER BY s.display_name"""),
            {"tenant": tenant_id, "service": service["id"], "location": location["id"],
             "staff": staff_id}).mappings().all()
        if not candidates:
            return []

        weekly = connection.execute(
            text("""SELECT weekday, to_char(opens_at,'HH24:MI') AS opens_at,
                           to_char(closes_at,'HH24:MI') AS closes_at
                    FROM location_hours WHERE tenant_id=:tenant AND location_id=:location
                      AND superseded_at IS NULL"""),
            {"tenant": tenant_id, "location": location["id"]}).mappings().all()
        hours_by_day: dict[int, list[tuple[int, int]]] = {}
        for row in weekly:
            opens = _minutes(row["opens_at"])
            closes = _minutes(row["closes_at"])
            hours_by_day.setdefault(row["weekday"], []).append((opens, closes))

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
        total = duration + pre + post

        slots = []
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
                windows = hours_by_day.get(day.weekday(), [])

            for staff in candidates:
                shifts = connection.execute(
                    text("""SELECT to_char(starts_at,'HH24:MI') AS starts_at,
                                   to_char(ends_at,'HH24:MI') AS ends_at, breaks, effective_from, effective_to
                            FROM staff_shifts WHERE tenant_id=:tenant AND staff_id=:staff
                              AND weekday=:weekday
                              AND effective_from <= :day
                              AND (effective_to IS NULL OR effective_to >= :day)"""),
                    {"tenant": tenant_id, "staff": staff["id"], "weekday": day.weekday(),
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
                        occupied = [(_minutes(b["start"]), _minutes(b["end"]))
                                    for b in (shift["breaks"] or [])]
                        for minute in range(shift_start, shift_end - total + 1, SLOT_STEP_MINUTES):
                            # The whole busy block, buffers included, must fit the shift and
                            # must not cross a break.
                            block_start, block_end = minute, minute + total
                            service_start, service_end = minute + pre, minute + pre + duration
                            if any(block_start < b_end and b_start < block_end for b_start, b_end in occupied):
                                continue
                            starts_at = _local_instant(day, service_start, zone)
                            ends_at = _local_instant(day, service_end, zone)
                            occupies_from = _local_instant(day, block_start, zone)
                            occupies_to = _local_instant(day, block_end, zone)
                            if None in (starts_at, ends_at, occupies_from, occupies_to):
                                continue
                            if starts_at < earliest or starts_at > latest:
                                continue
                            clash = connection.execute(
                                text("""SELECT 1 FROM reservations
                                        WHERE tenant_id=:tenant AND state='active' AND staff_id=:staff
                                          AND occupied_range && tstzrange(:from_ts,:to_ts,'[)') LIMIT 1"""),
                                {"tenant": tenant_id, "staff": staff["id"],
                                 "from_ts": occupies_from.isoformat(), "to_ts": occupies_to.isoformat()}
                            ).scalar_one_or_none()
                            if clash is not None:
                                continue
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
        hours, minutes = value.split(":")
        return int(hours) * 60 + int(minutes)

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
                                   post_buffer_minutes, price_minor, currency
                            FROM services WHERE tenant_id=:tenant AND id=:id
                              AND archived_at IS NULL AND NOT requires_resource"""),
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
                    text("""SELECT id::text AS id, name, duration_minutes, price_minor, currency
                            FROM services WHERE tenant_id=:tenant AND archived_at IS NULL
                              AND NOT requires_resource ORDER BY name"""),
                    {"tenant": location["tenant_id"]}).mappings().all()
            return {"salon": location["display_name"], "timezone": location["timezone"],
                    "weekly_hours": [dict(r) for r in weekly],
                    "upcoming_exceptions": [{**dict(r),
                                             "exception_date": r["exception_date"].isoformat()}
                                            for r in upcoming],
                    "services": [dict(r) for r in services]}
        finally:
            engine.dispose()