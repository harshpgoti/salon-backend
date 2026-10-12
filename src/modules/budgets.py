"""Atomic usage allowances and messaging budgets (FR-069; TASK-049).

Every dispatch reserves its estimated cost before sending and reconciles to
the ledgered actual afterwards. The budget row is locked inside the dispatch
transaction, so concurrent sends serialize per tenant: the ceiling can never
be overshot by a race, and a replayed dispatch can never reserve twice
(unique per communication).

- AC-1: reservations reconcile against the cost ledger; the tenant-approved
  spend ceiling blocks sends that would exceed it (failed/budget_exceeded —
  visible, retryable after the ceiling moves). Plan allowances report usage
  against included amounts; only the ceiling blocks.
- AC-2: provider usage lines match reservations exactly once; duplicates are
  ignored; unknown lines stay pending instead of inventing charges.

Billing period: calendar month in the tenant's primary (earliest-created)
location timezone, falling back to UTC. All spend sums use it.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from modules.communications.senders import SMS_COST_MINOR_PER_SEGMENT

__all__ = [
    "BudgetModuleError",
    "period_spend_minor",
    "reconcile_provider_usage",
    "register_budget_routes",
    "reserve_usage",
    "void_reservation",
]

CHANNEL_RATES = {"sms": SMS_COST_MINOR_PER_SEGMENT,
                 "whatsapp": SMS_COST_MINOR_PER_SEGMENT, "email": 0}


class BudgetModuleError(Exception):
    """Base exception for budget operations."""


def period_bounds(connection, *, tenant_id: str,
                  now: datetime | None = None) -> tuple[str, str, str]:
    """Calendar-month bounds in the tenant's primary location timezone.

    Returns (start_iso, end_iso, timezone). The primary location is the
    earliest-created one; a tenant with no locations yet falls back to UTC.
    """
    moment = now or datetime.now(timezone.utc)
    tzname = connection.execute(
        text("""SELECT timezone FROM locations
                WHERE tenant_id=:tenant ORDER BY created_at LIMIT 1"""),
        {"tenant": tenant_id}).scalar_one_or_none() or "UTC"
    from zoneinfo import ZoneInfo
    try:
        local = moment.astimezone(ZoneInfo(tzname))
    except Exception:
        local = moment.astimezone(ZoneInfo("UTC"))
        tzname = "UTC"
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start.isoformat(), end.isoformat(), tzname


def period_spend_minor(connection, *, tenant_id: str,
                       now: datetime | None = None) -> dict:
    """Reconciled spend this period plus the allowance snapshot."""
    start, end, tzname = period_bounds(connection, tenant_id=tenant_id, now=now)
    spent = connection.execute(
        text("""SELECT COALESCE(SUM(mc.cost_minor), 0) FROM message_costs mc
                JOIN communications c ON c.tenant_id=mc.tenant_id
                                      AND c.id=mc.communication_id
                WHERE mc.tenant_id=:tenant AND mc.recorded_at >= :start
                  AND mc.recorded_at < :end AND c.state='dispatched'"""),
        {"tenant": tenant_id, "start": start, "end": end}).scalar_one()
    reserved = connection.execute(
        text("""SELECT COALESCE(SUM(estimated_minor), 0) FROM usage_reservations
                WHERE tenant_id=:tenant AND state='reserved'"""),
        {"tenant": tenant_id}).scalar_one()
    return {"spent_minor": int(spent), "reserved_minor": int(reserved),
            "period_start": start, "period_end": end, "timezone": tzname}


def estimate_cost_minor(channel: str) -> int:
    """Pre-send estimate: one segment at the channel rate (matches ledger)."""
    return CHANNEL_RATES.get(channel, 0)


def reserve_usage(connection, *, tenant_id: str, communication_id: str,
                  channel: str, now: datetime | None = None) -> dict:
    """Hold the estimated cost; block when the ceiling would be exceeded.

    Locks the tenant budget row first, so concurrent dispatchers serialize
    here and the ceiling holds exactly. Returns the reservation, or raises
    :class:`BudgetModuleError` when the send would exceed the approved
    ceiling. No-ceiling tenants (NULL) always pass and still get a row, so
    usage reports stay complete.
    """
    estimate = estimate_cost_minor(channel)
    connection.execute(
        text("""INSERT INTO messaging_budgets (tenant_id) VALUES (:tenant)
                ON CONFLICT (tenant_id) DO NOTHING"""),
        {"tenant": tenant_id})
    ceiling = connection.execute(
        text("""SELECT spend_ceiling_minor FROM messaging_budgets
                WHERE tenant_id=:tenant FOR UPDATE"""),
        {"tenant": tenant_id}).scalar_one_or_none()
    if ceiling is not None:
        spend = period_spend_minor(connection, tenant_id=tenant_id, now=now)
        if spend["spent_minor"] + spend["reserved_minor"] + estimate > ceiling:
            raise BudgetModuleError(
                f"send would exceed the approved {ceiling}c spend ceiling "
                f"(spent {spend['spent_minor']}c, held {spend['reserved_minor']}c)")
    # A voided row (an earlier attempt that never sent) is re-held; a live
    # reserved/reconciled row is kept as-is. No exception path: the statement
    # never aborts the transaction, so replays reconcile instead of dying.
    row = connection.execute(
        text("""INSERT INTO usage_reservations
                (tenant_id, communication_id, estimated_minor)
                VALUES (:tenant, :comm, :estimate)
                ON CONFLICT (tenant_id, communication_id) DO UPDATE
                SET state='reserved', estimated_minor=EXCLUDED.estimated_minor,
                    actual_minor=NULL, updated_at=now()
                WHERE usage_reservations.state='voided'
                RETURNING communication_id::text AS communication_id,
                          estimated_minor, state"""),
        {"tenant": tenant_id, "comm": communication_id,
         "estimate": estimate}).mappings().one_or_none()
    if row is not None:
        return {**dict(row), "replayed": False}
    existing = connection.execute(
        text("""SELECT communication_id::text AS communication_id,
                       estimated_minor, state FROM usage_reservations
                WHERE tenant_id=:tenant AND communication_id=:comm"""),
        {"tenant": tenant_id, "comm": communication_id}).mappings().one()
    return {**dict(existing), "replayed": True}


def reconcile_reservation(connection, *, tenant_id: str,
                          communication_id: str) -> dict:
    """Settle a reservation to the ledgered actual after the send."""
    actual = connection.execute(
        text("""SELECT cost_minor FROM message_costs
                WHERE tenant_id=:tenant AND communication_id=:comm"""),
        {"tenant": tenant_id, "comm": communication_id}).scalar_one_or_none()
    row = connection.execute(
        text("""UPDATE usage_reservations SET state='reconciled',
                       actual_minor=:actual, updated_at=now()
                WHERE tenant_id=:tenant AND communication_id=:comm
                  AND state='reserved'
                RETURNING estimated_minor"""),
        {"tenant": tenant_id, "comm": communication_id,
         "actual": actual if actual is not None else 0}).mappings().one_or_none()
    return {"communication_id": communication_id,
            "actual_minor": actual if actual is not None else 0,
            "reconciled": row is not None}


def void_reservation(connection, *, tenant_id: str, communication_id: str) -> None:
    """Release a held estimate when a later gate blocks the send."""
    connection.execute(
        text("""UPDATE usage_reservations SET state='voided', updated_at=now()
                WHERE tenant_id=:tenant AND communication_id=:comm AND state='reserved'"""),
        {"tenant": tenant_id, "comm": communication_id})


def reconcile_provider_usage(connection, *, tenant_id: str, provider_ref: str,
                             communication_id: str | None, segments: int | None,
                             cost_minor: int | None) -> dict:
    """Match one provider usage line exactly once (AC-2).

    Known communication: the reservation settles to provider actuals and the
    line marks matched — a redelivery of the same provider ref changes
    nothing. Unknown communication: the line stays pending; no charge is
    invented and nothing is billed.
    """
    try:
        row = connection.execute(
            text("""INSERT INTO provider_usage
                    (tenant_id, id, provider_ref, communication_id, segments, cost_minor)
                    VALUES (:tenant, :id, :ref, :comm, :segments, :cost)
                    ON CONFLICT (tenant_id, provider_ref) DO NOTHING
                    RETURNING id::text AS id, state"""),
            {"tenant": tenant_id, "id": str(uuid.uuid4()), "ref": provider_ref,
             "comm": communication_id, "segments": segments, "cost": cost_minor},
        ).mappings().one_or_none()
    except IntegrityError:
        row = None
    if row is None:
        return {"provider_ref": provider_ref, "status": "duplicate"}
    if communication_id is None:
        return {"provider_ref": provider_ref, "status": "pending"}
    known = connection.execute(
        text("""SELECT 1 FROM usage_reservations
                WHERE tenant_id=:tenant AND communication_id=:comm"""),
        {"tenant": tenant_id, "comm": communication_id}).scalar_one_or_none()
    if known is None:
        return {"provider_ref": provider_ref, "status": "pending"}
    connection.execute(
        text("""UPDATE usage_reservations SET actual_minor=:cost, updated_at=now()
                WHERE tenant_id=:tenant AND communication_id=:comm"""),
        {"tenant": tenant_id, "comm": communication_id,
         "cost": cost_minor if cost_minor is not None else 0})
    connection.execute(
        text("""UPDATE provider_usage SET state='matched'
                WHERE tenant_id=:tenant AND provider_ref=:ref"""),
        {"tenant": tenant_id, "ref": provider_ref})
    return {"provider_ref": provider_ref, "status": "matched"}


class BudgetIn(BaseModel):
    spend_ceiling_minor: int | None = Field(default=None, ge=0)


def register_budget_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    @app.get("/v1/tenants/{tenant_id}/billing/budget", tags=["company"])
    def read_budget(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "team.manage")
                ceiling = connection.execute(
                    text("""SELECT spend_ceiling_minor FROM messaging_budgets
                            WHERE tenant_id=:tenant"""),
                    {"tenant": str(tenant_id)}).scalar_one_or_none()
                spend = period_spend_minor(connection, tenant_id=str(tenant_id))
            return {"spend_ceiling_minor": ceiling, **spend, "currency": "CAD"}
        finally:
            engine.dispose()

    @app.put("/v1/tenants/{tenant_id}/billing/budget", tags=["company"])
    def write_budget(tenant_id: uuid.UUID, payload: BudgetIn,
                     request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "team.manage")
                connection.execute(
                    text("""INSERT INTO messaging_budgets (tenant_id, spend_ceiling_minor)
                            VALUES (:tenant, :ceiling)
                            ON CONFLICT (tenant_id) DO UPDATE SET
                                spend_ceiling_minor=EXCLUDED.spend_ceiling_minor,
                                version=messaging_budgets.version+1, updated_at=now()"""),
                    {"tenant": str(tenant_id), "ceiling": payload.spend_ceiling_minor})
                insert_audit(connection, tenant_id, identity, action="billing.budget_set",
                             resource_type="messaging_budget", resource_id=str(tenant_id),
                             reason=f"ceiling {payload.spend_ceiling_minor}",
                             request_id=request.state.request_id,
                             changed_fields=["spend_ceiling_minor"])
                spend = period_spend_minor(connection, tenant_id=str(tenant_id))
            return {"spend_ceiling_minor": payload.spend_ceiling_minor,
                    **spend, "currency": "CAD"}
        finally:
            engine.dispose()
