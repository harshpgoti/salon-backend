"""Operational dashboards and reports (FR-058/FR-059/FR-062; TASK-041/042).

The Today endpoint is the front-desk surface; the operational report is the
owner surface: bookings, completions, cancellations, no-shows, utilization,
service mix, repeat business, forecast booked value and recorded receipts.
Every figure carries its canonical definition (FR-062): what it counts, over
what denominator, in what currency and timezone, from which source, and when
it was generated. Filters are permission-scoped like everywhere else.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text

__all__ = ["register_analytics_routes"]


def _day_bounds(day: date) -> tuple[str, str]:
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    from datetime import timedelta as _td
    return start.isoformat(), (end + _td(days=1)).isoformat()


def _scope_locations(role: str, assigned: list, location_id: uuid.UUID | None) -> list[str] | None:
    """Location ids the caller may see: owner sees all; staff narrow to assignment."""
    if location_id is not None:
        wanted = str(location_id)
        if role != "owner" and assigned and wanted not in {str(a) for a in assigned}:
            raise HTTPException(status_code=403, detail={"code": "location_out_of_scope"})
        return [wanted]
    if role == "owner" or not assigned:
        return None
    return [str(a) for a in assigned]


def _range_tz(connection, *, tenant_id: str, locations: list[str] | None,
              day_from: date, day_to: date) -> tuple[str, str, str]:
    """UTC bounds for an inclusive local date range.

    Dates are interpreted in the filtered location's timezone, else the
    primary location's, else UTC — and the response states which (FR-062).
    """
    tzname = "UTC"
    if locations is not None and len(locations) == 1:
        tzname = connection.execute(
            text("SELECT timezone FROM locations WHERE tenant_id=:t AND id=:l"),
            {"t": tenant_id, "l": locations[0]}).scalar_one_or_none() or "UTC"
    else:
        tzname = connection.execute(
            text("""SELECT timezone FROM locations
                    WHERE tenant_id=:t ORDER BY created_at LIMIT 1"""),
            {"t": tenant_id}).scalar_one_or_none() or "UTC"
    from zoneinfo import ZoneInfo
    try:
        zone = ZoneInfo(tzname)
    except Exception:
        zone = ZoneInfo("UTC")
        tzname = "UTC"
    start = datetime(day_from.year, day_from.month, day_from.day, tzinfo=zone)
    end = datetime(day_to.year, day_to.month, day_to.day, tzinfo=zone)
    from datetime import timedelta as _td
    return start.isoformat(), (end + _td(days=1)).isoformat(), tzname


def register_analytics_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
) -> None:
    @app.get("/v1/tenants/{tenant_id}/reports/today", tags=["reports"])
    def today_report(tenant_id: uuid.UUID, day: date | None = None,
                     location_id: uuid.UUID | None = None,
                     identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.read")
                locations = _scope_locations(role, assigned, location_id)
                target = day or date.today()
                start_iso, end_iso = _day_bounds(target)
                loc_filter = ""
                params: dict = {"tenant": str(tenant_id),
                                "start": start_iso, "end": end_iso}
                if locations is not None:
                    loc_filter = "AND a.location_id = ANY(:locs)"
                    params["locs"] = locations

                day_rows = connection.execute(
                    text(f"""SELECT a.id::text AS id, a.location_id::text AS location_id,
                                    a.client_id::text AS client_id, c.name AS client_name,
                                    a.staff_id::text AS staff_id, a.status,
                                    a.starts_at, a.ends_at, s.service_name
                             FROM appointments a
                             JOIN clients c ON c.tenant_id=a.tenant_id AND c.id=a.client_id
                             JOIN appointment_services s
                               ON s.tenant_id=a.tenant_id AND s.appointment_id=a.id
                              AND s.sequence = 1
                             WHERE a.tenant_id=:tenant {loc_filter}
                               AND a.starts_at >= :start AND a.starts_at < :end
                             ORDER BY a.starts_at"""),
                    params).mappings().all()
                appointments = [
                    {**dict(r), "starts_at": r["starts_at"].isoformat(),
                     "ends_at": r["ends_at"].isoformat()} for r in day_rows]

                by_state: dict[str, int] = {}
                for r in day_rows:
                    by_state[r["status"]] = by_state.get(r["status"], 0) + 1

                approvals = connection.execute(
                    text(f"""SELECT a.id::text AS id, a.starts_at,
                                    c.name AS client_name FROM appointments a
                             JOIN clients c ON c.tenant_id=a.tenant_id AND c.id=a.client_id
                             WHERE a.tenant_id=:tenant {loc_filter}
                               AND a.status='pending_approval'
                             ORDER BY a.starts_at LIMIT 50"""),
                    params).mappings().all()

                closures = connection.execute(
                    text("""SELECT ci.id::text AS id,
                                   ci.appointment_id::text AS appointment_id,
                                   ci.exception_date, ci.status
                            FROM closure_impacts ci
                            WHERE ci.tenant_id=:tenant AND ci.status='active'
                              AND (:locs_all OR ci.location_id = ANY(:locs))
                            ORDER BY ci.exception_date LIMIT 50"""),
                    {"tenant": str(tenant_id),
                     "locs_all": locations is None,
                     "locs": locations or []}).mappings().all()

                weekday = target.weekday()
                on_shift = connection.execute(
                    text("""SELECT DISTINCT p.id::text AS staff_id, p.display_name
                            FROM staff_profiles p
                            JOIN staff_shifts s ON s.tenant_id=p.tenant_id
                                                AND s.staff_id=p.id
                            JOIN staff_locations sl ON sl.tenant_id=p.tenant_id
                                                    AND sl.staff_id=p.id
                            WHERE p.tenant_id=:tenant AND s.weekday=:weekday
                              AND (:locs_all OR sl.location_id = ANY(:locs))
                            ORDER BY p.display_name LIMIT 100"""),
                    {"tenant": str(tenant_id), "weekday": weekday,
                     "locs_all": locations is None, "locs": locations or []}).mappings().all()
                on_leave = connection.execute(
                    text("""SELECT DISTINCT p.id::text AS staff_id FROM staff_profiles p
                            JOIN staff_leave lv ON lv.tenant_id=p.tenant_id
                                               AND lv.staff_id=p.id
                            WHERE p.tenant_id=:tenant AND lv.approved IS TRUE
                              AND lv.starts_on <= :day AND lv.ends_on >= :day
                              AND (:locs_all OR EXISTS (
                                     SELECT 1 FROM staff_locations sl
                                     WHERE sl.tenant_id=p.tenant_id
                                       AND sl.staff_id=p.id
                                       AND sl.location_id = ANY(:locs)))"""),
                    {"tenant": str(tenant_id), "day": target.isoformat(),
                     "locs_all": locations is None,
                     "locs": locations or []}).scalars().all()

                failed_comms = connection.execute(
                    text("""SELECT count(*) FROM communications
                            WHERE tenant_id=:tenant AND state='failed'"""),
                    {"tenant": str(tenant_id)}).scalar_one()
                open_letters = connection.execute(
                    text("""SELECT count(*) FROM dead_letters
                            WHERE tenant_id=:tenant AND resolved_at IS NULL"""),
                    {"tenant": str(tenant_id)}).scalar_one()
                open_tasks = connection.execute(
                    text("""SELECT count(*) FROM follow_up_tasks
                            WHERE tenant_id=:tenant AND status='open'"""),
                    {"tenant": str(tenant_id)}).scalar_one()
            return {
                "date": target.isoformat(),
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "locations_scoped": locations,
                "appointments": appointments,
                "counts_by_state": by_state,
                "pending_approvals": [
                    {**dict(r), "starts_at": r["starts_at"].isoformat()} for r in approvals],
                "active_closure_impacts": [
                    {**dict(r), "exception_date": str(r["exception_date"])} for r in closures],
                "staff_on_shift": [dict(r) for r in on_shift],
                "staff_on_leave": list(on_leave),
                "delivery_tasks": {"failed_communications": failed_comms,
                                   "open_dead_letters": open_letters,
                                   "open_follow_up_tasks": open_tasks},
            }
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/reports/operational", tags=["reports"])
    def operational_report(tenant_id: uuid.UUID, date_from: date, date_to: date,
                           location_id: uuid.UUID | None = None,
                           staff_id: uuid.UUID | None = None,
                           service_id: uuid.UUID | None = None,
                           identity=Depends(require_identity)) -> dict:
        """Owner operational and receipt report (FR-059/FR-062).

        One call, nine sections, every figure defined: bookings by state with
        completion/cancellation/no-show rates over stated denominators; staff
        utilization (booked vs scheduled minutes); service mix with booked
        value; repeat vs new clients; forecast booked value (future confirmed
        work, labelled booked — never revenue); recorded gross/net receipts
        with refund/adjustment summaries (recorded — never settled). Dates are
        inclusive local days in the stated timezone.
        """
        if date_to < date_from:
            raise HTTPException(status_code=422, detail={"code": "inverted_range"})
        if (date_to - date_from).days > 366:
            raise HTTPException(status_code=422, detail={"code": "range_too_wide"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "reports.read")
                locations = _scope_locations(role, assigned, location_id)
                start_iso, end_iso, tzname = _range_tz(
                    connection, tenant_id=str(tenant_id), locations=locations,
                    day_from=date_from, day_to=date_to)
                filt = ["a.tenant_id=:tenant",
                        "a.starts_at >= :start", "a.starts_at < :end"]
                params: dict = {"tenant": str(tenant_id),
                                "start": start_iso, "end": end_iso}
                if locations is not None:
                    filt.append("a.location_id = ANY(:locs)")
                    params["locs"] = locations
                if staff_id is not None:
                    filt.append("a.staff_id = :staff")
                    params["staff"] = str(staff_id)
                if service_id is not None:
                    filt.append("""EXISTS (
                        SELECT 1 FROM appointment_services s
                        WHERE s.tenant_id=a.tenant_id AND s.appointment_id=a.id
                          AND s.service_id=:service)""")
                    params["service"] = str(service_id)
                where = " AND ".join(filt)

                states = connection.execute(
                    text(f"""SELECT a.status, count(*) FROM appointments a
                             WHERE {where} GROUP BY a.status"""),
                    params).mappings().all()
                by_state = {r["status"]: r["count"] for r in states}
                total = sum(by_state.values())
                completed = by_state.get("completed", 0)
                cancelled = by_state.get("cancelled", 0)
                no_show = by_state.get("no_show", 0)

                mix = connection.execute(
                    text(f"""SELECT s.service_name AS service, count(*) AS bookings,
                                    SUM(s.price_minor) AS booked_minor
                             FROM appointments a
                             JOIN appointment_services s
                               ON s.tenant_id=a.tenant_id AND s.appointment_id=a.id
                              AND s.sequence = 1
                             WHERE {where} AND a.status NOT IN ('cancelled', 'no_show',
                                                                'rejected')
                             GROUP BY s.service_name ORDER BY bookings DESC"""),
                    params).mappings().all()

                # Repeat = the client booked anything before the range opened;
                # otherwise the ranged business is new.
                repeat = connection.execute(
                    text(f"""SELECT count(DISTINCT a.client_id) FROM appointments a
                             WHERE {where} AND EXISTS (
                               SELECT 1 FROM appointments prior
                               WHERE prior.tenant_id=a.tenant_id
                                 AND prior.client_id=a.client_id
                                 AND prior.starts_at < :start)"""),
                    params).scalar_one()
                booked_clients = connection.execute(
                    text(f"""SELECT count(DISTINCT a.client_id) FROM appointments a
                             WHERE {where}"""),
                    params).scalar_one()

                future_value = connection.execute(
                    text(f"""SELECT COALESCE(SUM(s.price_minor), 0) FROM appointments a
                             JOIN appointment_services s
                               ON s.tenant_id=a.tenant_id AND s.appointment_id=a.id
                              AND s.sequence = 1
                             WHERE {where} AND a.status IN ('confirmed', 'checked_in',
                                                            'in_service', 'pending_approval')
                               AND a.starts_at >= :now"""),
                    {**params, "now": datetime.now(timezone.utc).isoformat()},
                ).scalar_one()

                staff_rows = connection.execute(
                    text(f"""SELECT a.staff_id::text AS staff_id,
                                    COALESCE(SUM(s.duration_minutes), 0) AS booked_minutes
                             FROM appointments a
                             JOIN appointment_services s
                               ON s.tenant_id=a.tenant_id AND s.appointment_id=a.id
                              AND s.sequence = 1
                             WHERE {where} AND a.status NOT IN ('cancelled', 'no_show',
                                                                'rejected')
                             GROUP BY a.staff_id"""),
                    params).mappings().all()
                weeks = max(1.0, round(((date_to - date_from).days + 1) / 7, 2))
                utilization = []
                for row in staff_rows:
                    scheduled = connection.execute(
                        text("""SELECT COALESCE(SUM(
                                          (EXTRACT(EPOCH FROM ends_at::time - starts_at::time)
                                           / 60)::int), 0)
                                FROM staff_shifts WHERE tenant_id=:t AND staff_id=:s"""),
                        {"t": str(tenant_id), "s": row["staff_id"]}).scalar_one()
                    available = scheduled * weeks
                    utilization.append({
                        "staff_id": row["staff_id"],
                        "booked_minutes": int(row["booked_minutes"]),
                        "scheduled_minutes": int(available),
                        "utilization": (round(row["booked_minutes"] / available, 3)
                                        if available else None)})

                receipt_filter = ["e.tenant_id=:tenant", "e.posted_at >= :start",
                                  "e.posted_at < :end"]
                receipt_params: dict = {"tenant": str(tenant_id),
                                        "start": start_iso, "end": end_iso}
                if locations is not None:
                    receipt_filter.append("""EXISTS (
                        SELECT 1 FROM appointments a
                        WHERE a.tenant_id=e.tenant_id AND a.id=e.appointment_id
                          AND a.location_id = ANY(:locs))""")
                    receipt_params["locs"] = locations
                receipt_where = " AND ".join(receipt_filter)
                receipts = connection.execute(
                    text(f"""SELECT e.kind, count(*) AS entries,
                                    COALESCE(SUM(e.amount_minor), 0) AS minor
                             FROM salon_receipt_entries e
                             WHERE {receipt_where} GROUP BY e.kind"""),
                    receipt_params).mappings().all()
                takings = {r["kind"]: {"entries": r["entries"], "minor": r["minor"]}
                           for r in receipts}
                gross = takings.get("payment", {}).get("minor", 0)
                refunds = takings.get("refund", {}).get("minor", 0)
                adjustments = takings.get("adjustment", {}).get("minor", 0)
            definitions = {
                "booking_counts": "Appointments starting in range by status; "
                                  "denominator: all such appointments.",
                "completion_rate": "completed / all ranged appointments.",
                "cancellation_rate": "cancelled / all ranged appointments.",
                "no_show_rate": "no_show / all ranged appointments.",
                "service_mix": "Non-cancelled ranged bookings by first service line; "
                               "booked_minor sums snapshot prices (booked value, not revenue).",
                "repeat_clients": "Ranged clients with any appointment before "
                                  "the range opened; new_clients = ranged minus repeat.",
                "forecast_booked_value": "Snapshot prices of live future appointments; "
                                         "booked value, never revenue.",
                "utilization": "Booked minutes / scheduled shift minutes over the range; "
                               "null when no shifts scheduled.",
                "recorded_receipts": "Manually posted salon money in range by kind; "
                                     "net = payments - refunds + adjustments "
                                     "(recorded, never settled).",
                "basis": f"Inclusive local dates {date_from.isoformat()}..{date_to.isoformat()} "
                         f"in {tzname}; money in CAD minor units.",
            }
            return {
                "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                "timezone": tzname,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "filters": {"location_id": str(location_id) if location_id else None,
                            "staff_id": str(staff_id) if staff_id else None,
                            "service_id": str(service_id) if service_id else None},
                "bookings": {"by_state": by_state, "total": total},
                "rates": {
                    "completion_rate": round(completed / total, 4) if total else None,
                    "cancellation_rate": round(cancelled / total, 4) if total else None,
                    "no_show_rate": round(no_show / total, 4) if total else None},
                "service_mix": [dict(r) for r in mix],
                "clients": {"repeat_clients": repeat,
                            "new_clients": booked_clients - repeat},
                "forecast_booked_value_minor": int(future_value or 0),
                "staff_utilization": utilization,
                "recorded_receipts": {"by_kind": takings, "gross_minor": gross,
                                      "refunds_minor": refunds,
                                      "adjustments_minor": adjustments,
                                      "net_minor": gross - refunds + adjustments},
                "definitions": definitions,
            }
        finally:
            engine.dispose()
