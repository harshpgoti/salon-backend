"""Service rebooking cycles and deterministic rules (FR-049/FR-050; TASK-035/036).

Cycles (TASK-035): a completed service line creates exactly one actionable
fact — "client C is due for service S on date D" — computed deterministically
from the completion date plus the interval in force at that moment.

Rules (TASK-036): the owner configures one rule per service (interval,
channel, local send time, follow-ups) and previews its effect before
activating: how many due clients are eligible, how many are suppressed or
unreachable, and what the prompts would cost. The rule schema is fixed
columns — there is no script field, and unknown fields are rejected — and
references resolve only to tenant-owned rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

__all__ = [
    "create_cycles_for_visit",
    "preview_rule",
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


# Synthetic per-message CAD minor-unit rates for previews (match the ledger).
CHANNEL_RATES = {"sms": 1, "whatsapp": 1, "email": 0}


def preview_rule(connection, *, tenant_id: str, rule_id: str) -> dict:
    """Preview a rule deterministically: due/eligible/suppressed/unreachable/cost.

    Pure function of current rows — same data, same preview. A due client is
    eligible when marketing consent for the rule channel is current and the
    primary contact is not suppressed on it; suppressed when consent lapsed or
    the contact opted out; unreachable when no primary contact exists. Cost
    covers the prompt plus the configured follow-ups at the channel rate.
    """
    from modules.communications.consent import check_consented, check_suppressed
    rule = connection.execute(
        text("""SELECT r.id::text AS id, r.service_id::text AS service_id,
                       s.name AS service_name, r.interval_days, r.channel,
                       r.send_time_local, r.max_follow_ups, r.is_active, r.version
                FROM rebooking_rules r
                JOIN services s ON s.tenant_id=r.tenant_id AND s.id=r.service_id
                WHERE r.tenant_id=:tenant AND r.id=:id"""),
        {"tenant": tenant_id, "id": rule_id}).mappings().one_or_none()
    if rule is None:
        raise RetentionModuleError("unknown rebooking rule")
    due = connection.execute(
        text("""SELECT c.client_id::text AS client_id FROM rebooking_cycles c
                WHERE c.tenant_id=:tenant AND c.service_id=:service AND c.status='due'"""),
        {"tenant": tenant_id, "service": rule["service_id"]}).mappings().all()
    eligible = suppressed = unreachable = 0
    for row in due:
        contact = connection.execute(
            text("""SELECT id::text AS id, kind, normalized_value FROM client_contacts
                    WHERE tenant_id=:tenant AND client_id=:client
                    ORDER BY is_primary DESC, created_at LIMIT 1"""),
            {"tenant": tenant_id, "client": row["client_id"]}).mappings().one_or_none()
        if contact is None:
            unreachable += 1
            continue
        consented = check_consented(
            connection, tenant_id=tenant_id, client_id=row["client_id"],
            channel=rule["channel"], purpose="marketing")
        blocked = check_suppressed(
            connection, tenant_id=tenant_id, contact_value=contact["normalized_value"],
            channel=rule["channel"])
        if consented and not blocked:
            eligible += 1
        else:
            suppressed += 1
    sends = eligible * (1 + rule["max_follow_ups"])
    return {"rule_id": rule["id"], "service_name": rule["service_name"],
            "channel": rule["channel"], "is_active": rule["is_active"],
            "due_total": len(due), "eligible": eligible, "suppressed": suppressed,
            "unreachable": unreachable,
            "estimated_sends": sends,
            "estimated_cost_minor": sends * CHANNEL_RATES[rule["channel"]],
            "currency": "CAD"}


class RetentionModuleError(Exception):
    """Base exception for retention operations."""


class RuleIn(BaseModel):
    # Fixed columns only: unknown fields (a script, an expression, anything
    # else) are rejected, never stored or executed.
    model_config = ConfigDict(extra="forbid")

    service_id: uuid.UUID
    interval_days: int = Field(ge=1, le=730)
    channel: str = Field(pattern="^(email|sms|whatsapp)$")
    send_time_local: str = Field(pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$")
    max_follow_ups: int = Field(ge=0, le=2)
    segment_id: uuid.UUID | None = None


class RulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    interval_days: int | None = Field(default=None, ge=1, le=730)
    channel: str | None = Field(default=None, pattern="^(email|sms|whatsapp)$")
    send_time_local: str | None = Field(default=None, pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$")
    max_follow_ups: int | None = Field(default=None, ge=0, le=2)


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

    @app.post("/v1/tenants/{tenant_id}/rebooking-rules", tags=["retention"], status_code=201)
    def create_rule(tenant_id: uuid.UUID, payload: RuleIn,
                    request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.manage")
                try:
                    row = connection.execute(
                        text("""INSERT INTO rebooking_rules
                                (tenant_id, id, service_id, interval_days, channel,
                                 send_time_local, max_follow_ups, segment_id)
                                VALUES (:tenant, :id, :service, :interval, :channel,
                                        :send_at, :follow_ups, :segment)
                                RETURNING id::text AS id, version, is_active"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                         "service": str(payload.service_id), "interval": payload.interval_days,
                         "channel": payload.channel, "send_at": payload.send_time_local,
                         "follow_ups": payload.max_follow_ups,
                         "segment": str(payload.segment_id) if payload.segment_id else None},
                    ).mappings().one()
                except IntegrityError as exc:
                    raise HTTPException(status_code=422, detail={
                        "code": "invalid_rule_reference",
                        "message": "service unknown to this salon, duplicate rule, "
                                   "or audience segments are not available yet"})
                insert_audit(connection, tenant_id, identity, action="retention.rule_created",
                             resource_type="rebooking_rule", resource_id=row["id"],
                             reason=f"{payload.channel} every {payload.interval_days}d",
                             request_id=request.state.request_id,
                             changed_fields=["service_id", "interval_days", "channel"])
            return dict(row)
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/rebooking-rules/{rule_id}", tags=["retention"])
    def update_rule(tenant_id: uuid.UUID, rule_id: uuid.UUID, payload: RulePatch,
                    request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.manage")
                changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
                if not changes:
                    raise HTTPException(status_code=422, detail={"code": "rule_change_required"})
                current = connection.execute(
                    text("""SELECT version FROM rebooking_rules
                            WHERE tenant_id=:tenant AND id=:id FOR UPDATE"""),
                    {"tenant": str(tenant_id), "id": str(rule_id)}).scalar_one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "rule_not_found"})
                if payload.expected_version != current:
                    raise HTTPException(status_code=409, detail={"code": "rule_version_conflict"})
                assignments = ", ".join(f"{key}=:{key}" for key in changes)
                row = connection.execute(
                    text(f"""UPDATE rebooking_rules SET {assignments},
                                   version=version+1, updated_at=now()
                             WHERE tenant_id=:tenant AND id=:id
                             RETURNING id::text AS id, version, is_active"""),
                    {"tenant": str(tenant_id), "id": str(rule_id), **changes},
                ).mappings().one()
                insert_audit(connection, tenant_id, identity, action="retention.rule_updated",
                             resource_type="rebooking_rule", resource_id=row["id"],
                             reason="rule updated",
                             request_id=request.state.request_id,
                             changed_fields=list(changes.keys()))
            return dict(row)
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/rebooking-rules/{rule_id}/preview", tags=["retention"])
    def preview_existing_rule(tenant_id: uuid.UUID, rule_id: uuid.UUID,
                              identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.manage")
                try:
                    return preview_rule(connection, tenant_id=str(tenant_id),
                                        rule_id=str(rule_id))
                except RetentionModuleError as exc:
                    raise HTTPException(status_code=404, detail={"code": "rule_not_found",
                                                                 "message": str(exc)})
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/rebooking-rules/{rule_id}/activate", tags=["retention"])
    def activate_rule(tenant_id: uuid.UUID, rule_id: uuid.UUID,
                      request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.manage")
                row = connection.execute(
                    text("""UPDATE rebooking_rules SET is_active=true,
                                   version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id
                            RETURNING id::text AS id, is_active, version"""),
                    {"tenant": str(tenant_id), "id": str(rule_id)}).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={"code": "rule_not_found"})
                insert_audit(connection, tenant_id, identity, action="retention.rule_activated",
                             resource_type="rebooking_rule", resource_id=row["id"],
                             reason="rule activated",
                             request_id=request.state.request_id,
                             changed_fields=["is_active"])
            return dict(row)
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/rebooking-rules", tags=["retention"])
    def list_rules(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.manage")
                rows = connection.execute(
                    text("""SELECT r.id::text AS id, r.service_id::text AS service_id,
                                   s.name AS service_name, r.interval_days, r.channel,
                                   r.send_time_local::text AS send_time_local,
                                   r.max_follow_ups, r.is_active, r.version
                            FROM rebooking_rules r
                            JOIN services s ON s.tenant_id=r.tenant_id AND s.id=r.service_id
                            WHERE r.tenant_id=:tenant ORDER BY s.name"""),
                    {"tenant": str(tenant_id)}).mappings().all()
            return {"items": [dict(r) for r in rows]}
        finally:
            engine.dispose()
