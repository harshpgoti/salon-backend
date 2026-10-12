"""WhatsApp ISV onboarding and approved template sends (FR-045; TASK-031).

Sandbox only (DQ-006): the ISV here is an explicit local decision, never a
real vendor call and never browser automation. The module enforces the rules
a real onboarding would impose, so swapping in a provider adapter later
cannot silently widen them:

- AC-1: one business phone per salon (global UNIQUE); status
  draft -> submitted -> approved (or rejected/suspended) with the ISV
  reference, quality rating and messaging limit recorded and visible.
  Templates carry category/locale/status; only approved templates may open a
  conversation outside the 24h customer-service window.
- AC-2: an unapproved or restricted sender/template fails dispatch with a
  visible reason and creates a staff-visible unsent task row (the
  ``communications`` row itself, state ``failed`` with its reason). Inside an
  open 24h window free-form replies send without a template.

The window derives from message timestamps: open when the latest WhatsApp
inbound from the contact is within 24h. Marketing templates additionally need
consent (FR-040, enforced by the shared dispatcher gate).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

__all__ = [
    "CONVERSATION_WINDOW",
    "WhatsAppModuleError",
    "approve_sender",
    "approve_template",
    "check_wa_eligible",
    "create_template",
    "register_wa_sender_routes",
    "register_wa_sender",
    "reject_sender",
    "reject_template",
    "submit_sender",
    "submit_template",
    "wa_window_open",
]

# Meta's customer-service window: 24h from the client's last message.
CONVERSATION_WINDOW = timedelta(hours=24)

_PHONE_RE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_NAME_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_LOCALE_RE = re.compile(r"^[a-z]{2}(_[A-Z]{2})?$")
_PARAM_RE = re.compile(r"\{\{(\d+)\}\}")


class WhatsAppModuleError(Exception):
    """Base exception for WhatsApp onboarding operations."""


def _check_phone(phone_number: str) -> str:
    value = phone_number.strip().replace(" ", "").replace("-", "")
    if not _PHONE_RE.match(value):
        raise WhatsAppModuleError("business number must be E.164 (+14165550132)")
    return value


def _check_params(body: str) -> None:
    numbers = sorted({int(n) for n in _PARAM_RE.findall(body)})
    if numbers != list(range(1, len(numbers) + 1)):
        raise WhatsAppModuleError(
            "template parameters must be sequential {{1}}..{{n}} with no gaps")


def register_wa_sender(connection, *, tenant_id: str, business_name: str,
                       phone_number: str) -> dict:
    """Register a business sender as a local draft (not yet sent anywhere)."""
    name = business_name.strip()
    if not (1 <= len(name) <= 120):
        raise WhatsAppModuleError("business name must be 1-120 characters")
    phone = _check_phone(phone_number)
    try:
        row = connection.execute(
            text("""INSERT INTO whatsapp_senders (tenant_id, id, business_name, phone_number)
                    VALUES (:tenant, :id, :name, :phone)
                    RETURNING id, business_name, phone_number, status"""),
            {"tenant": tenant_id, "id": str(uuid.uuid4()), "name": name, "phone": phone},
        ).mappings().one()
    except IntegrityError as exc:
        raise WhatsAppModuleError(
            "this business number is already registered to a salon") from exc
    return dict(row)


def submit_sender(connection, *, tenant_id: str, sender_id: str,
                  isv_reference: str | None = None) -> dict:
    """Send a draft to the ISV sandbox; only drafts can be submitted."""
    row = connection.execute(
        text("""UPDATE whatsapp_senders
                SET status='submitted', isv_reference=:ref,
                    version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id AND status='draft'
                RETURNING id::text AS id, status, isv_reference"""),
        {"tenant": tenant_id, "id": sender_id, "ref": isv_reference or f"isv-{uuid.uuid4().hex[:12]}"},
    ).mappings().one_or_none()
    if row is None:
        raise WhatsAppModuleError("only a draft sender can be submitted")
    return dict(row)


def approve_sender(connection, *, tenant_id: str, sender_id: str,
                   quality_rating: str = "green",
                   messaging_limit: int | None = None) -> dict:
    """Record the ISV sandbox decision: approved with quality and limit.

    A red quality rating blocks sends at dispatch with a visible reason, the
    way Meta's quality gating would.
    """
    if quality_rating not in ("green", "yellow", "red"):
        raise WhatsAppModuleError("quality rating must be green, yellow or red")
    row = connection.execute(
        text("""UPDATE whatsapp_senders
                SET status='approved', quality_rating=:quality,
                    messaging_limit=:limit, approved_at=now(),
                    version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id AND status IN ('submitted', 'approved')
                RETURNING id::text AS id, business_name, phone_number, status,
                          quality_rating, messaging_limit"""),
        {"tenant": tenant_id, "id": sender_id,
         "quality": quality_rating, "limit": messaging_limit},
    ).mappings().one_or_none()
    if row is None:
        raise WhatsAppModuleError("only a submitted sender can be approved")
    return dict(row)


def reject_sender(connection, *, tenant_id: str, sender_id: str, note: str) -> dict:
    """Record the ISV refusal with its reason; rejected senders never route."""
    if not note.strip():
        raise WhatsAppModuleError("a refusal needs its reason recorded")
    row = connection.execute(
        text("""UPDATE whatsapp_senders
                SET status='rejected', decision_note=:note,
                    version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id AND status='submitted'
                RETURNING id::text AS id, status"""),
        {"tenant": tenant_id, "id": sender_id, "note": note.strip()},
    ).mappings().one_or_none()
    if row is None:
        raise WhatsAppModuleError("only a submitted sender can be rejected")
    return dict(row)


def create_template(connection, *, tenant_id: str, name: str, category: str,
                    locale: str, body: str) -> dict:
    """Create a template draft; parameters must be sequential {{1}}..{{n}}."""
    clean = name.strip().lower()
    if not _NAME_RE.match(clean):
        raise WhatsAppModuleError("template name must be [a-z0-9_], max 64 chars")
    if category not in ("utility", "marketing", "authentication"):
        raise WhatsAppModuleError("category must be utility, marketing or authentication")
    if not _LOCALE_RE.match(locale):
        raise WhatsAppModuleError("locale must look like en or en_US")
    if not (1 <= len(body) <= 1024):
        raise WhatsAppModuleError("template body must be 1-1024 characters")
    _check_params(body)
    try:
        row = connection.execute(
            text("""INSERT INTO whatsapp_templates
                    (tenant_id, id, name, category, locale, body)
                    VALUES (:tenant, :id, :name, :category, :locale, :body)
                    RETURNING id, name, category, locale, status"""),
            {"tenant": tenant_id, "id": str(uuid.uuid4()), "name": clean,
             "category": category, "locale": locale, "body": body},
        ).mappings().one()
    except IntegrityError as exc:
        raise WhatsAppModuleError(
            "a template with this name and locale already exists") from exc
    return dict(row)


def submit_template(connection, *, tenant_id: str, template_id: str) -> dict:
    row = connection.execute(
        text("""UPDATE whatsapp_templates SET status='submitted',
                       version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id AND status='draft'
                RETURNING id::text AS id, status"""),
        {"tenant": tenant_id, "id": template_id}).mappings().one_or_none()
    if row is None:
        raise WhatsAppModuleError("only a draft template can be submitted")
    return dict(row)


def approve_template(connection, *, tenant_id: str, template_id: str) -> dict:
    row = connection.execute(
        text("""UPDATE whatsapp_templates SET status='approved',
                       version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id AND status='submitted'
                RETURNING id::text AS id, name, category, locale, status"""),
        {"tenant": tenant_id, "id": template_id}).mappings().one_or_none()
    if row is None:
        raise WhatsAppModuleError("only a submitted template can be approved")
    return dict(row)


def reject_template(connection, *, tenant_id: str, template_id: str) -> dict:
    row = connection.execute(
        text("""UPDATE whatsapp_templates SET status='rejected',
                       version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id AND status='submitted'
                RETURNING id::text AS id, status"""),
        {"tenant": tenant_id, "id": template_id}).mappings().one_or_none()
    if row is None:
        raise WhatsAppModuleError("only a submitted template can be rejected")
    return dict(row)


def get_approved_wa_sender(connection, *, tenant_id: str) -> dict | None:
    """Oldest approved, non-red sender — the deterministic routing identity."""
    row = connection.execute(
        text("""SELECT id::text AS id, business_name, phone_number, quality_rating,
                       messaging_limit FROM whatsapp_senders
                WHERE tenant_id=:tenant AND status='approved'
                ORDER BY approved_at NULLS LAST, created_at LIMIT 1"""),
        {"tenant": tenant_id}).mappings().one_or_none()
    return dict(row) if row else None


def wa_window_open(connection, *, tenant_id: str, contact_value: str,
                   now: datetime | None = None) -> bool:
    """True when the client's last WhatsApp message is within 24h."""
    moment = now or datetime.now(timezone.utc)
    last = connection.execute(
        text("""SELECT received_at FROM inbound_messages
                WHERE tenant_id=:tenant AND from_value=:value AND channel='whatsapp'
                ORDER BY received_at DESC LIMIT 1"""),
        {"tenant": tenant_id, "value": contact_value}).scalar_one_or_none()
    if last is None:
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (moment - last) <= CONVERSATION_WINDOW


