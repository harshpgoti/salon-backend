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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text

__all__ = ["register_analytics_routes"]


def _scope_locations(role: str, assigned: list, location_id: uuid.UUID | None) -> list[str] | None:
    """Location ids the caller may see: owner sees all; staff narrow to assignment."""
    if role == "owner":
        return [str(location_id)] if location_id is not None else None
    allowed = {str(value) for value in (assigned or [])}
    if not allowed:
        if location_id is not None:
            raise HTTPException(status_code=403, detail={"code": "location_out_of_scope"})
        return []
    if location_id is not None:
        wanted = str(location_id)
        if wanted not in allowed:
            raise HTTPException(status_code=403, detail={"code": "location_out_of_scope"})
        return [wanted]
    return sorted(allowed)


def _default_today(connection, *, tenant_id: str,
                   locations: list[str] | None) -> date:
    """Use the selected location's zone, or the tenant's primary location zone."""
    if locations is not None and not locations:
        return datetime.now(timezone.utc).date()
    params: dict = {"tenant": tenant_id}
    location_filter = ""
    if locations is not None and len(locations) == 1:
        location_filter = "AND id=:location"
        params["location"] = locations[0]
    elif locations is not None:
        location_filter = "AND id=ANY(:locations)"
        params["locations"] = locations
    tzname = connection.execute(
        text(f"""SELECT timezone FROM locations
                WHERE tenant_id=:tenant {location_filter}
                ORDER BY created_at, id LIMIT 1"""), params).scalar_one_or_none() or "UTC"
    try:
        zone = ZoneInfo(tzname)
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    return datetime.now(zone).date()


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
                target = day or _default_today(
                    connection, tenant_id=str(tenant_id), locations=locations)
                loc_filter = ""
                params: dict = {"tenant": str(tenant_id), "day": target.isoformat()}
                if locations is not None:
                    loc_filter = "AND a.location_id = ANY(:locs)"
                    params["locs"] = locations

                day_rows = connection.execute(
                    text(f"""SELECT a.id::text AS id, a.location_id::text AS location_id,
                                    a.client_id::text AS client_id, c.name AS client_name,
                                    a.staff_id::text AS staff_id, a.status,
                                    a.starts_at, a.ends_at, a.local_timezone, s.service_name
                             FROM appointments a
                             JOIN clients c ON c.tenant_id=a.tenant_id AND c.id=a.client_id
                             JOIN appointment_services s
                               ON s.tenant_id=a.tenant_id AND s.appointment_id=a.id
                              AND s.sequence = 1
                             WHERE a.tenant_id=:tenant {loc_filter}
                               AND (a.starts_at AT TIME ZONE a.local_timezone)::date=:day
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
                    text("""SELECT count(*) FROM communications c
                            WHERE c.tenant_id=:tenant AND c.state='failed'
                              AND (:locs_all OR
                                (c.source_type='appointment' AND EXISTS (
                                  SELECT 1 FROM appointments a
                                  WHERE a.tenant_id=c.tenant_id AND a.id=c.source_id
                                    AND a.location_id=ANY(:locs))) OR
                                (c.source_type='location_broadcast' AND c.source_id=ANY(:locs)))"""),
                    {"tenant": str(tenant_id), "locs_all": locations is None,
                     "locs": locations or []}).scalar_one()
                open_letters = connection.execute(
                    text("""SELECT count(*) FROM dead_letters d
                            WHERE d.tenant_id=:tenant AND d.resolved_at IS NULL
                              AND (:locs_all OR EXISTS (
                                SELECT 1 FROM communications c
                                WHERE c.tenant_id=d.tenant_id AND c.id=d.communication_id
                                  AND ((c.source_type='appointment' AND EXISTS (
                                    SELECT 1 FROM appointments a
                                    WHERE a.tenant_id=c.tenant_id AND a.id=c.source_id
                                      AND a.location_id=ANY(:locs))) OR
                                       (c.source_type='location_broadcast' AND c.source_id=ANY(:locs)))))"""),
                    {"tenant": str(tenant_id), "locs_all": locations is None,
                     "locs": locations or []}).scalar_one()
                open_tasks = connection.execute(
                    text("""SELECT count(*) FROM follow_up_tasks t
                            WHERE t.tenant_id=:tenant AND t.status='open'
                              AND (:locs_all OR EXISTS (
                                SELECT 1 FROM clients c
                                WHERE c.tenant_id=t.tenant_id AND c.id=t.client_id
                                  AND c.preferred_location_id=ANY(:locs)) OR EXISTS (
                                SELECT 1 FROM closure_impacts ci
                                WHERE ci.tenant_id=t.tenant_id AND t.source_type='closure_impact'
                                  AND ci.id=t.source_id AND ci.location_id=ANY(:locs)) OR
                                (t.source_type='closure_broadcast' AND t.source_id=ANY(:locs)))"""),
                    {"tenant": str(tenant_id), "locs_all": locations is None,
                     "locs": locations or []}).scalar_one()
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
        if (date_to - date_from).days > 365:
            raise HTTPException(status_code=422, detail={"code": "range_too_wide"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "reports.read")
                locations = _scope_locations(role, assigned, location_id)
                _, _, tzname = _range_tz(
                    connection, tenant_id=str(tenant_id), locations=locations,
                    day_from=date_from, day_to=date_to)
                if locations is None or len(locations) != 1:
                    tzname = "per_appointment_local_timezone"
                filt = ["a.tenant_id=:tenant",
                        "(a.starts_at AT TIME ZONE a.local_timezone)::date >= :date_from",
                        "(a.starts_at AT TIME ZONE a.local_timezone)::date <= :date_to"]
                params: dict = {"tenant": str(tenant_id),
                                "date_from": date_from.isoformat(),
                                "date_to": date_to.isoformat(),
                                "service": str(service_id) if service_id else None}
                if locations is not None:
                    filt.append("a.location_id = ANY(:locs)")
                    params["locs"] = locations
                if staff_id is not None:
                    filt.append("""EXISTS (SELECT 1 FROM appointment_services staff_line
                        WHERE staff_line.tenant_id=a.tenant_id
                          AND staff_line.appointment_id=a.id AND staff_line.staff_id=:staff)""")
                    params["staff"] = str(staff_id)
                if service_id is not None:
                    filt.append("""EXISTS (
                        SELECT 1 FROM appointment_services s
                        WHERE s.tenant_id=a.tenant_id AND s.appointment_id=a.id
                          AND s.service_id=:service)""")
                    params["service"] = str(service_id)
                where = " AND ".join(filt)
                service_line_filter = ""
                if service_id is not None:
                    service_line_filter = "AND s.service_id=:service"
                if staff_id is not None:
                    service_line_filter += " AND s.staff_id=:staff"

                states = connection.execute(
                    text(f"""SELECT a.status, count(*) FROM appointments a
                             WHERE {where} GROUP BY a.status"""),
                    params).mappings().all()
                by_state = {r["status"]: r["count"] for r in states}
                total = sum(by_state.values())
                completed = by_state.get("completed", 0)
                cancelled = by_state.get("cancelled", 0)
                no_show = by_state.get("no_show", 0)
                accepted = total - by_state.get("pending_approval", 0) - by_state.get("rejected", 0)
                closed_out = completed + no_show

                mix = connection.execute(
                    text(f"""SELECT s.service_id::text AS service_id,
                                    s.service_name AS service, count(*) AS bookings,
                                    SUM(s.price_minor) AS booked_minor
                             FROM appointments a
                             JOIN appointment_services s
                               ON s.tenant_id=a.tenant_id AND s.appointment_id=a.id
                             WHERE {where} AND a.status NOT IN ('cancelled', 'no_show',
                                                                'rejected', 'pending_approval')
                               {service_line_filter}
                             GROUP BY s.service_id, s.service_name ORDER BY bookings DESC"""),
                    params).mappings().all()

                # Repeat clients have completed history before this range; a booking
                # attempt alone does not make a client returning.
                repeat = connection.execute(
                    text(f"""SELECT count(DISTINCT a.client_id) FROM appointments a
                             WHERE {where} AND a.status='completed' AND EXISTS (
                               SELECT 1 FROM appointments prior
                               WHERE prior.tenant_id=a.tenant_id
                                 AND prior.client_id=a.client_id
                                 AND prior.status='completed'
                                 AND (prior.starts_at AT TIME ZONE prior.local_timezone)::date < :date_from
                                 AND (CAST(:service AS uuid) IS NULL OR EXISTS (
                                   SELECT 1 FROM appointment_services prior_service
                                   WHERE prior_service.tenant_id=prior.tenant_id
                                     AND prior_service.appointment_id=prior.id
                                     AND prior_service.service_id=:service)))"""),
                    params).scalar_one()
                booked_clients = connection.execute(
                    text(f"""SELECT count(DISTINCT a.client_id) FROM appointments a
                             WHERE {where} AND a.status='completed'"""),
                    params).scalar_one()

                future_value = connection.execute(
                    text(f"""SELECT COALESCE(SUM(s.price_minor), 0) FROM appointments a
                             JOIN appointment_services s
                               ON s.tenant_id=a.tenant_id AND s.appointment_id=a.id
                             WHERE {where} AND a.status IN ('confirmed', 'checked_in',
                                                            'in_service')
                               {service_line_filter}
                               AND a.starts_at >= :now
                               AND (a.starts_at AT TIME ZONE a.local_timezone)::date <= :date_to"""),
                    {**params, "now": datetime.now(timezone.utc).isoformat()},
                ).scalar_one()

                staff_rows = connection.execute(
                    text(f"""SELECT s.staff_id::text AS staff_id, p.display_name,
                                    COALESCE(SUM(s.duration_minutes), 0) AS booked_minutes
                             FROM appointments a
                             JOIN appointment_services s
                               ON s.tenant_id=a.tenant_id AND s.appointment_id=a.id
                             JOIN staff_profiles p ON p.tenant_id=s.tenant_id AND p.id=s.staff_id
                             WHERE {where} AND a.status IN ('confirmed','checked_in',
                                                           'in_service','completed')
                               {service_line_filter}
                             GROUP BY s.staff_id, p.display_name"""),
                    params).mappings().all()
                utilization = []
                for row in staff_rows:
                    shift_filters = ""
                    shift_params = {"t": str(tenant_id), "s": row["staff_id"],
                                    "from_day": date_from.isoformat(),
                                    "to_day": date_to.isoformat()}
                    if locations is not None:
                        shift_filters = "AND sh.location_id = ANY(:locs)"
                        shift_params["locs"] = locations
                    available = connection.execute(text(f"""SELECT COALESCE(SUM(
                                GREATEST(0,
                                  (EXTRACT(EPOCH FROM (
                                    (CASE WHEN x.kind='custom' THEN LEAST(sh.ends_at,x.closes_at)
                                          ELSE sh.ends_at END) -
                                    (CASE WHEN x.kind='custom' THEN GREATEST(sh.starts_at,x.opens_at)
                                          ELSE sh.starts_at END)))/60)::int
                                  - COALESCE((SELECT SUM(GREATEST(0, EXTRACT(EPOCH FROM (
                                      LEAST((b->>'end')::time,
                                        CASE WHEN x.kind='custom' THEN LEAST(sh.ends_at,x.closes_at) ELSE sh.ends_at END) -
                                      GREATEST((b->>'start')::time,
                                        CASE WHEN x.kind='custom' THEN GREATEST(sh.starts_at,x.opens_at) ELSE sh.starts_at END)
                                    ))/60)::int) FROM jsonb_array_elements(sh.breaks) b), 0))), 0)
                              FROM generate_series(CAST(:from_day AS date), CAST(:to_day AS date),
                                                   interval '1 day') AS work_day
                              JOIN staff_shifts sh ON sh.tenant_id=:t AND sh.staff_id=:s
                                AND sh.weekday=(EXTRACT(ISODOW FROM work_day)::int - 1)
                                AND sh.effective_from <= work_day::date
                                AND (sh.effective_to IS NULL OR sh.effective_to >= work_day::date)
                                {shift_filters}
                              LEFT JOIN location_exceptions x ON x.tenant_id=sh.tenant_id
                                AND x.location_id=sh.location_id AND x.exception_date=work_day::date
                                AND x.published_at IS NOT NULL
                              WHERE NOT EXISTS (SELECT 1 FROM staff_leave l
                                WHERE l.tenant_id=sh.tenant_id AND l.staff_id=sh.staff_id
                                  AND l.approved AND l.starts_on <= work_day::date
                                  AND l.ends_on >= work_day::date)
                                AND x.kind IS DISTINCT FROM 'closed'"""), shift_params).scalar_one()
                    utilization.append({
                        "staff_id": row["staff_id"],
                        "staff_name": row["display_name"],
                        "booked_minutes": int(row["booked_minutes"]),
                        "scheduled_minutes": int(available),
                        "utilization": (round(int(row["booked_minutes"]) / int(available), 3)
                                        if available else None)})

                receipt_filter = ["e.tenant_id=:tenant",
                                  "(e.posted_at AT TIME ZONE a.local_timezone)::date >= :date_from",
                                  "(e.posted_at AT TIME ZONE a.local_timezone)::date <= :date_to"]
                receipt_params: dict = {"tenant": str(tenant_id),
                                        "date_from": date_from.isoformat(),
                                        "date_to": date_to.isoformat()}
                if locations is not None:
                    receipt_filter.append("a.location_id = ANY(:locs)")
                    receipt_params["locs"] = locations
                if staff_id is not None:
                    receipt_filter.append("""EXISTS (SELECT 1 FROM appointment_services staff_line
                        WHERE staff_line.tenant_id=a.tenant_id
                          AND staff_line.appointment_id=a.id AND staff_line.staff_id=:staff)""")
                    receipt_params["staff"] = str(staff_id)
                if service_id is not None:
                    receipt_filter.append("""EXISTS (SELECT 1 FROM appointment_services service_line
                        WHERE service_line.tenant_id=a.tenant_id
                          AND service_line.appointment_id=a.id AND service_line.service_id=:service)""")
                    receipt_params["service"] = str(service_id)
                receipt_where = " AND ".join(receipt_filter)
                receipts = connection.execute(
                    text(f"""SELECT e.kind, count(*) AS entries,
                                    COALESCE(SUM(e.amount_minor), 0) AS minor
                             FROM salon_receipt_entries e
                             JOIN appointments a ON a.tenant_id=e.tenant_id
                                                AND a.id=e.appointment_id
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
                "completion_rate": "completed / accepted ranged appointments.",
                "cancellation_rate": "cancelled / accepted ranged appointments; pending and rejected excluded.",
                "no_show_rate": "no_show / (completed + no_show) closed-out ranged appointments.",
                "service_mix": "Non-cancelled ranged bookings by each service line; "
                               "booked_minor sums snapshot prices (booked value, not revenue).",
                "repeat_clients": "Clients with a completed visit in range and an earlier "
                                  "completed visit; new_clients = completed clients minus repeat.",
                "forecast_booked_value": "Snapshot prices of live future appointments; "
                                         "booked value, never revenue.",
                "utilization": "Reserved attended service minutes for confirmed/checked-in/in-service/completed "
                               "bookings / dated effective shift minutes after breaks, approved leave "
                               "and published location exceptions; null when capacity is zero.",
                "recorded_receipts": "Manually posted salon money in range by kind; "
                                     "refund entries are stored negative; net is the signed sum "
                                     "(recorded, never settled); staff/service filters include "
                                     "the whole receipt ledger for matching appointments because "
                                     "receipt entries are not allocated to individual service lines.",
                "basis": f"Inclusive dates {date_from.isoformat()}..{date_to.isoformat()} "
                         "grouped using each appointment's saved local_timezone snapshot; "
                         "receipts use the linked appointment snapshot; money in CAD minor units.",
            }
            return {
                "metric_version": "salon-operational-v1",
                "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                "timezone": "appointment local_timezone snapshot",
                "location_timezone": tzname,
                "timezone_basis": "Appointment local_timezone snapshot for booking dates; linked appointment snapshot for receipt posting dates.",
                "currency": "CAD",
                "source": "appointments, appointment_services, staff_shifts, staff_leave, location_exceptions, salon_receipt_entries",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "filters": {"location_id": str(location_id) if location_id else None,
                            "staff_id": str(staff_id) if staff_id else None,
                            "service_id": str(service_id) if service_id else None},
                "bookings": {"by_state": by_state, "total": total},
                "rates": {
                    "completion_rate": {"numerator": completed, "denominator": accepted,
                                        "value": round(completed / accepted, 4) if accepted else None},
                    "cancellation_rate": {"numerator": cancelled, "denominator": accepted,
                                          "value": round(cancelled / accepted, 4) if accepted else None},
                    "no_show_rate": {"numerator": no_show, "denominator": closed_out,
                                     "value": round(no_show / closed_out, 4) if closed_out else None}},
                "service_mix": [dict(r) for r in mix],
                "clients": {"repeat_clients": repeat,
                            "new_clients": booked_clients - repeat},
                "forecast_booked_value_minor": int(future_value or 0),
                "staff_utilization": utilization,
                "recorded_receipts": {"by_kind": takings, "gross_minor": gross,
                                      "refunds_minor": abs(refunds),
                                      "adjustments_minor": adjustments,
                                      "net_minor": gross + refunds + adjustments},
                "definitions": definitions,
            }
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/reports/clients", tags=["reports"])
    def clients_report(tenant_id: uuid.UUID, as_of: date | None = None,
                       location_id: uuid.UUID | None = None,
                       identity=Depends(require_identity)) -> dict:
        """Client cohorts, retention and data quality (FR-060/FR-062).

        Cohorts group by first completed-visit month: size, repeaters and
        average first-to-second return gap. Cohorts younger than ninety days
        are flagged immature — their return figures are provisional, never
        final. Deleted clients never count; import-sourced clients carry
        their provenance. Data quality lists the fixable gaps: no contact,
        no primary, unverified primary, open duplicate candidates and import
        error rows.
        """
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "reports.read")
                locations = _scope_locations(role, assigned, location_id)
                moment = as_of or _default_today(
                    connection, tenant_id=str(tenant_id), locations=locations)
                loc_filter = ""
                params: dict = {"tenant": str(tenant_id), "as_of": moment.isoformat()}
                if locations is not None:
                    loc_filter = "AND c.preferred_location_id = ANY(:locs)"
                    params["locs"] = locations
                snapshot_at = datetime.now(timezone.utc)
                params["snapshot_at"] = snapshot_at.isoformat()
                reachable_sql = """EXISTS (SELECT 1 FROM consents co
                    JOIN client_contacts cc ON cc.tenant_id=co.tenant_id
                                           AND cc.id=co.contact_id
                                           AND cc.client_id=co.client_id
                    WHERE co.tenant_id=c.tenant_id AND co.client_id=c.id
                      AND co.status='granted' AND co.purpose='marketing'
                      AND co.channel=cc.kind
                      AND co.contact_value_snapshot=cc.normalized_value
                      AND (co.expires_at IS NULL OR co.expires_at > :snapshot_at)
                      AND NOT EXISTS (SELECT 1 FROM consents newer
                        WHERE newer.tenant_id=co.tenant_id
                          AND newer.client_id=co.client_id
                          AND newer.contact_id=co.contact_id
                          AND newer.channel=co.channel AND newer.purpose=co.purpose
                          AND newer.event_sequence > co.event_sequence)
                      AND NOT EXISTS (SELECT 1 FROM suppressions sup
                        WHERE sup.tenant_id=cc.tenant_id
                          AND sup.contact_value=cc.normalized_value
                          AND sup.channel=co.channel
                          AND sup.suppressed_at <= :snapshot_at))"""

                firsts = connection.execute(
                    text(f"""SELECT c.id::text AS client_id,
                                    min((v.completed_at AT TIME ZONE va.local_timezone)::date)
                                        AS first_visit
                             FROM clients c
                             LEFT JOIN visits v ON v.tenant_id=c.tenant_id
                                               AND v.client_id=c.id
                             LEFT JOIN appointments va ON va.tenant_id=v.tenant_id
                                                       AND va.id=v.appointment_id
                                                       AND (v.completed_at AT TIME ZONE
                                                            va.local_timezone)::date <= :as_of
                             WHERE c.tenant_id=:tenant AND c.deleted_at IS NULL
                               {loc_filter}
                             GROUP BY c.id"""),
                    params).mappings().all()
                cohorts: dict[str, dict] = {}
                for row in firsts:
                    key = row["first_visit"].strftime("%Y-%m") if row["first_visit"] else "never"
                    bucket = cohorts.setdefault(key, {"clients": [], "month": key})
                    bucket["clients"].append(row["client_id"])
                cohort_rows = []
                for key in sorted(cohorts):
                    ids = cohorts[key]["clients"]
                    if key == "never":
                        cohort_rows.append({"cohort": key, "clients": len(ids),
                                            "repeaters": 0, "avg_return_days": None,
                                            "immature": False})
                        continue
                    gaps = connection.execute(
                        text("""SELECT v.client_id::text AS client_id,
                                       count(*) AS visits,
                                       min((v.completed_at AT TIME ZONE a.local_timezone)::date)
                                           AS first_visit,
                                       max((v.completed_at AT TIME ZONE a.local_timezone)::date)
                                           AS last_visit
                                FROM visits v
                                JOIN appointments a ON a.tenant_id=v.tenant_id
                                                   AND a.id=v.appointment_id
                                WHERE v.tenant_id=:tenant AND v.client_id = ANY(:ids)
                                  AND (v.completed_at AT TIME ZONE a.local_timezone)::date <= :as_of
                                GROUP BY v.client_id"""),
                        {"tenant": str(tenant_id), "ids": ids,
                         "as_of": moment.isoformat()}).mappings().all()
                    repeaters = sum(1 for g in gaps if g["visits"] > 1)
                    returned_gaps = []
                    for g in gaps:
                        if g["visits"] > 1:
                            second = connection.execute(
                                text("""SELECT (v.completed_at AT TIME ZONE a.local_timezone)::date
                                        FROM visits v JOIN appointments a
                                          ON a.tenant_id=v.tenant_id AND a.id=v.appointment_id
                                        WHERE v.tenant_id=:tenant AND v.client_id=:client
                                          AND (v.completed_at AT TIME ZONE a.local_timezone)::date <= :as_of
                                        ORDER BY v.completed_at LIMIT 1 OFFSET 1"""),
                                {"tenant": str(tenant_id),
                                 "client": g["client_id"],
                                 "as_of": moment.isoformat()}).scalar_one()
                            returned_gaps.append((second - g["first_visit"]).days)
                    cohort_age = (moment - date(
                        int(key[:4]), int(key[5:7]), 1)).days
                    cohort_rows.append({
                        "cohort": key, "clients": len(ids), "repeaters": repeaters,
                        "avg_return_days": (round(sum(returned_gaps) / len(returned_gaps), 1)
                                            if returned_gaps else None),
                        "immature": cohort_age < 90})

                facts = connection.execute(
                    text(f"""SELECT c.id::text AS client_id, c.name, c.source,
                                    (SELECT min((v.completed_at AT TIME ZONE va.local_timezone)::date)
                                      FROM visits v
                                      JOIN appointments va ON va.tenant_id=v.tenant_id
                                                          AND va.id=v.appointment_id
                                      WHERE v.tenant_id=c.tenant_id AND v.client_id=c.id
                                        AND (v.completed_at AT TIME ZONE va.local_timezone)::date <= :as_of)
                                    AS first_visit,
                                    (SELECT max((v.completed_at AT TIME ZONE va.local_timezone)::date)
                                      FROM visits v
                                      JOIN appointments va ON va.tenant_id=v.tenant_id
                                                          AND va.id=v.appointment_id
                                      WHERE v.tenant_id=c.tenant_id AND v.client_id=c.id
                                        AND (v.completed_at AT TIME ZONE va.local_timezone)::date <= :as_of)
                                    AS last_visit,
                                    (SELECT (second.visit_day - first.visit_day)
                                     FROM (
                                       SELECT v.completed_at,
                                              (v.completed_at AT TIME ZONE va.local_timezone)::date
                                                  AS visit_day
                                       FROM visits v JOIN appointments va
                                         ON va.tenant_id=v.tenant_id AND va.id=v.appointment_id
                                       WHERE v.tenant_id=c.tenant_id AND v.client_id=c.id
                                         AND (v.completed_at AT TIME ZONE va.local_timezone)::date <= :as_of
                                       ORDER BY v.completed_at LIMIT 1
                                     ) first
                                     CROSS JOIN LATERAL (
                                       SELECT (v.completed_at AT TIME ZONE va.local_timezone)::date
                                                  AS visit_day
                                       FROM visits v JOIN appointments va
                                         ON va.tenant_id=v.tenant_id AND va.id=v.appointment_id
                                       WHERE v.tenant_id=c.tenant_id AND v.client_id=c.id
                                         AND v.completed_at > first.completed_at
                                         AND (v.completed_at AT TIME ZONE va.local_timezone)::date <= :as_of
                                       ORDER BY v.completed_at LIMIT 1
                                     ) second) AS return_interval_days,
                                    (SELECT s.service_name FROM visit_services s
                                      JOIN visits v ON v.tenant_id=s.tenant_id
                                                   AND v.id=s.visit_id
                                      JOIN appointments va ON va.tenant_id=v.tenant_id
                                                          AND va.id=v.appointment_id
                                      WHERE v.tenant_id=c.tenant_id AND v.client_id=c.id
                                        AND (v.completed_at AT TIME ZONE va.local_timezone)::date <= :as_of
                                      ORDER BY v.completed_at DESC LIMIT 1) AS last_service,
                                    (SELECT min(a.starts_at AT TIME ZONE a.local_timezone)
                                     FROM appointments a
                                      WHERE a.tenant_id=c.tenant_id AND a.client_id=c.id
                                        AND (a.starts_at AT TIME ZONE a.local_timezone)::date > :as_of
                                        AND a.created_at <= ((CAST(:as_of AS date) + 1)::timestamp
                                                           AT TIME ZONE a.local_timezone)
                                        AND a.status IN ('pending_approval', 'confirmed',
                                                         'checked_in', 'in_service'))
                                    AS next_appointment,
                                    EXISTS (SELECT 1 FROM rebooking_cycles r
                                            WHERE r.tenant_id=c.tenant_id
                                              AND r.client_id=c.id AND r.status='due')
                                    AS has_due_cycle,
                                    {reachable_sql} AS reachable
                             FROM clients c
                             WHERE c.tenant_id=:tenant AND c.deleted_at IS NULL
                               {loc_filter}
                             ORDER BY c.created_at DESC LIMIT 200"""),
                    params).mappings().all()
                client_facts = []
                for fact in facts:
                    item = dict(fact)
                    for stamp in ("first_visit", "last_visit", "next_appointment"):
                        item[stamp] = item[stamp].isoformat() if item[stamp] else None
                    client_facts.append(item)

                inactive = {}
                for days in (30, 60, 90):
                    inactive[f"inactive_{days}d"] = connection.execute(
                        text(f"""SELECT count(DISTINCT c.id) FROM clients c
                                 WHERE c.tenant_id=:tenant AND c.deleted_at IS NULL
                                   {loc_filter}
                                   AND NOT EXISTS (
                                     SELECT 1 FROM visits v JOIN appointments a
                                       ON a.tenant_id=v.tenant_id AND a.id=v.appointment_id
                                     WHERE v.tenant_id=c.tenant_id AND v.client_id=c.id
                                       AND (v.completed_at AT TIME ZONE a.local_timezone)::date
                                           BETWEEN CAST(:as_of AS date) - CAST(:days AS integer) + 1
                                               AND CAST(:as_of AS date))"""),
                        {**params, "days": days}).scalar_one()
                due_cycles = connection.execute(
                    text(f"""SELECT count(*) FROM rebooking_cycles r
                            JOIN clients c ON c.tenant_id=r.tenant_id AND c.id=r.client_id
                            WHERE r.tenant_id=:tenant AND r.status='due'
                              AND c.deleted_at IS NULL {loc_filter}"""),
                    params).scalar_one()

                no_contact = connection.execute(
                    text(f"""SELECT count(*) FROM clients c
                             WHERE c.tenant_id=:tenant AND c.deleted_at IS NULL
                               {loc_filter}
                               AND NOT EXISTS (
                                 SELECT 1 FROM client_contacts cc
                                 WHERE cc.tenant_id=c.tenant_id AND cc.client_id=c.id)"""),
                    params).scalar_one()
                no_primary = connection.execute(
                    text(f"""SELECT count(*) FROM clients c
                             WHERE c.tenant_id=:tenant AND c.deleted_at IS NULL
                               {loc_filter}
                               AND EXISTS (SELECT 1 FROM client_contacts cc
                                           WHERE cc.tenant_id=c.tenant_id
                                             AND cc.client_id=c.id)
                               AND NOT EXISTS (SELECT 1 FROM client_contacts cc
                                               WHERE cc.tenant_id=c.tenant_id
                                                 AND cc.client_id=c.id AND cc.is_primary)"""),
                    params).scalar_one()
                unverified_primary = connection.execute(
                    text(f"""SELECT count(*) FROM clients c
                             WHERE c.tenant_id=:tenant AND c.deleted_at IS NULL
                               {loc_filter}
                               AND EXISTS (SELECT 1 FROM client_contacts cc
                                           WHERE cc.tenant_id=c.tenant_id
                                             AND cc.client_id=c.id AND cc.is_primary
                                             AND cc.verification <> 'verified')"""),
                    params).scalar_one()
                open_dupes = connection.execute(
                    text(f"""SELECT count(*) FROM client_duplicate_candidates d
                            JOIN clients c ON c.tenant_id=d.tenant_id AND c.id=d.client_id
                            WHERE d.tenant_id=:tenant AND d.resolution IS NULL
                              AND c.deleted_at IS NULL {loc_filter}"""),
                    params).scalar_one()
                # Import records have no location key. Only an unfiltered owner
                # report can safely display their tenant-wide aggregate.
                import_errors = None
                if role == "owner" and locations is None:
                    import_errors = connection.execute(
                        text("""SELECT COALESCE(SUM(error_rows), 0) FROM imports
                                WHERE tenant_id=:tenant"""),
                        {"tenant": str(tenant_id)}).scalar_one()
                by_source = connection.execute(
                    text(f"""SELECT c.source, count(*) FROM clients c
                             WHERE c.tenant_id=:tenant AND c.deleted_at IS NULL
                               {loc_filter}
                             GROUP BY c.source"""),
                    params).mappings().all()
                reachable_clients = connection.execute(
                    text(f"""SELECT count(*) FROM clients c
                            WHERE c.tenant_id=:tenant AND c.deleted_at IS NULL
                              {loc_filter} AND {reachable_sql}"""),
                    params).scalar_one()
            return {
                "metric_version": "client-cohorts-v2",
                "as_of": moment.isoformat(),
                "source_date_basis": "visit and appointment local date in each saved appointment timezone",
                "timezone": "appointment local_timezone snapshot",
                "currency": None,
                "source": "clients, visits, appointments, visit_services, rebooking_cycles, consents, client_contacts, suppressions, client_duplicate_candidates, imports",
                "generated_at": snapshot_at.isoformat(),
                "filters": {"location_id": str(location_id) if location_id else None},
                "cohorts": cohort_rows,
                "clients": client_facts,
                "segments": {"due_cycles": due_cycles, **inactive},
                "consent_reachability": {
                    "reachable_clients": int(reachable_clients),
                    "listed_clients": len(client_facts),
                    "client_list_limit": 200},
                "data_quality": {
                    "clients_without_contacts": no_contact,
                    "clients_without_primary": no_primary,
                    "unverified_primaries": unverified_primary,
                    "open_duplicate_candidates": open_dupes,
                    "import_error_rows": (int(import_errors or 0)
                                           if import_errors is not None else None),
                    "import_error_scope": (
                        "tenant-wide; all-location owner report"
                        if import_errors is not None else
                        "unavailable for this location scope; import records have no location key"),
                    "clients_by_source": {r["source"]: r["count"] for r in by_source}},
                "definitions": {
                    "cohort": "First completed-visit month; 'never' visited separately.",
                    "repeaters": "Cohort clients with 2+ completed visits.",
                    "avg_return_days": "Mean first-to-second visit gap among repeaters; "
                                       "null without repeaters.",
                    "immature": "Cohort younger than 90 days: return figures provisional.",
                    "reachable": "Current latest unexpired marketing grant matches the saved contact and has no channel suppression; as of generated_at.",
                    "inactive_30/60/90d": "No completed visit in the last N local calendar days through as_of; never-visited clients count.",
                    "due_cycles": "Currently due rebooking cycles for non-deleted clients; as of generated_at.",
                    "return_interval_days": "Calendar days from the first to second completed visit through as_of; null without two visits.",
                },
            }
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/reports/marketing", tags=["reports"])
    def marketing_report(tenant_id: uuid.UUID, date_from: date, date_to: date,
                         identity=Depends(require_identity)) -> dict:
        """Campaign and message cost performance (FR-061/FR-062).

        Delivery, failure, unsubscribes, campaign bookings, automation
        conversion, closure resolution and costs — each from ledger rows with
        its denominator stated. Accepted-but-never-confirmed provider sends
        are labelled unknown delivery, never successes; stale projections say
        so. Nothing here measures opens, and nothing claims revenue lift.
        """
        from modules.segments import resolve_segment
        if date_to < date_from:
            raise HTTPException(status_code=422, detail={"code": "inverted_range"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "reports.read")
                start_iso, end_iso, tzname = _range_tz(
                    connection, tenant_id=str(tenant_id), locations=None,
                    day_from=date_from, day_to=date_to)
                params: dict = {"tenant": str(tenant_id),
                                "start": start_iso, "end": end_iso}

                delivery = connection.execute(
                    text("""SELECT channel, state, count(*) FROM communications
                            WHERE tenant_id=:tenant AND created_at >= :start
                              AND created_at < :end
                            GROUP BY channel, state"""),
                    params).mappings().all()
                callbacks = connection.execute(
                    text("""SELECT event, count(DISTINCT communication_id) AS count
                            FROM provider_callbacks
                            WHERE tenant_id=:tenant AND occurred_at >= :start
                              AND occurred_at < :end
                            GROUP BY event"""),
                    params).mappings().all()
                unknown_delivery = connection.execute(
                    text("""SELECT count(*) FROM communications c
                            WHERE c.tenant_id=:tenant AND c.created_at >= :start
                              AND c.created_at < :end AND c.state='dispatched'
                              AND NOT EXISTS (
                                SELECT 1 FROM provider_callbacks p
                                WHERE p.tenant_id=c.tenant_id
                                  AND p.communication_id=c.id
                                  AND p.event IN ('delivered', 'read', 'failed'))"""),
                    params).scalar_one()

                unsubs = connection.execute(
                    text("""SELECT reason, count(DISTINCT (channel, contact_value)) AS count
                            FROM suppressions
                            WHERE tenant_id=:tenant AND suppressed_at >= :start
                              AND suppressed_at < :end
                            GROUP BY reason"""),
                    params).mappings().all()
                revoked = connection.execute(
                    text("""SELECT count(*) FROM consents
                            WHERE tenant_id=:tenant AND status='revoked'
                              AND revoked_at >= :start AND revoked_at < :end"""),
                    params).scalar_one()

                campaigns = connection.execute(
                    text("""SELECT c.id::text AS id, c.name, c.channel, c.status,
                                   c.budget_minor,
                                   (SELECT count(*) FROM campaign_sends s
                                     JOIN communications cm ON cm.tenant_id=s.tenant_id
                                                           AND cm.id=s.communication_id
                                     WHERE s.tenant_id=c.tenant_id AND s.campaign_id=c.id
                                       AND s.state='sent' AND cm.created_at >= :start
                                       AND cm.created_at < :end) AS sent,
                                   (SELECT count(*) FROM campaign_sends s
                                     WHERE s.tenant_id=c.tenant_id AND s.campaign_id=c.id
                                       AND s.state LIKE 'skipped%'
                                       AND s.updated_at >= :start AND s.updated_at < :end) AS skipped,
                                   (SELECT count(DISTINCT r.appointment_id) FROM offer_redemptions r
                                     WHERE r.tenant_id=c.tenant_id AND r.campaign_id=c.id
                                       AND r.appointment_id IS NOT NULL
                                       AND r.redeemed_at >= :start AND r.redeemed_at < :end)
                                   AS attributed,
                                   (SELECT count(*) FROM offer_redemptions r
                                     WHERE r.tenant_id=c.tenant_id AND r.campaign_id=c.id
                                       AND r.redeemed_at >= :start AND r.redeemed_at < :end)
                                   AS redemptions,
                                   (SELECT COALESCE(SUM(pu.cost_minor), 0)
                                      FROM provider_usage pu
                                      JOIN campaign_sends s ON s.tenant_id=pu.tenant_id
                                                           AND s.communication_id=pu.communication_id
                                      JOIN communications cm ON cm.tenant_id=pu.tenant_id
                                                            AND cm.id=pu.communication_id
                                      WHERE s.tenant_id=c.tenant_id AND s.campaign_id=c.id
                                        AND pu.state='matched' AND cm.created_at >= :start
                                        AND cm.created_at < :end) AS provider_actual_cost_minor,
                                   (SELECT COALESCE(SUM(ur.actual_minor), 0)
                                      FROM usage_reservations ur
                                      JOIN campaign_sends s ON s.tenant_id=ur.tenant_id
                                                           AND s.communication_id=ur.communication_id
                                      JOIN communications cm ON cm.tenant_id=ur.tenant_id
                                                            AND cm.id=ur.communication_id
                                      WHERE s.tenant_id=c.tenant_id AND s.campaign_id=c.id
                                        AND ur.state='reconciled'
                                        AND cm.created_at >= :start AND cm.created_at < :end
                                        AND NOT EXISTS (SELECT 1 FROM provider_usage pu
                                          WHERE pu.tenant_id=ur.tenant_id
                                            AND pu.communication_id=ur.communication_id
                                            AND pu.state='matched')) AS estimated_cost_minor,
                                   (SELECT COALESCE(SUM(ur.estimated_minor), 0)
                                      FROM usage_reservations ur
                                      JOIN campaign_sends s ON s.tenant_id=ur.tenant_id
                                                           AND s.communication_id=ur.communication_id
                                      JOIN communications cm ON cm.tenant_id=ur.tenant_id
                                                            AND cm.id=ur.communication_id
                                      WHERE s.tenant_id=c.tenant_id AND s.campaign_id=c.id
                                        AND ur.state='reserved'
                                        AND cm.created_at >= :start AND cm.created_at < :end)
                                   AS pending_estimate_minor
                            FROM offer_campaigns c
                            WHERE c.tenant_id=:tenant AND (
                              (c.created_at >= :start AND c.created_at < :end)
                              OR EXISTS (SELECT 1 FROM campaign_sends s
                                JOIN communications cm ON cm.tenant_id=s.tenant_id
                                                      AND cm.id=s.communication_id
                                WHERE s.tenant_id=c.tenant_id AND s.campaign_id=c.id
                                  AND cm.created_at >= :start AND cm.created_at < :end)
                              OR EXISTS (SELECT 1 FROM campaign_sends s
                                WHERE s.tenant_id=c.tenant_id AND s.campaign_id=c.id
                                  AND s.updated_at >= :start AND s.updated_at < :end)
                              OR EXISTS (SELECT 1 FROM offer_redemptions r
                                WHERE r.tenant_id=c.tenant_id AND r.campaign_id=c.id
                                  AND r.redeemed_at >= :start AND r.redeemed_at < :end))
                            ORDER BY c.created_at"""),
                    params).mappings().all()

                prompts_sent = connection.execute(
                    text("""SELECT count(*) FROM rebooking_prompts p
                            JOIN rebooking_cycles c ON c.tenant_id=p.tenant_id
                                                   AND c.id=p.cycle_id
                            WHERE p.tenant_id=:tenant AND p.sent_at >= :start
                              AND p.sent_at < :end"""),
                    params).scalar_one()
                prompts_booked = connection.execute(
                    text("""SELECT count(*) FROM rebooking_cycles
                            WHERE tenant_id=:tenant AND status='booked'
                              AND attributed_communication_id IS NOT NULL
                              AND updated_at >= :start AND updated_at < :end"""),
                    params).scalar_one()

                closures = connection.execute(
                    text("""SELECT status, resolution, count(*) FROM closure_impacts
                            WHERE tenant_id=:tenant
                            GROUP BY status, resolution"""),
                    {"tenant": str(tenant_id)}).mappings().all()

                costs = connection.execute(
                    text("""WITH provider_costs AS (
                                SELECT tenant_id, communication_id,
                                       SUM(cost_minor) AS provider_actual_minor
                                FROM provider_usage WHERE state='matched'
                                GROUP BY tenant_id, communication_id)
                            SELECT c.channel,
                                   COALESCE(SUM(CASE WHEN pc.provider_actual_minor IS NOT NULL
                                     THEN pc.provider_actual_minor ELSE 0 END), 0) AS provider_actual_minor,
                                   COALESCE(SUM(CASE WHEN pc.provider_actual_minor IS NULL
                                      AND ur.state='reconciled' THEN ur.actual_minor ELSE 0 END), 0)
                                      AS estimated_minor,
                                   COALESCE(SUM(CASE WHEN ur.state='reserved'
                                      THEN ur.estimated_minor ELSE 0 END), 0) AS pending_estimate_minor,
                                   count(DISTINCT c.id) AS messages
                            FROM communications c
                            LEFT JOIN provider_costs pc ON pc.tenant_id=c.tenant_id
                                                       AND pc.communication_id=c.id
                            LEFT JOIN usage_reservations ur ON ur.tenant_id=c.tenant_id
                                                          AND ur.communication_id=c.id
                            WHERE c.tenant_id=:tenant AND c.created_at >= :start
                              AND c.created_at < :end
                              AND (ur.state IS NULL OR ur.state <> 'voided')
                            GROUP BY c.channel"""),
                    params).mappings().all()

                segments = connection.execute(
                    text("""SELECT id::text AS id, name FROM client_segments
                            WHERE tenant_id=:tenant ORDER BY name LIMIT 20"""),
                    {"tenant": str(tenant_id)}).mappings().all()
                audiences = []
                for segment in segments:
                    try:
                        resolved = resolve_segment(
                            connection, tenant_id=str(tenant_id),
                            segment_id=segment["id"], channel="email")
                    except Exception:
                        continue
                    audiences.append({"segment_id": segment["id"], "name": segment["name"],
                                      "candidates": resolved["candidates"],
                                      "eligible": resolved["eligible_count"],
                                      "exclusions": resolved["exclusions"]})
            generated_at = datetime.now(timezone.utc).isoformat()
            return {
                "metric_version": "marketing-performance-v2",
                "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                "timezone": tzname,
                "generated_at": generated_at,
                "freshness": {"source": "canonical PostgreSQL tables",
                              "stale": False, "age_seconds": 0,
                              "generated_at": generated_at,
                              "definition": "Computed on request; no cached projection."},
                "delivery_by_channel_state": [
                    {"channel": r["channel"], "state": r["state"], "count": r["count"]}
                    for r in delivery],
                "provider_events": {r["event"]: r["count"] for r in callbacks},
                "unknown_delivery": unknown_delivery,
                "unsubscribes_by_reason": {r["reason"]: r["count"] for r in unsubs},
                "consents_revoked": revoked,
                "campaigns": [{**dict(c)} for c in campaigns],
                "automation": {"rebooking_prompts_sent": prompts_sent,
                               "prompt_attributed_bookings": prompts_booked,
                               "prompt_conversion": (round(prompts_booked / prompts_sent, 4)
                                                     if prompts_sent else None)},
                "closure_resolution": [
                    {"status": r["status"], "resolution": r["resolution"],
                     "count": r["count"]} for r in closures],
                "message_costs": {"by_channel": [
                    {**dict(r),
                     "provider_actual_minor": int(r["provider_actual_minor"] or 0),
                     "estimated_minor": int(r["estimated_minor"] or 0),
                     "pending_estimate_minor": int(r["pending_estimate_minor"] or 0),
                     "messages": int(r["messages"])} for r in costs],
                                  "currency": "CAD"},
                "audiences": audiences,
                "definitions": {
                    "delivery": "Accepted/delivered/read/failed are distinct communications with provider evidence by callback occurrence time; outbox state counts are communications created in range.",
                    "unknown_delivery": "Dispatched with no delivered/read/failed "
                                        "evidence: labelled, never counted as delivered.",
                    "prompt_conversion": "Prompt-attributed bookings / prompts sent; "
                                         "descriptive attribution, not causal lift.",
                    "costs": "Provider-reconciled usage is separate from application estimates and still-held estimates; unmatched provider usage is not invented as a charge. Amounts are CAD minor units by communication creation time.",
                },
            }
        finally:
            engine.dispose()
