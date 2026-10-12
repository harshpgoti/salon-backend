"""Service rebooking cycles from completed visits (FR-049; TASK-035).

A completed service line creates exactly one actionable fact — "client C is
due for service S on date D" — computed deterministically from the completion
date plus the interval in force at that moment: the client's per-service
override first, else the service's versioned default. A service with no
interval anywhere creates no cycle. Cancelled and no-show appointments
complete no visit, so they create nothing.

- AC-1: interval/override yields the deterministic due date; the interval is
  snapshotted on the row, so later rule edits never rewrite history.
- AC-2: a newer completion for the same client+service supersedes the older
  due row (no stale cycle); corrections reuse the same visit, so they create
  no second cycle either.

Prompts, snooze, dismissal and booking attribution belong to TASK-037; this
module only opens and supersedes due rows, plus the read and override seams
the UI needs.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

__all__ = [
    "create_cycles_for_visit",
    "register_retention_routes",
    "resolve_interval_days",
]


def resolve_interval_days(connection, *, tenant_id: str, client_id: str,
                          service_id: str) -> int | None:
    """Interval in force: client override, else the service default, else None."""
    override = connection.execute(
        text("""SELECT interval_days FROM client_rebooking_overrides
                WHERE tenant_id=:tenant AND client_id=:client AND service_id=:service"""),
        {"tenant": tenant_id, "client": client_id, "service": service_id},
    ).scalar_one_or_none()
    if override is not None:
        return int(override)
    return connection.execute(
        text("""SELECT rebooking_interval_days FROM services
                WHERE tenant_id=:tenant AND id=:service"""),
        {"tenant": tenant_id, "service": service_id}).scalar_one_or_none()


def create_cycles_for_visit(connection, *, tenant_id: str, visit_id: str) -> list[dict]:
    """Create one due cycle per completed service line of a visit.

    Called inside the completion transaction, after the visit rows exist. A
    prior due cycle for the same client+service is superseded first, so only
    the newest completion stays actionable. Lines whose service has no
    interval are skipped without error.
    """
    visit = connection.execute(
        text("""SELECT id::text AS id, client_id::text AS client_id,
                       appointment_id::text AS appointment_id, completed_at
                FROM visits WHERE tenant_id=:tenant AND id=:visit"""),
        {"tenant": tenant_id, "visit": visit_id}).mappings().one_or_none()
    if visit is None:
        return []
    base_date = visit["completed_at"].date() if visit["completed_at"] else date.today()
    lines = connection.execute(
        text("""SELECT service_id::text AS service_id, service_version
                FROM visit_services
                WHERE tenant_id=:tenant AND visit_id=:visit ORDER BY sequence"""),
        {"tenant": tenant_id, "visit": visit_id}).mappings().all()
    created = []
    for line in lines:
        interval = resolve_interval_days(
            connection, tenant_id=tenant_id, client_id=visit["client_id"],
            service_id=line["service_id"])
        if interval is None:
            continue
        due = base_date.fromordinal(base_date.toordinal() + int(interval))
        connection.execute(
            text("""UPDATE rebooking_cycles SET status='superseded',
                           version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND client_id=:client
                      AND service_id=:service AND status='due'"""),
            {"tenant": tenant_id, "client": visit["client_id"],
             "service": line["service_id"]})
        row = connection.execute(
            text("""INSERT INTO rebooking_cycles
                    (tenant_id, id, client_id, service_id, service_version,
                     visit_id, source_appointment_id, interval_days, due_date)
                    VALUES (:tenant, :id, :client, :service, :version,
                            :visit, :appt, :interval, :due)
                    RETURNING id::text AS id, due_date"""),
            {"tenant": tenant_id, "id": str(uuid.uuid4()), "client": visit["client_id"],
             "service": line["service_id"], "version": line["service_version"],
             "visit": visit_id, "appt": visit["appointment_id"],
             "interval": int(interval), "due": due.isoformat()},
        ).mappings().one()
        created.append({**dict(row), "client_id": visit["client_id"],
                        "service_id": line["service_id"], "interval_days": int(interval)})
    return created


class OverrideIn(BaseModel):
    client_id: uuid.UUID
    service_id: uuid.UUID
    interval_days: int = Field(ge=1, le=730)


def register_retention_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    @app.get("/v1/tenants/{tenant_id}/rebooking-cycles", tags=["retention"])
    def list_cycles(tenant_id: uuid.UUID, status: str = "due",
                    client_id: uuid.UUID | None = None,
                    identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.read")
                if status not in ("due", "booked", "snoozed", "dismissed",
                                  "superseded", "all"):
                    raise HTTPException(status_code=422, detail={"code": "invalid_status"})
                filt = "" if status == "all" else "AND c.status = :status"
                if client_id is not None:
                    filt += " AND c.client_id = :client"
                rows = connection.execute(
                    text(f"""SELECT c.id::text AS id, c.client_id::text AS client_id,
                                    cl.name AS client_name, c.service_id::text AS service_id,
                                    s.name AS service_name, c.interval_days,
                                    c.due_date, c.status
                             FROM rebooking_cycles c
                             JOIN clients cl ON cl.tenant_id=c.tenant_id AND cl.id=c.client_id
                             JOIN services s ON s.tenant_id=c.tenant_id AND s.id=c.service_id
                             WHERE c.tenant_id=:tenant {filt}
                             ORDER BY c.due_date"""),
                    {"tenant": str(tenant_id), "status": status,
                     "client": str(client_id) if client_id else None}).mappings().all()
            return {"items": [{**dict(r), "due_date": r["due_date"].isoformat()} for r in rows]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/rebooking-overrides", tags=["retention"], status_code=201)
    def set_override(tenant_id: uuid.UUID, payload: OverrideIn,
                     request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.manage")
                connection.execute(
                    text("""INSERT INTO client_rebooking_overrides
                            (tenant_id, client_id, service_id, interval_days)
                            VALUES (:tenant, :client, :service, :interval)
                            ON CONFLICT (tenant_id, client_id, service_id)
                            DO UPDATE SET interval_days=EXCLUDED.interval_days,
                                          version=client_rebooking_overrides.version+1,
                                          updated_at=now()"""),
                    {"tenant": str(tenant_id), "client": str(payload.client_id),
                     "service": str(payload.service_id), "interval": payload.interval_days})
                insert_audit(connection, tenant_id, identity, action="retention.override_set",
                             resource_type="client", resource_id=payload.client_id,
                             reason=f"service {payload.service_id} every {payload.interval_days}d",
                             request_id=request.state.request_id,
                             changed_fields=["interval_days"])
            return {"client_id": str(payload.client_id), "service_id": str(payload.service_id),
                    "interval_days": payload.interval_days}
        finally:
            engine.dispose()