def check_wa_eligible(connection, *, tenant_id: str, contact_value: str,
                      template_id: str | None, now: datetime | None = None) -> dict:
    """Decide whether one WhatsApp send may go, and on what basis.

    Returns ``{"ok": True, "basis": ...}`` or ``{"ok": False, "reason": ...}``.
    Outside an open window a send needs an approved template; inside, the
    reply is free-form. Sender approval and quality gate first either way.
    """
    sender = get_approved_wa_sender(connection, tenant_id=tenant_id)
    if sender is None:
        return {"ok": False, "reason": "no approved WhatsApp sender for this salon"}
    if sender.get("quality_rating") == "red":
        return {"ok": False,
                "reason": "sender quality is red; Meta would block these sends"}
    if wa_window_open(connection, tenant_id=tenant_id,
                      contact_value=contact_value, now=now):
        return {"ok": True, "basis": "open_window", "sender": sender}
    if template_id is None:
        return {"ok": False,
                "reason": "outside the 24h window an approved template is required"}
    template = connection.execute(
        text("""SELECT id::text AS id, name, category, locale, status FROM whatsapp_templates
                WHERE tenant_id=:tenant AND id=:tid"""),
        {"tenant": tenant_id, "tid": template_id}).mappings().one_or_none()
    if template is None:
        return {"ok": False, "reason": "unknown WhatsApp template"}
    if template["status"] != "approved":
        return {"ok": False,
                "reason": f"template {template['name']} is {template['status']}, not approved"}
    return {"ok": True, "basis": "approved_template", "sender": sender,
            "template": dict(template)}


