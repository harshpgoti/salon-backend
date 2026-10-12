"""Operational dashboards and reports (FR-058; TASK-041 opens the set).

The Today endpoint is the front-desk surface: one call returns the salon's
actionable work for a date — today's appointments by state, pending
approvals, cancellations and no-shows, affected closures, staff availability,
and the delivery/follow-up tasks that need a human. Everything is
tenant-scoped; staff roles additionally narrow to their assigned locations
(owners see all), and every list carries the generation timestamp so stale
screens are recognizable.
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
