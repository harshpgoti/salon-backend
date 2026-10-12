"""SMS sender setup and delivery workflows (FR-044; TASK-029).

A salon sends SMS only through its own verified sender identity. Registration
is synthetic (a verification code is issued, the owner proves receipt by
echoing it back), but the invariants are real:

- AC-1: a sender identity belongs to exactly one tenant (global UNIQUE), and
  only ``verified`` senders route SMS. Dispatch stamps the sender identity on
  every send so the brand the client sees is always their own salon's.
- AC-2: inbound STOP creates/refreshes a suppression (unresolved numbers get a
  client-less suppression row); HELP answers with help text and never
  suppresses; every dispatch writes a CAD segment-cost ledger row.

DQ-006 applies: the provider here is the recording adapter. No real vendor is
called, registered or stored anywhere in this task.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from modules.communications.consent import normalize_contact_value, record_suppression

__all__ = [
    "SMS_COST_MINOR_PER_SEGMENT",
    "get_verified_sender",
    "handle_inbound_sms",
    "record_message_cost",
    "register_sender",
    "register_sender_routes",
    "resolve_client_for_value",
    "suspend_sender",
    "verify_sender",
]

# Synthetic dev-only rate so spend is visible before campaigns. Not a vendor
# quote and not a product price (DQ-005): one cent CAD per SMS segment.
SMS_COST_MINOR_PER_SEGMENT = 1
SMS_SEGMENT_CHARS = 160

_IDENTITY_RE = re.compile(r"^(\+[1-9][0-9]{7,14}|[0-9]{4,6})$")


class SenderModuleError(Exception):
    """Base exception for sender operations."""


def _check_identity(sender_identity: str) -> str:
    value = sender_identity.strip().replace(" ", "").replace("-", "")
    if not _IDENTITY_RE.match(value):
        raise SenderModuleError(
            "sender identity must be E.164 (+14165550132) or a 4-6 digit short code")
    return value


def register_sender(connection, *, tenant_id: str, label: str, sender_identity: str) -> dict:
    """Register a sender identity as pending and issue a verification code.

    The returned ``verification_code`` is the synthetic proof-of-receipt the
    owner echoes back through ``verify_sender``. The global UNIQUE on
    ``sender_identity`` rejects a number already serving another salon.
    """
    identity = _check_identity(sender_identity)
    clean_label = label.strip()
    if not (1 <= len(clean_label) <= 80):
        raise SenderModuleError("label must be 1-80 characters")
    code = f"{secrets.randbelow(900000) + 100000:06d}"
    try:
        row = connection.execute(
            text("""INSERT INTO sms_senders
                    (tenant_id, id, label, sender_identity, verification_reference)
                    VALUES (:tenant, :id, :label, :identity, :code)
                    RETURNING id, label, sender_identity, status"""),
            {"tenant": tenant_id, "id": str(uuid.uuid4()), "label": clean_label,
             "identity": identity, "code": code},
        ).mappings().one()
    except IntegrityError as exc:
        raise SenderModuleError(
            "this sender identity is already registered to a salon; "
            "sender numbers are never pooled across salons") from exc
    return {**dict(row), "verification_code": code}


def verify_sender(connection, *, tenant_id: str, sender_id: str, code: str) -> dict:
    """Confirm a pending sender with its verification code.

    A wrong code leaves the row pending and reports the mismatch; it never
    flips state. Suspended senders cannot be re-verified into service here.
    """
    row = connection.execute(
        text("""SELECT id::text AS id, status FROM sms_senders
                WHERE tenant_id=:tenant AND id=:id"""),
        {"tenant": tenant_id, "id": sender_id}).mappings().one_or_none()
    if row is None:
        raise SenderModuleError("unknown sender")
    if row["status"] == "verified":
        return {"id": row["id"], "status": "verified", "already": True}
    if row["status"] != "pending":
        raise SenderModuleError(f"sender is {row['status']} and cannot be verified")
    updated = connection.execute(
        text("""UPDATE sms_senders SET status='verified', verified_at=now(),
                       version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id
                  AND status='pending' AND verification_reference=:code
                RETURNING id::text AS id, status"""),
        {"tenant": tenant_id, "id": sender_id, "code": code.strip()},
    ).mappings().one_or_none()
    if updated is None:
        raise SenderModuleError("verification code does not match; sender stays pending")
    return {**dict(updated), "already": False}


def suspend_sender(connection, *, tenant_id: str, sender_id: str) -> dict:
    """Suspend a sender so it stops routing immediately."""
    row = connection.execute(
        text("""UPDATE sms_senders SET status='suspended',
                       version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id AND status <> 'suspended'
                RETURNING id::text AS id, status"""),
        {"tenant": tenant_id, "id": sender_id}).mappings().one_or_none()
    if row is None:
        raise SenderModuleError("unknown sender or already suspended")
    return dict(row)


def get_verified_sender(connection, *, tenant_id: str) -> dict | None:
    """Return the tenant's verified SMS sender, if exactly the routing one.

    When several are verified the oldest verified wins deterministically, so
    dispatch never flaps between identities.
    """
    row = connection.execute(
        text("""SELECT id::text AS id, label, sender_identity, verified_at FROM sms_senders
                WHERE tenant_id=:tenant AND status='verified'
                ORDER BY verified_at NULLS LAST, created_at LIMIT 1"""),
        {"tenant": tenant_id}).mappings().one_or_none()
    return dict(row) if row else None


def resolve_client_for_value(connection, *, tenant_id: str, contact_value: str) -> dict | None:
    """Resolve a normalized contact value to its client/contact ids, if known."""
    row = connection.execute(
        text("""SELECT client_id::text AS client_id, id::text AS contact_id
                FROM client_contacts
                WHERE tenant_id=:tenant AND normalized_value=:value
                ORDER BY is_primary DESC, created_at LIMIT 1"""),
        {"tenant": tenant_id, "value": contact_value}).mappings().one_or_none()
    return dict(row) if row else None


def handle_inbound_sms(connection, *, sender_identity: str, from_value: str,
                       body: str, channel: str = "sms") -> dict:
    """Handle one inbound message from the (recording) provider.

    The sender identity maps to exactly one tenant — an unknown identity is
    logged as unresolved and no tenant state changes. STOP (any case,
    surrounding whitespace) creates or refreshes a suppression for the
    normalized sender value on the same channel, resolving the client when
    the number is known. HELP answers with help text and never suppresses.
    Anything else is logged for the staff inbox (TASK-034). A WhatsApp inbound
    additionally opens the 24h conversation window (TASK-031).
    """
    if channel not in ("sms", "whatsapp"):
        raise SenderModuleError(f"unknown channel {channel!r}")
    identity = _check_identity(sender_identity)
    if channel == "sms":
        sender = connection.execute(
            text("""SELECT tenant_id::text AS tenant_id, id::text AS id FROM sms_senders
                    WHERE sender_identity=:identity"""),
            {"identity": identity}).mappings().one_or_none()
    else:
        sender = connection.execute(
            text("""SELECT tenant_id::text AS tenant_id, id::text AS id FROM whatsapp_senders
                    WHERE phone_number=:identity"""),
            {"identity": identity}).mappings().one_or_none()
    if sender is None:
        return {"action": "unresolved", "tenant_id": None}
    tenant_id = sender["tenant_id"]
    keyword = body.strip().lower()
    action = "logged" if keyword not in ("stop", "help") else ("suppressed" if keyword == "stop" else "help_sent")
    if keyword == "stop":
        value = normalize_contact_value(from_value)
        resolved = resolve_client_for_value(connection, tenant_id=tenant_id, contact_value=value)
        # Client-less suppression when the number is unknown: the value block
        # is what protects future sends (TASK-028 keying).
        record_suppression(connection, tenant_id=tenant_id,
                           client_id=resolved["client_id"] if resolved else None,
                           contact_value=value, channel=channel, reason="stop",
                           source="provider")
    inbound_id = str(uuid.uuid4())
    connection.execute(
        text("""INSERT INTO inbound_messages
                (tenant_id, id, sender_id, from_value, channel, keyword, action, body)
                VALUES (:tenant, :id, :sender, :from, :channel, :keyword, :action, :body)"""),
        {"tenant": tenant_id, "id": inbound_id,
         "sender": sender["id"] if channel == "sms" else None,
         "from": from_value.strip(), "channel": channel,
         "keyword": keyword if keyword in ("stop", "help") else "other",
         "action": action, "body": body[:2000]},
    )
    result: dict = {"action": action, "tenant_id": tenant_id,
                    "inbound_id": inbound_id}
    if keyword == "help":
        result["reply"] = ("Thanks for contacting your salon. Reply STOP to opt out "
                           "of SMS reminders. For anything else, call the salon directly.")
    return result


def record_message_cost(connection, *, tenant_id: str, communication_id: str,
                        channel: str, body_chars: int | None = None) -> dict:
    """Write the CAD segment-cost ledger row for one dispatched message.

    Synthetic dev-only rates (not vendor quotes, not product prices): SMS and
    WhatsApp cost one cent per 160-char segment; email keeps a zero-cost row
    so spend reports can distinguish "free channel" from "unrecorded".
    Idempotent per communication: replays keep the first row.
    """
    rates = {"sms": SMS_COST_MINOR_PER_SEGMENT, "whatsapp": SMS_COST_MINOR_PER_SEGMENT}
    per_segment = rates.get(channel, 0)
    if per_segment:
        chars = body_chars if body_chars and body_chars > 0 else SMS_SEGMENT_CHARS
        segments = (chars + SMS_SEGMENT_CHARS - 1) // SMS_SEGMENT_CHARS
        cost = segments * per_segment
    else:
        segments, cost = 1, 0
    row = connection.execute(
        text("""INSERT INTO message_costs
                (tenant_id, communication_id, channel, segments, cost_minor)
                VALUES (:tenant, :comm, :channel, :segments, :cost)
                ON CONFLICT (tenant_id, communication_id) DO NOTHING
                RETURNING communication_id::text AS communication_id, segments, cost_minor"""),
        {"tenant": tenant_id, "comm": communication_id, "channel": channel,
         "segments": segments, "cost": cost},
    ).mappings().one_or_none()
    return dict(row) if row else {"communication_id": communication_id,
                                  "segments": segments, "cost_minor": cost, "replayed": True}


class SenderIn(BaseModel):
    label: str = Field(min_length=1, max_length=80)
    sender_identity: str = Field(min_length=4, max_length=20)


class SenderVerifyIn(BaseModel):
    code: str = Field(min_length=6, max_length=6, pattern="^[0-9]{6}$")


def register_sender_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    @app.post("/v1/tenants/{tenant_id}/sms-senders", tags=["communications"], status_code=201)
    def create_sender(tenant_id: uuid.UUID, payload: SenderIn,
                      request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                try:
                    created = register_sender(
                        connection, tenant_id=str(tenant_id),
                        label=payload.label, sender_identity=payload.sender_identity)
                except SenderModuleError as exc:
                    raise HTTPException(status_code=422, detail={"code": "invalid_sender",
                                                                 "message": str(exc)})
                insert_audit(connection, tenant_id, identity, action="sender.registered",
                             resource_type="sms_sender", resource_id=created["id"],
                             reason=created["sender_identity"],
                             request_id=request.state.request_id,
                             changed_fields=["label", "sender_identity"])
            return {"id": created["id"], "label": created["label"],
                    "sender_identity": created["sender_identity"], "status": created["status"],
                    "verification_code": created["verification_code"]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/sms-senders/{sender_id}/verify", tags=["communications"])
    def confirm_sender(tenant_id: uuid.UUID, sender_id: uuid.UUID, payload: SenderVerifyIn,
                       request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                try:
                    result = verify_sender(connection, tenant_id=str(tenant_id),
                                           sender_id=str(sender_id), code=payload.code)
                except SenderModuleError as exc:
                    raise HTTPException(status_code=422, detail={"code": "verification_failed",
                                                                 "message": str(exc)})
                insert_audit(connection, tenant_id, identity, action="sender.verified",
                             resource_type="sms_sender", resource_id=result["id"],
                             reason="verification code confirmed",
                             request_id=request.state.request_id,
                             changed_fields=["status"])
            return result
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/sms-senders", tags=["communications"])
    def list_senders(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                rows = connection.execute(
                    text("""SELECT id::text AS id, label, sender_identity, status,
                                   verified_at FROM sms_senders
                            WHERE tenant_id=:tenant ORDER BY created_at"""),
                    {"tenant": str(tenant_id)}).mappings().all()
            return {"items": [{**dict(r), "verified_at": r["verified_at"].isoformat()
                                              if r["verified_at"] else None} for r in rows]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/sms-senders/{sender_id}/suspend", tags=["communications"])
    def freeze_sender(tenant_id: uuid.UUID, sender_id: uuid.UUID,
                      request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                try:
                    result = suspend_sender(connection, tenant_id=str(tenant_id),
                                            sender_id=str(sender_id))
                except SenderModuleError as exc:
                    raise HTTPException(status_code=422, detail={"code": "suspend_failed",
                                                                 "message": str(exc)})
                insert_audit(connection, tenant_id, identity, action="sender.suspended",
                             resource_type="sms_sender", resource_id=result["id"],
                             reason="sender suspended by staff",
                             request_id=request.state.request_id,
                             changed_fields=["status"])
            return result
        finally:
            engine.dispose()