class WaSenderIn(BaseModel):
    business_name: str = Field(min_length=1, max_length=120)
    phone_number: str = Field(min_length=8, max_length=20)


class WaTemplateIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    category: str
    locale: str = Field(min_length=2, max_length=5)
    body: str = Field(min_length=1, max_length=1024)


class WaDecisionIn(BaseModel):
    note: str | None = None
    quality_rating: str | None = None
    messaging_limit: int | None = None


def register_wa_sender_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    def _fail(exc: WhatsAppModuleError, code: str):
        raise HTTPException(status_code=422,
                            detail={"code": code, "message": str(exc)})

    @app.post("/v1/tenants/{tenant_id}/whatsapp-senders", tags=["communications"], status_code=201)
    def create_wa_sender(tenant_id: uuid.UUID, payload: WaSenderIn,
                         request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                try:
                    created = register_wa_sender(
                        connection, tenant_id=str(tenant_id),
                        business_name=payload.business_name,
                        phone_number=payload.phone_number)
                except WhatsAppModuleError as exc:
                    _fail(exc, "invalid_whatsapp_sender")
                insert_audit(connection, tenant_id, identity, action="wa_sender.registered",
                             resource_type="whatsapp_sender", resource_id=created["id"],
                             reason=created["phone_number"],
                             request_id=request.state.request_id,
                             changed_fields=["business_name", "phone_number"])
            return created
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/whatsapp-senders/{sender_id}/submit", tags=["communications"])
    def submit_wa_sender(tenant_id: uuid.UUID, sender_id: uuid.UUID,
                         request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                try:
                    result = submit_sender(connection, tenant_id=str(tenant_id),
                                           sender_id=str(sender_id))
                except WhatsAppModuleError as exc:
                    _fail(exc, "wa_sender_submit_failed")
                insert_audit(connection, tenant_id, identity, action="wa_sender.submitted",
                             resource_type="whatsapp_sender", resource_id=result["id"],
                             reason="sent to ISV sandbox",
                             request_id=request.state.request_id,
                             changed_fields=["status"])
            return result
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/whatsapp-senders/{sender_id}/decide", tags=["communications"])
    def decide_wa_sender(tenant_id: uuid.UUID, sender_id: uuid.UUID, payload: WaDecisionIn,
                         request: Request, identity=Depends(require_identity)) -> dict:
        """Sandbox ISV decision: approve (quality/limit) or reject with a reason."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                try:
                    if payload.note:
                        result = reject_sender(connection, tenant_id=str(tenant_id),
                                               sender_id=str(sender_id), note=payload.note)
                        action = "wa_sender.rejected"
                    else:
                        result = approve_sender(
                            connection, tenant_id=str(tenant_id), sender_id=str(sender_id),
                            quality_rating=payload.quality_rating or "green",
                            messaging_limit=payload.messaging_limit)
                        action = "wa_sender.approved"
                except WhatsAppModuleError as exc:
                    _fail(exc, "wa_sender_decision_failed")
                insert_audit(connection, tenant_id, identity, action=action,
                             resource_type="whatsapp_sender", resource_id=result["id"],
                             reason=payload.note or f"quality={result.get('quality_rating')}",
                             request_id=request.state.request_id,
                             changed_fields=["status"])
            return result
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/whatsapp-senders", tags=["communications"])
    def list_wa_senders(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                rows = connection.execute(
                    text("""SELECT id::text AS id, business_name, phone_number, status,
                                   isv_reference, quality_rating, messaging_limit,
                                   decision_note FROM whatsapp_senders
                            WHERE tenant_id=:tenant ORDER BY created_at"""),
                    {"tenant": str(tenant_id)}).mappings().all()
            return {"items": [dict(r) for r in rows]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/whatsapp-templates", tags=["communications"], status_code=201)
    def create_wa_template(tenant_id: uuid.UUID, payload: WaTemplateIn,
                           request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                try:
                    created = create_template(
                        connection, tenant_id=str(tenant_id), name=payload.name,
                        category=payload.category, locale=payload.locale, body=payload.body)
                except WhatsAppModuleError as exc:
                    _fail(exc, "invalid_whatsapp_template")
                insert_audit(connection, tenant_id, identity, action="wa_template.created",
                             resource_type="whatsapp_template", resource_id=created["id"],
                             reason=f"{created['name']}/{created['locale']}",
                             request_id=request.state.request_id,
                             changed_fields=["name", "category", "locale", "body"])
            return created
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/whatsapp-templates/{template_id}/submit", tags=["communications"])
    def submit_wa_template(tenant_id: uuid.UUID, template_id: uuid.UUID,
                           request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                try:
                    result = submit_template(connection, tenant_id=str(tenant_id),
                                             template_id=str(template_id))
                except WhatsAppModuleError as exc:
                    _fail(exc, "wa_template_submit_failed")
                insert_audit(connection, tenant_id, identity, action="wa_template.submitted",
                             resource_type="whatsapp_template", resource_id=result["id"],
                             reason="sent to ISV sandbox",
                             request_id=request.state.request_id,
                             changed_fields=["status"])
            return result
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/whatsapp-templates/{template_id}/decide", tags=["communications"])
    def decide_wa_template(tenant_id: uuid.UUID, template_id: uuid.UUID, payload: WaDecisionIn,
                           request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                try:
                    if payload.note:
                        result = reject_template(connection, tenant_id=str(tenant_id),
                                                 template_id=str(template_id))
                        action = "wa_template.rejected"
                    else:
                        result = approve_template(connection, tenant_id=str(tenant_id),
                                                  template_id=str(template_id))
                        action = "wa_template.approved"
                except WhatsAppModuleError as exc:
                    _fail(exc, "wa_template_decision_failed")
                insert_audit(connection, tenant_id, identity, action=action,
                             resource_type="whatsapp_template", resource_id=result["id"],
                             reason=payload.note or "ISV sandbox approved",
                             request_id=request.state.request_id,
                             changed_fields=["status"])
            return result
        finally:
            engine.dispose()
