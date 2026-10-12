"""Closure impact resolution (FR-025, FR-026, TASK-026) and notices (TASK-033).

When a closure exception is published, existing appointments that overlap
with the closure are tracked so owners can explicitly choose to reschedule,
cancel, or leave them. A booking never vanishes automatically.

TASK-033 notices: recorded impacts notify their client (transactional
closure_notice, immediate priority); broader location audiences follow
purpose policy (marketing consent required, no fallback, suppressed contacts
skipped). Unreachable impact clients open a staff follow-up task instead of
an unlawful fallback send; broadcasts leave one summary task with the
unreachable count.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

CLOSE_STATUSES = ("active", "rescheduled", "cancelled", "left")


class ClosureImpactIn(BaseModel):
    appointment_id: uuid.UUID
    location_id: uuid.UUID
    exception_date: date
    status: str = "active"


class ResolveImpactIn(BaseModel):
    impact_id: uuid.UUID
    resolution: str = Field(pattern=r"^(active|rescheduled|cancelled|left)$")
    resolved_by: uuid.UUID
    reason: str | None = Field(default=None, max_length=240)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "appointment_id": "a",
                    "location_id": "b",
                    "exception_date": "2026-12-25",
                }
            ]
        }
    }


class ClosureImpactOut(BaseModel):
    id: str
    appointment_id: str
    location_id: str
    exception_date: str
    status: str
    resolution: str | None = None
    resolved_at: str | None = None
    resolved_by: str | None = None
    resolution_reason: str | None = None
    client_id: str
    staff_id: str | None = None
    starts_at: str
    ends_at: str

    model_config = {"from_attributes": True}


def register_closure_impacts_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    """Register all closure-impact endpoints under /v1/tenants/{tenant_id}."""

    @app.post("/v1/tenants/{tenant_id}/closures/{location_id}/impacts", tags=["closure"])
    def record_closure_impact(
        tenant_id: uuid.UUID, location_id: uuid.UUID, payload: ClosureImpactIn,
        request: Request, identity=Depends(require_identity),
    ) -> dict:
        """Record which appointments are affected by a published closure."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "closures.manage")
                exception = connection.execute(
                    text("""SELECT id FROM location_exceptions
                            WHERE tenant_id=:tenant AND location_id=:location
                              AND exception_date=:date AND published_at IS NOT NULL"""),
                    {"tenant": str(tenant_id), "location": str(location_id),
                     "date": str(payload.exception_date)}).mappings().one_or_none()
                if exception is None:
                    raise HTTPException(status_code=422, detail={"code": "exception_not_published"})

                appt = connection.execute(
                    text("""SELECT a.id, a.client_id, a.staff_id, a.starts_at, a.ends_at
                            FROM appointments a WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(payload.appointment_id)}).mappings().one_or_none()
                if appt is None:
                    raise HTTPException(status_code=404, detail={"code": "appointment_not_found"})

                impacted = connection.execute(
                    text("""INSERT INTO closure_impacts (
                            tenant_id, location_id, exception_date, appointment_id,
                            client_id, staff_id, starts_at, ends_at, status
                          ) VALUES (
                            :tenant, :location, :date, :appointment,
                            :client, :staff, :starts, :ends, :status
                          )
                          ON CONFLICT (tenant_id, location_id, exception_date, appointment_id)
                          DO UPDATE SET
                            status = EXCLUDED.status,
                            version = closure_impacts.version + 1,
                            updated_at = now()
                          RETURNING *"""),
                    {"tenant": str(tenant_id), "location": str(location_id),
                     "date": str(payload.exception_date),
                     "appointment": str(payload.appointment_id),
                     "client": str(appt["client_id"]),
                     "staff": str(appt["staff_id"]) if appt["staff_id"] else None,
                     "starts": str(appt["starts_at"]),
                     "ends": str(appt["ends_at"]),
                     "status": payload.status}).mappings().one()
                insert_audit(connection, tenant_id, identity,
                             action="closure_impact.recorded", resource_type="closure",
                             resource_id=str(location_id),
                             reason=f"exception {payload.exception_date} affected appointment {payload.appointment_id}",
                             request_id=request.state.request_id, changed_fields=["status"])
                return {
                    "id": str(impacted["id"]), "appointment_id": str(impacted["appointment_id"]),
                    "location_id": str(impacted["location_id"]),
                    "exception_date": str(impacted["exception_date"]),
                    "status": impacted["status"], "client_id": str(impacted["client_id"]),
                    "staff_id": str(impacted["staff_id"]) if impacted["staff_id"] else None,
                    "starts_at": str(impacted["starts_at"]), "ends_at": str(impacted["ends_at"]),
                }
        finally:
            engine.dispose()


    @app.post("/v1/tenants/{tenant_id}/closures/{location_id}/impacts/resolve", tags=["closure"])
    def resolve_closure_impact(
        tenant_id: uuid.UUID, location_id: uuid.UUID, payload: ResolveImpactIn,
        request: Request, identity=Depends(require_identity),
    ) -> dict:
        """Resolve an impact by setting its status (cancelled, rescheduled, left)."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "closures.manage")
                impact = connection.execute(
                    text("""SELECT id, appointment_id, status, version
                            FROM closure_impacts
                            WHERE tenant_id=:tenant AND id=:impact_id"""),
                    {"tenant": str(tenant_id), "impact_id": str(payload.impact_id)}).mappings().one_or_none()
                if impact is None:
                    raise HTTPException(status_code=404, detail={"code": "impact_not_found"})
                if impact["status"] != "active":
                    raise HTTPException(status_code=409, detail={"code": "impact_already_resolved"})

                updated = connection.execute(
                    text("""UPDATE closure_impacts
                            SET status=:resolution, resolution=:resolution,
                                resolved_at=now(), resolved_by=:resolved_by,
                                resolution_reason=:reason, version=version+1,
                                updated_at=now()
                            WHERE tenant_id=:tenant AND id=:impact_id
                            RETURNING *"""),
                    {"tenant": str(tenant_id), "impact_id": str(payload.impact_id),
                     "resolution": payload.resolution, "resolved_by": str(payload.resolved_by),
                     "reason": payload.reason}).mappings().one()
                insert_audit(connection, tenant_id, identity,
                             action="closure_impact.resolved", resource_type="closure",
                             resource_id=str(location_id),
                             reason=f"impact {payload.impact_id} resolved as {payload.resolution}",
                             request_id=request.state.request_id, changed_fields=["status", "resolution"])
                return {
                    "id": str(updated["id"]), "appointment_id": str(updated["appointment_id"]),
                    "status": updated["status"], "resolution": updated["resolution"],
                    "resolved_at": str(updated["resolved_at"]),
                    "resolved_by": str(updated["resolved_by"]),
                    "resolution_reason": str(updated["resolution_reason"]),
                }
        finally:
            engine.dispose()
    @app.get("/v1/tenants/{tenant_id}/closures/{location_id}/impacts/preview", tags=["closure"])
    def preview_closure_impacts(
        tenant_id: uuid.UUID, location_id: uuid.UUID,
        exception_date: date = Query(...),
        identity=Depends(require_identity),
    ) -> dict:
        """Find live appointments overlapping a closure date, before recording.

        AC-1 lists the appointments a published closure affects so each can be
        resolved explicitly. Only bookable states count — completed, cancelled,
        no-show and rejected history is never an impact. Days are compared in
        the location's own timezone. Already-recorded appointments carry their
        impact id so the UI can skip them.
        """
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "closures.read")
                rows = connection.execute(
                    text("""SELECT a.id::text AS appointment_id, a.client_id::text AS client_id,
                                   a.staff_id::text AS staff_id, a.status,
                                   a.starts_at, a.ends_at,
                                   ci.id::text AS impact_id, ci.status AS impact_status
                            FROM appointments a
                            JOIN locations l ON l.tenant_id=a.tenant_id
                                            AND l.id=a.location_id
                            LEFT JOIN closure_impacts ci
                              ON ci.tenant_id=a.tenant_id
                             AND ci.appointment_id=a.id
                             AND ci.location_id=:location
                             AND ci.exception_date=:date
                            WHERE a.tenant_id=:tenant AND a.location_id=:location
                              AND (a.starts_at AT TIME ZONE l.timezone)::date = :date
                              AND a.status IN ('pending_approval', 'confirmed',
                                               'checked_in', 'in_service')
                            ORDER BY a.starts_at"""),
                    {"tenant": str(tenant_id), "location": str(location_id),
                     "date": str(exception_date)}).mappings().all()
                items = [
                    {"appointment_id": r["appointment_id"], "client_id": r["client_id"],
                     "staff_id": r["staff_id"], "status": r["status"],
                     "starts_at": r["starts_at"].isoformat(), "ends_at": r["ends_at"].isoformat(),
                     "impact_id": r["impact_id"], "impact_status": r["impact_status"]}
                    for r in rows
                ]
                return {"items": items, "total": len(items)}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/closures/{location_id}/impacts", tags=["closure"])
    def list_closure_impacts(
        tenant_id: uuid.UUID, location_id: uuid.UUID,
        exception_date: date = Query(...),
        include_resolved: bool = Query(False),
        identity=Depends(require_identity),
    ) -> dict:
        """List all impacts for a given closure exception date."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "closures.read")
                where_clause = (
                    "WHERE tenant_id=:tenant AND location_id=:location AND exception_date=:date"
                )
                if not include_resolved:
                    where_clause += " AND status = 'active'"
                rows = connection.execute(
                    text(f"""SELECT id, appointment_id, location_id, exception_date,
                               status, resolution, resolved_at, resolved_by,
                               resolution_reason, client_id, staff_id, starts_at, ends_at
                          FROM closure_impacts
                          {where_clause}
                          ORDER BY exception_date, starts_at"""),
                    {"tenant": str(tenant_id), "location": str(location_id),
                     "date": str(exception_date)}).mappings().all()
                impacts = [
                    {
                        "id": str(r["id"]), "appointment_id": str(r["appointment_id"]),
                        "location_id": str(r["location_id"]), "exception_date": str(r["exception_date"]),
                        "status": r["status"], "resolution": r["resolution"],
                        "resolved_at": str(r["resolved_at"]) if r["resolved_at"] else None,
                        "resolved_by": str(r["resolved_by"]) if r["resolved_by"] else None,
                        "resolution_reason": r["resolution_reason"],
                        "client_id": str(r["client_id"]),
                        "staff_id": str(r["staff_id"]) if r["staff_id"] else None,
                        "starts_at": str(r["starts_at"]), "ends_at": str(r["ends_at"]),
                    }
                    for r in rows
                ]
                return {"impacts": impacts, "total": len(impacts)}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/closures/{location_id}/impacts/unsent", tags=["closure"])
    def list_unsent_closure_impacts(
        tenant_id: uuid.UUID, location_id: uuid.UUID,
        identity=Depends(require_identity),
    ) -> dict:
        """List all active, unresolved impacts across all closure dates for follow-up.

        AC-2: each item carries reachability — the client's primary contact, the
        channels it is suppressed on, and whether staff must follow up by other
        means (no usable contact, or every channel suppressed). Resolving an
        impact removes it from this list, so the list always reflects explicit
        decisions.
        """
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "closures.read")
                rows = connection.execute(
                    text("""SELECT id, appointment_id, location_id, exception_date,
                                status, resolution, resolved_at, resolved_by,
                                resolution_reason, client_id, staff_id, starts_at, ends_at
                           FROM closure_impacts
                           WHERE tenant_id=:tenant AND location_id=:location
                             AND status = 'active'
                           ORDER BY exception_date, starts_at"""),
                    {"tenant": str(tenant_id), "location": str(location_id)}).mappings().all()
                impacts = []
                for r in rows:
                    contact = connection.execute(
                        text("""SELECT kind, normalized_value FROM client_contacts
                                WHERE tenant_id=:tenant AND client_id=:client
                                ORDER BY is_primary DESC, created_at LIMIT 1"""),
                        {"tenant": str(tenant_id),
                         "client": str(r["client_id"])}).mappings().one_or_none()
                    suppressed: list[str] = []
                    if contact is not None:
                        suppressed = connection.execute(
                            text("""SELECT channel FROM suppressions
                                    WHERE tenant_id=:tenant
                                      AND contact_value=:value"""),
                            {"tenant": str(tenant_id),
                             "value": contact["normalized_value"]}).scalars().all()
                    reachable = contact is not None and (
                        (contact["kind"] == "email" and "email" not in suppressed)
                        or (contact["kind"] == "phone"
                            and ("sms" not in suppressed or "whatsapp" not in suppressed)))
                    impacts.append(
                        {
                            "id": str(r["id"]), "appointment_id": str(r["appointment_id"]),
                            "location_id": str(r["location_id"]), "exception_date": str(r["exception_date"]),
                            "status": r["status"], "resolution": r["resolution"],
                            "resolved_at": str(r["resolved_at"]) if r["resolved_at"] else None,
                            "resolved_by": str(r["resolved_by"]) if r["resolved_by"] else None,
                            "resolution_reason": r["resolution_reason"],
                            "client_id": str(r["client_id"]),
                            "staff_id": str(r["staff_id"]) if r["staff_id"] else None,
                            "starts_at": str(r["starts_at"]), "ends_at": str(r["ends_at"]),
                            "contact_kind": contact["kind"] if contact else None,
                            "suppressed_channels": sorted(suppressed),
                            "reachable": reachable,
                            "follow_up": not reachable,
                        }
                    )
                return {"unsent_impacts": impacts, "total": len(impacts)}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/closures/{location_id}/impacts/{impact_id}/notify",
              tags=["closure"])
    def notify_impact(tenant_id: uuid.UUID, location_id: uuid.UUID, impact_id: uuid.UUID,
                      request: Request, identity=Depends(require_identity),
                      template_id: uuid.UUID | None = None) -> dict:
        """Notify one impact's client now (affected bookings prioritized).

        Records a transactional closure_notice for immediate dispatch. When
        the client has no usable contact or the channel is suppressed, no
        fallback fires (AC-2: never unlawful) — a closure_follow_up task
        opens for staff instead.
        """
        from modules.communications.consent import check_suppressed
        from modules.communications.outbox import record_booking_communication
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                impact = connection.execute(
                    text("""SELECT id::text AS id, appointment_id::text AS appointment_id,
                                   client_id::text AS client_id, status
                            FROM closure_impacts
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(impact_id)}).mappings().one_or_none()
                if impact is None:
                    raise HTTPException(status_code=404, detail={"code": "impact_not_found"})
                contact = connection.execute(
                    text("""SELECT kind, normalized_value FROM client_contacts
                            WHERE tenant_id=:tenant AND client_id=:client
                            ORDER BY is_primary DESC, created_at LIMIT 1"""),
                    {"tenant": str(tenant_id),
                     "client": impact["client_id"]}).mappings().one_or_none()
                channel = "email" if contact is None or contact["kind"] == "email" else "sms"
                blocked = contact is None or check_suppressed(
                    connection, tenant_id=str(tenant_id),
                    contact_value=contact["normalized_value"], channel=channel)
                if blocked:
                    task = connection.execute(
                        text("""INSERT INTO follow_up_tasks
                                (tenant_id, id, client_id, kind, title, detail,
                                 source_type, source_id)
                                VALUES (:tenant, :id, :client, 'closure_follow_up',
                                        'Closure notice unreachable',
                                        'No usable contact: staff must call or see the client.',
                                        'closure_impact', :source)
                                ON CONFLICT (tenant_id, source_type, source_id) DO NOTHING
                                RETURNING id::text AS id"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                         "client": impact["client_id"], "source": impact["id"]},
                    ).mappings().one_or_none()
                    insert_audit(connection, tenant_id, identity,
                                 action="closure_impact.follow_up_opened",
                                 resource_type="closure", resource_id=str(location_id),
                                 reason=f"impact {impact_id} unreachable, no fallback sent",
                                 request_id=request.state.request_id,
                                 changed_fields=["follow_up"])
                    return {"notified": False, "follow_up_task_id":
                            task["id"] if task else None}
                version = connection.execute(
                    text("SELECT version FROM appointments WHERE tenant_id=:t AND id=:a"),
                    {"t": str(tenant_id), "a": impact["appointment_id"]}).scalar_one_or_none()
                row = record_booking_communication(
                    connection, tenant_id=str(tenant_id),
                    appointment_id=impact["appointment_id"], client_id=impact["client_id"],
                    contact_value=contact["normalized_value"], channel=channel,
                    purpose="closure_notice", entity_version=version or 1,
                    template_id=str(template_id) if template_id else None)
                insert_audit(connection, tenant_id, identity,
                             action="closure_impact.notified",
                             resource_type="closure", resource_id=str(location_id),
                             reason=f"impact {impact_id} notice recorded",
                             request_id=request.state.request_id,
                             changed_fields=["notified"])
            return {"notified": True, "communication_id": row["id"] if row else None}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/closures/{location_id}/notify-audience",
              tags=["closure"])
    def notify_audience(tenant_id: uuid.UUID, location_id: uuid.UUID,
                        request: Request, identity=Depends(require_identity),
                        channel: str = "email") -> dict:
        """Notice a location's clients under purpose policy (AC-1/AC-2).

        Location-wide closure news is treated as marketing: only currently
        consented, unsuppressed clients are recorded — no fallback, no silent
        expansion. Suppressed and unreachable contacts are counted, not
        messaged, and one summary follow-up task carries the count for staff.
        """
        from modules.communications.consent import check_consented, check_suppressed
        from modules.communications.outbox import record_booking_communication
        if channel not in ("email", "sms", "whatsapp"):
            raise HTTPException(status_code=422, detail={"code": "unknown_channel"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                clients = connection.execute(
                    text("""SELECT DISTINCT c.id::text AS client_id FROM clients c
                            WHERE c.tenant_id=:tenant AND c.deleted_at IS NULL
                              AND (c.preferred_location_id=:loc OR EXISTS (
                                     SELECT 1 FROM visits v
                                     WHERE v.tenant_id=c.tenant_id AND v.client_id=c.id
                                       AND v.location_id=:loc))"""),
                    {"tenant": str(tenant_id), "loc": str(location_id)}).mappings().all()
                recorded = skipped_consent = skipped_suppressed = unreachable = 0
                for entry in clients:
                    contact = connection.execute(
                        text("""SELECT normalized_value FROM client_contacts
                                WHERE tenant_id=:tenant AND client_id=:client
                                ORDER BY is_primary DESC, created_at LIMIT 1"""),
                        {"tenant": str(tenant_id),
                         "client": entry["client_id"]}).mappings().one_or_none()
                    if contact is None:
                        unreachable += 1
                        continue
                    # Suppression is the stronger signal: an explicit opt-out
                    # sorts before a missing grant.
                    if check_suppressed(connection, tenant_id=str(tenant_id),
                                        contact_value=contact["normalized_value"],
                                        channel=channel):
                        skipped_suppressed += 1
                        continue
                    if not check_consented(
                            connection, tenant_id=str(tenant_id), client_id=entry["client_id"],
                            channel=channel, purpose="marketing"):
                        skipped_consent += 1
                        continue
                    # Deprioritized behind impact notices: broadcasts wait an
                    # hour while affected bookings go now.
                    row = record_booking_communication(
                        connection, tenant_id=str(tenant_id),
                        appointment_id=str(location_id),
                        client_id=entry["client_id"],
                        contact_value=contact["normalized_value"], channel=channel,
                        purpose="closure_broadcast", entity_version=1,
                        source_type="location_broadcast",
                        scheduled_at=datetime.now(timezone.utc) + timedelta(hours=1))
                    if row is not None:
                        recorded += 1
                task = connection.execute(
                    text("""INSERT INTO follow_up_tasks
                            (tenant_id, id, kind, title, detail,
                             source_type, source_id)
                            VALUES (:tenant, :id, 'closure_follow_up',
                                    'Closure broadcast summary',
                                    :detail, 'closure_broadcast', :source)
                            ON CONFLICT (tenant_id, source_type, source_id) DO NOTHING
                            RETURNING id::text AS id"""),
                    {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                     "detail": f"{recorded} recorded, {skipped_consent} no consent, "
                               f"{skipped_suppressed} suppressed, {unreachable} unreachable",
                     "source": str(location_id)},
                ).mappings().one_or_none()
                insert_audit(connection, tenant_id, identity,
                             action="closure.audience_notified",
                             resource_type="closure", resource_id=str(location_id),
                             reason=f"{recorded} recorded",
                             request_id=request.state.request_id,
                             changed_fields=["notified"])
            return {"recorded": recorded, "skipped_no_consent": skipped_consent,
                    "skipped_suppressed": skipped_suppressed, "unreachable": unreachable,
                    "summary_task_id": task["id"] if task else None}
        finally:
            engine.dispose()