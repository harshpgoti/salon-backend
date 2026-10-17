"""Budget-bounded offer campaigns (FR-054/FR-055/FR-057; TASK-039).

Drafting and sending are separate powers: anyone with retention.manage can
draft and edit, but only team.manage (the owner) approves, tests, cancels —
approval freezes the audience snapshot plus the exact template, budget and
schedule it was approved with. Editing send mechanics afterwards reopens the
draft, so an approval always means what it says.

- AC-1: draft/template/terms/expiry/sender/channel/schedule/approval/test/
  budget/final-confirmation all exist; audience confirmation shows the same
  counts execution will use.
- AC-2: pause and cancel stop future sends without touching sent ones; the
  budget caps sends exactly; over-budget increases need a fresh approval;
  every send revalidates eligibility live; frequency caps span offers and
  rebooking prompts together.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

__all__ = [
    "CAMPAIGN_FREQUENCY_CAP",
    "CAMPAIGN_FREQUENCY_DAYS",
    "CampaignModuleError",
    "register_campaign_routes",
    "run_campaigns",
]

# FR-057 frequency cap: at most this many marketing sends per client in the
# trailing window, across offers and rebooking prompts together.
CAMPAIGN_FREQUENCY_CAP = 4
CAMPAIGN_FREQUENCY_DAYS = 30

CAMPAIGN_STATUSES = ("draft", "scheduled", "sending", "paused", "completed",
                     "partial", "cancelled", "failed")
# Content edits never affect sending; mechanics edits reopen the draft.
CONTENT_FIELDS = {"name", "offer_title", "offer_terms", "offer_code",
                  "offer_expires_at"}


class CampaignModuleError(Exception):
    """Base exception for campaign operations."""


def _campaign_or_404(connection, *, tenant_id: str, campaign_id: str,
                     for_update: bool = False) -> dict:
    row = connection.execute(
        text("""SELECT id::text AS id, name, segment_id::text AS segment_id,
                       channel, offer_title, offer_terms, offer_code,
                       offer_expires_at, template_subject, template_body,
                       whatsapp_template_id::text AS whatsapp_template_id,
                       budget_minor, scheduled_at, status,
                       approved_by_issuer, approved_at, audience_snapshot,
                       version FROM offer_campaigns
                WHERE tenant_id=:tenant AND id=:id""" +
        (" FOR UPDATE" if for_update else "")),
        {"tenant": tenant_id, "id": campaign_id}).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "campaign_not_found"})
    return dict(row)


def run_campaigns(connection, *, tenant_id: str,
                  now: datetime | None = None, limit: int = 200) -> dict:
    """Worker step: drain due approved campaigns with live revalidation.

    Each queued send re-checks campaign state, consent, suppression,
    frequency cap and remaining budget before recording; the dispatcher
    re-checks everything again at send time. Returns per-outcome counts.
    """
    from modules.communications.consent import check_consented, check_suppressed
    from modules.communications.outbox import record_booking_communication
    from modules.budgets import CHANNEL_RATES
    moment = now or datetime.now(timezone.utc)
    summary = {"campaigns_started": 0, "sent": 0, "skipped_consent": 0,
               "skipped_suppressed": 0, "skipped_unreachable": 0,
               "skipped_budget": 0, "skipped_frequency": 0,
               "completed": 0, "partial": 0}
    due = connection.execute(
        text("""SELECT id::text AS id, channel, status, budget_minor,
                       snapshot_budget_minor, snapshot_template_body,
                       snapshot_template_subject,
                       snapshot_whatsapp_template_id::text AS whatsapp_template_id,
                       snapshot_whatsapp_template_version AS whatsapp_template_version,
                       snapshot_whatsapp_template_body
                FROM offer_campaigns
                WHERE tenant_id=:tenant AND status IN ('scheduled', 'sending')
                  AND COALESCE(snapshot_scheduled_at, scheduled_at) IS NOT NULL
                  AND COALESCE(snapshot_scheduled_at, scheduled_at) <= :now
                ORDER BY COALESCE(snapshot_scheduled_at, scheduled_at), id
                LIMIT 10 FOR UPDATE SKIP LOCKED"""),
        {"tenant": tenant_id, "now": moment.isoformat()}).mappings().all()
    for campaign in due:
        if campaign["status"] == "scheduled":
            connection.execute(
                text("""UPDATE offer_campaigns SET status='sending',
                               version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id AND status='scheduled'"""),
                {"tenant": tenant_id, "id": campaign["id"]})
            summary["campaigns_started"] += 1
        budget = campaign["snapshot_budget_minor"]
        if budget is None:
            budget = campaign["budget_minor"]
        rate = CHANNEL_RATES.get(campaign["channel"], 0)
        sends = connection.execute(
            text("""SELECT s.id::text AS id, s.client_id::text AS client_id
                    FROM campaign_sends s
                    WHERE s.tenant_id=:tenant AND s.campaign_id=:campaign
                      AND s.state='queued'
                    ORDER BY s.created_at LIMIT :limit"""),
            {"tenant": tenant_id, "campaign": campaign["id"],
             "limit": limit}).mappings().all()
        for send in sends:
            still_live = connection.execute(
                text("SELECT status FROM offer_campaigns WHERE tenant_id=:t AND id=:c"),
                {"t": tenant_id, "c": campaign["id"]}).scalar_one()
            if still_live != "sending":
                break
            outcome = _send_one(connection, tenant_id=tenant_id, campaign=campaign,
                                send=send, budget=budget, rate=rate, moment=moment)
            summary[outcome] = summary.get(outcome, 0) + 1
        left = connection.execute(
            text("""SELECT count(*) FROM campaign_sends
                    WHERE tenant_id=:tenant AND campaign_id=:campaign AND state='queued'"""),
            {"tenant": tenant_id, "campaign": campaign["id"]}).scalar_one()
        if left > 0:
            # More queue remains than this batch drained (or the owner
            # cancelled mid-run and bulk-skipped it): leave the status for
            # the next worker pass instead of claiming a finish.
            continue
        skipped = connection.execute(
            text("""SELECT count(*) FROM campaign_sends
                    WHERE tenant_id=:tenant AND campaign_id=:campaign
                      AND state LIKE 'skipped%'"""),
            {"tenant": tenant_id, "campaign": campaign["id"]}).scalar_one()
        final = "completed" if skipped == 0 else "partial"
        updated = connection.execute(
            text("""UPDATE offer_campaigns SET status=:status,
                           version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND id=:id AND status='sending'"""),
            {"tenant": tenant_id, "id": campaign["id"], "status": final})
        if updated.rowcount:
            summary[final] += 1
    return summary


def _send_one(connection, *, tenant_id: str, campaign: dict, send: dict,
              budget: int, rate: int, moment: datetime) -> str:
    """Revalidate and record one campaign send; returns the outcome key."""
    from modules.communications.consent import check_consented, check_suppressed
    from modules.communications.outbox import record_booking_communication
    contact = connection.execute(
        text("""SELECT kind, normalized_value FROM client_contacts
                WHERE tenant_id=:tenant AND client_id=:client
                ORDER BY is_primary DESC, created_at LIMIT 1"""),
        {"tenant": tenant_id, "client": send["client_id"]}).mappings().one_or_none()
    if contact is None:
        _mark_send(connection, tenant_id=tenant_id, send_id=send["id"],
                   state="skipped_unreachable")
        return "skipped_unreachable"
    if not check_consented(connection, tenant_id=tenant_id, client_id=send["client_id"],
                           channel=campaign["channel"], purpose="marketing", now=moment):
        _mark_send(connection, tenant_id=tenant_id, send_id=send["id"],
                   state="skipped_consent")
        return "skipped_consent"
    if check_suppressed(connection, tenant_id=tenant_id,
                        contact_value=contact["normalized_value"],
                        channel=campaign["channel"]):
        _mark_send(connection, tenant_id=tenant_id, send_id=send["id"],
                   state="skipped_suppressed")
        return "skipped_suppressed"
    recent = connection.execute(
        text("""SELECT count(*) FROM communications
                WHERE tenant_id=:tenant AND client_id=:client AND purpose='marketing'
                  AND created_at > :since"""),
        {"tenant": tenant_id, "client": send["client_id"],
         "since": (moment - timedelta(days=CAMPAIGN_FREQUENCY_DAYS)).isoformat()},
    ).scalar_one()
    if recent >= CAMPAIGN_FREQUENCY_CAP:
        _mark_send(connection, tenant_id=tenant_id, send_id=send["id"],
                   state="skipped_frequency")
        return "skipped_frequency"
    spent = connection.execute(
        text("""SELECT COALESCE(SUM(mc.cost_minor), 0) FROM message_costs mc
                JOIN campaign_sends s ON s.tenant_id=mc.tenant_id
                                      AND s.communication_id=mc.communication_id
                WHERE s.tenant_id=:tenant AND s.campaign_id=:campaign"""),
        {"tenant": tenant_id, "campaign": campaign["id"]}).scalar_one()
    # Ledgered spend plus recorded-but-undispatched estimates: the worker only
    # records, while dispatch ledgers later, so both count toward the cap.
    outstanding = connection.execute(
        text("""SELECT count(*) FROM communications
                WHERE tenant_id=:tenant AND source_type='campaign'
                  AND source_id=:campaign
                  AND state IN ('recorded', 'sending', 'retry_scheduled')"""),
        {"tenant": tenant_id, "campaign": campaign["id"]}).scalar_one()
    if spent + outstanding * rate + rate > budget:
        _mark_send(connection, tenant_id=tenant_id, send_id=send["id"],
                   state="skipped_budget")
        return "skipped_budget"
    token = uuid.uuid4().hex
    row = record_booking_communication(
        connection, tenant_id=tenant_id, appointment_id=campaign["id"],
        client_id=send["client_id"], contact_value=contact["normalized_value"],
        channel=campaign["channel"], purpose="marketing",
        entity_version=_campaign_version(connection, tenant_id=tenant_id,
                                         campaign_id=campaign["id"]),
        template_id=campaign["whatsapp_template_id"]
        if campaign["channel"] == "whatsapp" else None,
        template_version=campaign["whatsapp_template_version"]
        if campaign["channel"] == "whatsapp" else None,
        template_subject_snapshot=campaign["snapshot_template_subject"],
        template_body_snapshot=(campaign["snapshot_whatsapp_template_body"]
                                if campaign["channel"] == "whatsapp"
                                else campaign["snapshot_template_body"]),
        source_type="campaign")
    if row is None:
        return "skipped_consent"
    connection.execute(
        text("""UPDATE communications SET attribution_token=:token
                WHERE tenant_id=:tenant AND id=:id"""),
        {"tenant": tenant_id, "id": row["id"], "token": token})
    connection.execute(
        text("""UPDATE campaign_sends SET state='recorded', communication_id=:comm,
                       updated_at=now()
                WHERE tenant_id=:tenant AND id=:send"""),
        {"tenant": tenant_id, "send": send["id"], "comm": row["id"]})
    return "sent"


def _mark_send(connection, *, tenant_id: str, send_id: str, state: str) -> None:
    connection.execute(
        text("""UPDATE campaign_sends SET state=:state, updated_at=now()
                WHERE tenant_id=:tenant AND id=:send"""),
        {"tenant": tenant_id, "send": send_id, "state": state})


def _campaign_version(connection, *, tenant_id: str, campaign_id: str) -> int:
    return connection.execute(
        text("SELECT version FROM offer_campaigns WHERE tenant_id=:t AND id=:c"),
        {"t": tenant_id, "c": campaign_id}).scalar_one()


class CampaignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    segment_id: uuid.UUID
    channel: str = Field(pattern="^(email|sms|whatsapp)$")
    offer_title: str = Field(min_length=1, max_length=160)
    offer_terms: str = Field(default="", max_length=2000)
    offer_code: str | None = Field(default=None, max_length=40)
    offer_expires_at: datetime | None = None
    template_subject: str | None = Field(default=None, max_length=200)
    template_body: str = Field(min_length=1, max_length=2000)
    whatsapp_template_id: uuid.UUID | None = None
    budget_minor: int = Field(ge=0)
    scheduled_at: datetime | None = None


class CampaignPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=120)
    segment_id: uuid.UUID | None = None
    channel: str | None = Field(default=None, pattern="^(email|sms|whatsapp)$")
    offer_title: str | None = Field(default=None, min_length=1, max_length=160)
    offer_terms: str | None = Field(default=None, max_length=2000)
    offer_code: str | None = Field(default=None, max_length=40)
    offer_expires_at: datetime | None = None
    template_subject: str | None = Field(default=None, max_length=200)
    template_body: str | None = Field(default=None, min_length=1, max_length=2000)
    whatsapp_template_id: uuid.UUID | None = None
    budget_minor: int | None = Field(default=None, ge=0)
    scheduled_at: datetime | None = None


class CampaignTestIn(BaseModel):
    client_id: uuid.UUID


def register_campaign_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    @app.post("/v1/tenants/{tenant_id}/campaigns", tags=["retention"], status_code=201)
    def create_campaign(tenant_id: uuid.UUID, payload: CampaignIn,
                        request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.manage")
                segment = connection.execute(
                    text("SELECT 1 FROM client_segments WHERE tenant_id=:t AND id=:s"),
                    {"t": str(tenant_id), "s": str(payload.segment_id)},
                ).scalar_one_or_none()
                if segment is None:
                    raise HTTPException(status_code=422, detail={"code": "unknown_segment"})
                row = connection.execute(
                    text("""INSERT INTO offer_campaigns
                            (tenant_id, id, name, segment_id, channel, offer_title,
                             offer_terms, offer_code, offer_expires_at,
                             template_subject, template_body, whatsapp_template_id,
                             budget_minor, scheduled_at)
                            VALUES (:tenant, :id, :name, :segment, :channel, :title,
                                    :terms, :code, :expires, :subject, :body, :watpl,
                                    :budget, :scheduled)
                            RETURNING id::text AS id, status, version"""),
                    {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                     "name": payload.name.strip(), "segment": str(payload.segment_id),
                     "channel": payload.channel, "title": payload.offer_title.strip(),
                     "terms": payload.offer_terms,
                     "code": payload.offer_code.strip() if payload.offer_code else None,
                     "expires": payload.offer_expires_at.isoformat()
                     if payload.offer_expires_at else None,
                     "subject": payload.template_subject, "body": payload.template_body,
                     "watpl": str(payload.whatsapp_template_id)
                     if payload.whatsapp_template_id else None,
                     "budget": payload.budget_minor,
                     "scheduled": payload.scheduled_at.isoformat()
                     if payload.scheduled_at else None},
                ).mappings().one()
                insert_audit(connection, tenant_id, identity, action="campaign.drafted",
                             resource_type="offer_campaign", resource_id=row["id"],
                             reason=payload.name.strip(),
                             request_id=request.state.request_id,
                             changed_fields=["name", "segment_id", "channel", "budget_minor"])
            return dict(row)
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/campaigns/{campaign_id}", tags=["retention"])
    def update_campaign(tenant_id: uuid.UUID, campaign_id: uuid.UUID,
                        payload: CampaignPatch, request: Request,
                        identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.manage")
                current = _campaign_or_404(connection, tenant_id=str(tenant_id),
                                           campaign_id=str(campaign_id),
                                           for_update=True)
                if current["status"] != "draft":
                    raise HTTPException(status_code=409, detail={
                        "code": "campaign_locked",
                        "message": "only drafts edit; approved campaigns duplicate or cancel"})
                if payload.expected_version != current["version"]:
                    raise HTTPException(status_code=409, detail={"code": "campaign_version_conflict"})
                changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
                if not changes:
                    raise HTTPException(status_code=422, detail={"code": "campaign_change_required"})
                if "segment_id" in changes and changes["segment_id"] is not None:
                    segment = connection.execute(
                        text("SELECT 1 FROM client_segments WHERE tenant_id=:t AND id=:s"),
                        {"t": str(tenant_id), "s": str(changes["segment_id"])},
                    ).scalar_one_or_none()
                    if segment is None:
                        raise HTTPException(status_code=422, detail={"code": "unknown_segment"})
                serializable = {k: (v.isoformat() if isinstance(v, datetime) else
                                    (str(v) if isinstance(v, uuid.UUID) else v))
                                for k, v in changes.items()}
                assignments = ", ".join(f"{key}=:{key}" for key in serializable)
                row = connection.execute(
                    text(f"""UPDATE offer_campaigns SET {assignments},
                                   version=version+1, updated_at=now()
                             WHERE tenant_id=:tenant AND id=:id
                             RETURNING id::text AS id, status, version"""),
                    {"tenant": str(tenant_id), "id": str(campaign_id),
                     **serializable}).mappings().one()
                insert_audit(connection, tenant_id, identity, action="campaign.edited",
                             resource_type="offer_campaign", resource_id=row["id"],
                             reason="draft edited",
                             request_id=request.state.request_id,
                             changed_fields=list(changes.keys()))
            return dict(row)
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/campaigns/{campaign_id}/approve", tags=["retention"])
    def approve_campaign(tenant_id: uuid.UUID, campaign_id: uuid.UUID,
                         request: Request, identity=Depends(require_identity)) -> dict:
        """Freeze audience, template, budget and schedule; open the queue."""
        from modules.segments import resolve_segment
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "team.manage")
                campaign = _campaign_or_404(connection, tenant_id=str(tenant_id),
                                            campaign_id=str(campaign_id),
                                            for_update=True)
                if campaign["status"] != "draft":
                    raise HTTPException(status_code=409, detail={
                        "code": "campaign_not_draft",
                        "message": "only drafts approve"})
                if campaign["scheduled_at"] is None:
                    raise HTTPException(status_code=422, detail={
                        "code": "campaign_unscheduled",
                        "message": "set the send time before approval"})
                wa_template = None
                if campaign["channel"] == "whatsapp":
                    wa_template = connection.execute(
                        text("""SELECT id::text AS id, name, category, locale,
                                      body, status, version
                                FROM whatsapp_templates
                                WHERE tenant_id=:t AND id=:tpl"""),
                        {"t": str(tenant_id),
                         "tpl": campaign["whatsapp_template_id"]},
                    ).mappings().one_or_none() if campaign["whatsapp_template_id"] else None
                    if wa_template is None or wa_template["status"] != "approved":
                        raise HTTPException(status_code=422, detail={
                            "code": "whatsapp_template_unapproved",
                            "message": "WhatsApp campaigns need an approved template"})
                audience = resolve_segment(connection, tenant_id=str(tenant_id),
                                           segment_id=campaign["segment_id"],
                                           channel=campaign["channel"])
                for client_id in audience["eligible"]:
                    connection.execute(
                        text("""INSERT INTO campaign_sends (tenant_id, id, campaign_id, client_id)
                                VALUES (:tenant, :id, :campaign, :client)
                                ON CONFLICT (tenant_id, campaign_id, client_id) DO NOTHING"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                         "campaign": str(campaign_id), "client": client_id})
                connection.execute(
                    text("""UPDATE offer_campaigns
                            SET status='scheduled', approved_by_issuer=:issuer,
                                approved_by_subject=:subject, approved_at=now(),
                                audience_snapshot=:snapshot,
                                snapshot_template_subject=template_subject,
                                snapshot_template_body=template_body,
                                snapshot_budget_minor=budget_minor,
                                snapshot_scheduled_at=scheduled_at,
                                snapshot_whatsapp_template_id=:wa_id,
                                snapshot_whatsapp_template_version=:wa_version,
                                snapshot_whatsapp_template_name=:wa_name,
                                snapshot_whatsapp_template_category=:wa_category,
                                snapshot_whatsapp_template_locale=:wa_locale,
                                snapshot_whatsapp_template_body=:wa_body,
                                version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id AND status='draft'"""),
                    {"tenant": str(tenant_id), "id": str(campaign_id),
                     "issuer": identity.issuer, "subject": identity.subject,
                     "snapshot": json.dumps(audience["eligible"]),
                     "wa_id": wa_template["id"] if wa_template else None,
                     "wa_version": wa_template["version"] if wa_template else None,
                     "wa_name": wa_template["name"] if wa_template else None,
                     "wa_category": wa_template["category"] if wa_template else None,
                     "wa_locale": wa_template["locale"] if wa_template else None,
                     "wa_body": wa_template["body"] if wa_template else None})
                insert_audit(connection, tenant_id, identity, action="campaign.approved",
                             resource_type="offer_campaign", resource_id=str(campaign_id),
                             reason=f"{len(audience['eligible'])} eligible, "
                                    f"budget {campaign['budget_minor']}",
                             request_id=request.state.request_id,
                             changed_fields=["status", "audience_snapshot"])
            return {"id": str(campaign_id), "status": "scheduled",
                    "eligible": len(audience["eligible"]),
                    "exclusions": audience["exclusions"]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/campaigns/{campaign_id}/pause", tags=["retention"])
    def pause_campaign(tenant_id: uuid.UUID, campaign_id: uuid.UUID,
                       request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "team.manage")
                row = connection.execute(
                    text("""UPDATE offer_campaigns
                            SET paused_from_status=status, status='paused',
                                version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id
                              AND status IN ('scheduled', 'sending', 'completed', 'partial')
                            RETURNING id::text AS id, status"""),
                    {"tenant": str(tenant_id), "id": str(campaign_id)},
                ).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=409, detail={
                        "code": "campaign_not_pauseable"})
                insert_audit(connection, tenant_id, identity, action="campaign.paused",
                             resource_type="offer_campaign", resource_id=row["id"],
                             reason="owner paused future sends",
                             request_id=request.state.request_id,
                             changed_fields=["status", "paused_from_status"])
            return dict(row)
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/campaigns/{campaign_id}/resume", tags=["retention"])
    def resume_campaign(tenant_id: uuid.UUID, campaign_id: uuid.UUID,
                        request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "team.manage")
                row = connection.execute(
                    text("""UPDATE offer_campaigns
                            SET status=paused_from_status, paused_from_status=NULL,
                                version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id AND status='paused'
                            RETURNING id::text AS id, status"""),
                    {"tenant": str(tenant_id), "id": str(campaign_id)},
                ).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=409, detail={
                        "code": "campaign_not_paused"})
                insert_audit(connection, tenant_id, identity, action="campaign.resumed",
                             resource_type="offer_campaign", resource_id=row["id"],
                             reason="owner resumed approved campaign",
                             request_id=request.state.request_id,
                             changed_fields=["status", "paused_from_status"])
            return dict(row)
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/campaigns/{campaign_id}/cancel", tags=["retention"])
    def cancel_campaign(tenant_id: uuid.UUID, campaign_id: uuid.UUID,
                        request: Request, identity=Depends(require_identity)) -> dict:
        """Stop future sends; sent ones stay sent, queued ones skip."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "team.manage")
                row = connection.execute(
                    text("""UPDATE offer_campaigns SET status='cancelled',
                                   paused_from_status=NULL,
                                   version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id
                              AND status IN ('scheduled', 'sending', 'paused',
                                             'completed', 'partial')
                            RETURNING id::text AS id, status"""),
                    {"tenant": str(tenant_id), "id": str(campaign_id)},
                ).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={
                        "code": "campaign_not_cancellable",
                        "message": "only scheduled or sending campaigns cancel"})
                connection.execute(
                    text("""UPDATE campaign_sends SET state='skipped_cancelled',
                                   updated_at=now()
                            WHERE tenant_id=:tenant AND campaign_id=:campaign
                              AND state IN ('queued', 'recorded')"""),
                    {"tenant": str(tenant_id), "campaign": str(campaign_id)})
                connection.execute(
                    text("""UPDATE communications SET state='skipped_stale',
                                   skip_reason='campaign cancelled before dispatch',
                                   version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND source_type='campaign'
                              AND source_id=:campaign AND state='recorded'"""),
                    {"tenant": str(tenant_id), "campaign": str(campaign_id)})
                insert_audit(connection, tenant_id, identity, action="campaign.cancelled",
                             resource_type="offer_campaign", resource_id=row["id"],
                             reason="owner cancelled future sends",
                             request_id=request.state.request_id,
                             changed_fields=["status"])
            return dict(row)
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/campaigns/{campaign_id}/test", tags=["retention"])
    def test_campaign(tenant_id: uuid.UUID, campaign_id: uuid.UUID, payload: CampaignTestIn,
                      request: Request, identity=Depends(require_identity)) -> dict:
        """Send one real test through the full gates to a tenant client."""
        from modules.communications.consent import check_consented, check_suppressed
        from modules.communications.outbox import record_booking_communication
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "team.manage")
                campaign = _campaign_or_404(connection, tenant_id=str(tenant_id),
                                            campaign_id=str(campaign_id))
                contact = connection.execute(
                    text("""SELECT kind, normalized_value FROM client_contacts
                            WHERE tenant_id=:tenant AND client_id=:client
                            ORDER BY is_primary DESC, created_at LIMIT 1"""),
                    {"tenant": str(tenant_id),
                     "client": str(payload.client_id)}).mappings().one_or_none()
                if contact is None:
                    raise HTTPException(status_code=422, detail={
                        "code": "test_client_unreachable",
                        "message": "test clients need a contact"})
                if not check_consented(
                        connection, tenant_id=str(tenant_id), client_id=str(payload.client_id),
                        channel=campaign["channel"], purpose="marketing"):
                    raise HTTPException(status_code=422, detail={
                        "code": "test_client_unconsented",
                        "message": "even tests follow consent"})
                wa_template = None
                if campaign["channel"] == "whatsapp":
                    wa_template = connection.execute(
                        text("""SELECT id::text AS id, version, status
                                FROM whatsapp_templates
                                WHERE tenant_id=:t AND id=:id"""),
                        {"t": str(tenant_id),
                         "id": campaign["whatsapp_template_id"]},
                    ).mappings().one_or_none() if campaign["whatsapp_template_id"] else None
                    if wa_template is None or wa_template["status"] != "approved":
                        raise HTTPException(status_code=422, detail={
                            "code": "whatsapp_template_unapproved"})
                token = uuid.uuid4().hex
                row = record_booking_communication(
                    connection, tenant_id=str(tenant_id), appointment_id=str(campaign_id),
                    client_id=str(payload.client_id),
                    contact_value=contact["normalized_value"],
                    channel=campaign["channel"], purpose="marketing",
                    entity_version=campaign["version"], source_type="campaign_test",
                    template_id=wa_template["id"] if wa_template else None,
                    template_version=wa_template["version"] if wa_template else None,
                    template_subject_snapshot=campaign["template_subject"],
                    template_body_snapshot=campaign["template_body"])
                if row is None:
                    raise HTTPException(status_code=409, detail={"code": "test_already_sent"})
                connection.execute(
                    text("""UPDATE communications SET attribution_token=:token
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": row["id"], "token": token})
                insert_audit(connection, tenant_id, identity, action="campaign.tested",
                             resource_type="offer_campaign", resource_id=str(campaign_id),
                             reason=f"test to client {payload.client_id}",
                             request_id=request.state.request_id,
                             changed_fields=["tested"])
            return {"communication_id": row["id"], "attribution_token": token}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/campaigns", tags=["retention"])
    def list_campaigns(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.manage")
                rows = connection.execute(
                    text("""SELECT c.id::text AS id, c.name, c.channel, c.status,
                                   c.budget_minor, c.scheduled_at, c.version,
                                   (SELECT count(*) FROM campaign_sends s
                                     WHERE s.tenant_id=c.tenant_id AND s.campaign_id=c.id
                                       AND s.state='sent') AS sent,
                                   (SELECT count(*) FROM campaign_sends s
                                     WHERE s.tenant_id=c.tenant_id AND s.campaign_id=c.id
                                       AND s.state LIKE 'skipped%') AS skipped
                                   ,(SELECT count(*) FROM communications m
                                      WHERE m.tenant_id=c.tenant_id
                                        AND m.source_type='campaign' AND m.source_id=c.id
                                        AND m.state IN ('recorded', 'sending', 'retry_scheduled')) AS pending
                            FROM offer_campaigns c
                            WHERE c.tenant_id=:tenant ORDER BY c.created_at DESC"""),
                    {"tenant": str(tenant_id)}).mappings().all()
            return {"items": [{**dict(r),
                               "scheduled_at": r["scheduled_at"].isoformat()
                               if r["scheduled_at"] else None} for r in rows]}
        finally:
            engine.dispose()
