"""Offer redemptions and attributed outcomes (FR-056; TASK-040).

Attribution is claimed, never inferred: a booking attributes to a campaign
send only when it carries that send's token (or the campaign's code is
recorded by staff), and only inside the seven-day window after accepted dispatch.
One appointment attributes at most once across offers and rebooking — the first match wins — so revenue
is never double-counted and no causal lift is ever claimed. Reports describe
attributed bookings and recorded receipts; the word "lift" appears nowhere.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

__all__ = [
    "ATTRIBUTION_DAYS",
    "RedemptionModuleError",
    "attribute_campaign_booking",
    "register_redemption_routes",
]

# Canonical send-to-booking window from plan/analytics.md.
ATTRIBUTION_DAYS = 7


class RedemptionModuleError(Exception):
    """Base exception for redemption operations."""


def claim_appointment_attribution(connection, *, tenant_id: str,
                                  appointment_id: str, kind: str,
                                  campaign_id: str | None = None,
                                  cycle_id: str | None = None,
                                  communication_id: str | None = None) -> bool:
    """Claim the appointment once across campaign and rebooking sources."""
    row = connection.execute(
        text("""INSERT INTO appointment_attributions
                    (tenant_id, appointment_id, kind, campaign_id, cycle_id,
                     communication_id)
                VALUES (:tenant, :appointment, :kind, :campaign, :cycle, :communication)
                ON CONFLICT (tenant_id, appointment_id) DO NOTHING
                RETURNING appointment_id"""),
        {"tenant": tenant_id, "appointment": appointment_id, "kind": kind,
         "campaign": campaign_id, "cycle": cycle_id,
         "communication": communication_id},
    ).scalar_one_or_none()
    return row is not None


def attribute_campaign_booking(connection, *, tenant_id: str,
                               attribution_token: str | None,
                               appointment_id: str,
                               now: datetime | None = None) -> dict | None:
    """Match a tracked booking to its campaign send inside the window.

    Called in the confirming transaction next to cycle attribution. Returns
    the redemption row, or None when there is nothing to claim (no token,
    unknown token, outside the window, already attributed). Never raises for
    a non-match: organic bookings simply stand alone.
    """
    if not attribution_token:
        return None
    moment = now or datetime.now(timezone.utc)
    send = connection.execute(
        text("""SELECT s.campaign_id::text AS campaign_id, s.client_id::text AS client_id,
                       s.communication_id::text AS communication_id, c.dispatched_at
                FROM campaign_sends s
                JOIN communications c ON c.tenant_id=s.tenant_id
                                      AND c.id=s.communication_id
                WHERE s.tenant_id=:tenant AND c.attribution_token=:token
                  AND s.state='sent'"""),
        {"tenant": tenant_id, "token": attribution_token}).mappings().one_or_none()
    if send is None:
        return None
    sent_at = send["dispatched_at"]
    if sent_at is None:
        return None
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    if moment < sent_at or moment > sent_at + timedelta(days=ATTRIBUTION_DAYS):
        return None
    savepoint = connection.begin_nested()
    try:
        if not claim_appointment_attribution(
                connection, tenant_id=tenant_id, appointment_id=appointment_id,
                kind="campaign", campaign_id=send["campaign_id"],
                communication_id=send["communication_id"]):
            savepoint.rollback()
            return None
        row = connection.execute(
            text("""INSERT INTO offer_redemptions
                    (tenant_id, id, campaign_id, client_id, appointment_id,
                     communication_id)
                    VALUES (:tenant, :id, :campaign, :client, :appt, :comm)
                    ON CONFLICT (tenant_id, appointment_id)
                    WHERE appointment_id IS NOT NULL DO NOTHING
                    RETURNING id::text AS id"""),
            {"tenant": tenant_id, "id": str(uuid.uuid4()),
             "campaign": send["campaign_id"], "client": send["client_id"],
             "appt": appointment_id, "comm": send["communication_id"]},
        ).mappings().one_or_none()
    except IntegrityError:
        savepoint.rollback()
        return None
    if row is None:
        savepoint.rollback()
        return None
    savepoint.commit()
    return {"id": row["id"], "campaign_id": send["campaign_id"],
            "appointment_id": appointment_id,
            "communication_id": send["communication_id"]}


class RedemptionIn(BaseModel):
    campaign_id: uuid.UUID | None = None
    code: str | None = Field(default=None, max_length=40)
    client_id: uuid.UUID
    appointment_id: uuid.UUID | None = None


def register_redemption_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    @app.post("/v1/tenants/{tenant_id}/redemptions", tags=["retention"], status_code=201)
    def record_redemption(tenant_id: uuid.UUID, payload: RedemptionIn,
                          request: Request, identity=Depends(require_identity)) -> dict:
        """Staff-recorded code redemption, audited and deduplicated."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                campaign_id = str(payload.campaign_id) if payload.campaign_id else None
                if campaign_id is None:
                    if not payload.code:
                        raise HTTPException(status_code=422, detail={
                            "code": "redemption_needs_campaign_or_code"})
                    found = connection.execute(
                        text("""SELECT id::text AS id FROM offer_campaigns
                                WHERE tenant_id=:tenant AND offer_code=:code"""),
                        {"tenant": str(tenant_id), "code": payload.code.strip()},
                    ).scalar_one_or_none()
                    if found is None:
                        raise HTTPException(status_code=422, detail={
                            "code": "unknown_offer_code",
                            "message": "no campaign carries this code"})
                    campaign_id = found
                else:
                    exists = connection.execute(
                        text("SELECT 1 FROM offer_campaigns WHERE tenant_id=:t AND id=:c"),
                        {"t": str(tenant_id), "c": campaign_id}).scalar_one_or_none()
                    if exists is None:
                        raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
                client = connection.execute(
                    text("SELECT 1 FROM clients WHERE tenant_id=:t AND id=:c"),
                    {"t": str(tenant_id), "c": str(payload.client_id)},
                ).scalar_one_or_none()
                if client is None:
                    raise HTTPException(status_code=404, detail={"code": "client_not_found"})
                if payload.appointment_id is not None:
                    appt = connection.execute(
                        text("""SELECT 1 FROM appointments
                                WHERE tenant_id=:t AND id=:a AND client_id=:c"""),
                        {"t": str(tenant_id), "a": str(payload.appointment_id),
                         "c": str(payload.client_id)}).scalar_one_or_none()
                    if appt is None:
                        raise HTTPException(status_code=422, detail={
                            "code": "appointment_mismatch",
                            "message": "appointment does not belong to this client"})
                savepoint = connection.begin_nested()
                if (payload.appointment_id is not None
                        and not claim_appointment_attribution(
                            connection, tenant_id=str(tenant_id),
                            appointment_id=str(payload.appointment_id),
                            kind="campaign", campaign_id=campaign_id)):
                    savepoint.rollback()
                    raise HTTPException(status_code=409, detail={
                        "code": "already_attributed",
                        "message": "this appointment already attributes once"})
                try:
                    row = connection.execute(
                        text("""INSERT INTO offer_redemptions
                                (tenant_id, id, campaign_id, client_id, appointment_id, code)
                                VALUES (:tenant, :id, :campaign, :client, :appt, :code)
                                ON CONFLICT (tenant_id, appointment_id)
                                WHERE appointment_id IS NOT NULL DO NOTHING
                                RETURNING id::text AS id"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                         "campaign": campaign_id, "client": str(payload.client_id),
                         "appt": str(payload.appointment_id) if payload.appointment_id else None,
                         "code": payload.code.strip() if payload.code else None},
                    ).mappings().one_or_none()
                except IntegrityError:
                    savepoint.rollback()
                    raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
                if row is None:
                    savepoint.rollback()
                    raise HTTPException(status_code=409, detail={
                        "code": "already_attributed",
                        "message": "this appointment already attributes once"})
                savepoint.commit()
                insert_audit(connection, tenant_id, identity, action="offer.redeemed",
                             resource_type="offer_redemption", resource_id=row["id"],
                             reason=f"campaign {campaign_id}",
                             request_id=request.state.request_id,
                             changed_fields=["campaign_id", "appointment_id"])
            return {"id": row["id"], "campaign_id": campaign_id}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/campaigns/{campaign_id}/report", tags=["retention"])
    def campaign_report(tenant_id: uuid.UUID, campaign_id: uuid.UUID,
                        identity=Depends(require_identity)) -> dict:
        """Delivery, outcomes and recorded money — attributed, never lifted.

        Counts come from ledger rows: sends by state, suppressions since
        approval, attributed bookings with their recorded receipts, and
        message costs. There is no lift, ROI or open-rate claim anywhere in
        this response by construction (no such computation exists).
        """
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.manage")
                campaign = connection.execute(
                    text("""SELECT id::text AS id, name, channel, status, budget_minor,
                                   approved_at FROM offer_campaigns
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(campaign_id)},
                ).mappings().one_or_none()
                if campaign is None:
                    raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
                sends = connection.execute(
                    text("""SELECT state, count(*) FROM campaign_sends
                            WHERE tenant_id=:tenant AND campaign_id=:campaign
                            GROUP BY state"""),
                    {"tenant": str(tenant_id), "campaign": str(campaign_id)},
                ).mappings().all()
                redemptions = connection.execute(
                    text("""SELECT r.id::text AS id, r.client_id::text AS client_id,
                                   r.appointment_id::text AS appointment_id,
                                   r.code, r.created_at,
                                   COALESCE((SELECT SUM(e.amount_minor)
                                             FROM salon_receipt_entries e
                                             WHERE e.tenant_id=r.tenant_id
                                               AND e.appointment_id=r.appointment_id), 0)
                                   AS recorded_minor
                            FROM offer_redemptions r
                            WHERE r.tenant_id=:tenant AND r.campaign_id=:campaign
                            ORDER BY r.created_at"""),
                    {"tenant": str(tenant_id), "campaign": str(campaign_id)},
                ).mappings().all()
                cost = connection.execute(
                    text("""SELECT COALESCE(SUM(mc.cost_minor), 0) FROM message_costs mc
                            JOIN campaign_sends s ON s.tenant_id=mc.tenant_id
                                                  AND s.communication_id=mc.communication_id
                            WHERE s.tenant_id=:tenant AND s.campaign_id=:campaign"""),
                    {"tenant": str(tenant_id), "campaign": str(campaign_id)}).scalar_one()
                unsubs = connection.execute(
                    text("""SELECT count(DISTINCT u.client_id)
                            FROM suppressions u
                            JOIN campaign_sends s ON s.tenant_id=u.tenant_id
                                                 AND s.client_id=u.client_id
                            JOIN communications c ON c.tenant_id=s.tenant_id
                                                   AND c.id=s.communication_id
                            WHERE s.tenant_id=:tenant AND s.campaign_id=:campaign
                              AND s.state='sent' AND c.dispatched_at IS NOT NULL
                              AND u.reason IN ('unsubscribe', 'stop')
                              AND u.contact_value=c.contact_value
                              AND u.channel=c.channel
                              AND u.suppressed_at >= c.dispatched_at
                              AND u.suppressed_at <= c.dispatched_at + CAST(:window AS interval)"""),
                    {"tenant": str(tenant_id), "campaign": str(campaign_id),
                     "window": f"{ATTRIBUTION_DAYS} days"},
                ).scalar_one()
            out = dict(campaign)
            out["approved_at"] = out["approved_at"].isoformat() if out["approved_at"] else None
            return {**out,
                    "sends_by_state": {r["state"]: r["count"] for r in sends},
                    "unsubscribes_since_approval": unsubs,
                    "attributed_bookings": [
                        {**dict(r), "created_at": r["created_at"].isoformat()}
                        for r in redemptions],
                    "attributed_count": len(redemptions),
                    "attributed_recorded_minor": sum(r["recorded_minor"] for r in redemptions),
                    "message_cost_minor": cost,
                    "currency": "CAD"}
        finally:
            engine.dispose()
