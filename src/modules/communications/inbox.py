"""Minimal inbound inbox and staff follow-up tasks (FR-048; TASK-034).

The inbox is triage, not a helpdesk: inbound STOP/HELP/reply events link to
their client (when the number resolves) and open exactly one follow-up task
each. Staff work the task — usually by calling the client back — and resolve
it with a note. There is deliberately no free-form outbound composer here:
outbound sends stay inside the consent-gated dispatcher, so channel windows
and permissions cannot be bypassed from the inbox.

- AC-1: STOP creates an opt-out task alongside its suppression; HELP creates a
  help-request task alongside its auto-reply; any other reply creates a
  reply task. Unknown sender identities resolve to no tenant, so they create
  nothing. Reprocessing the same message returns the existing task.
- AC-2: the list API is tenant-scoped, role-gated (inbox.read), and returns a
  140-char triage preview — never the full body. Task resolution needs
  communications.manage and is audited.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from modules.communications.callbacks import verify_callback_signature
from modules.communications.senders import handle_inbound_sms, resolve_client_for_value

__all__ = [
    "InboxModuleError",
    "PREVIEW_CHARS",
    "create_task_for_inbound",
    "list_inbox",
    "register_inbox_routes",
    "resolve_task",
]

# Triage preview length: enough to route, not enough to leak a full message
# through the list API (AC-2 redaction).
PREVIEW_CHARS = 140

TASK_KIND_BY_ACTION = {
    "suppressed": "opt_out",
    "help_sent": "help_request",
    "logged": "inbound_reply",
}

TASK_TITLE_BY_KIND = {
    "opt_out": "Client opted out",
    "help_request": "Client asked for help",
    "inbound_reply": "Client replied",
}


class InboxModuleError(Exception):
    """Base exception for inbox operations."""


def _preview(body: str | None) -> str:
    text_body = (body or "").strip().replace("\n", " ")
    return text_body[:PREVIEW_CHARS]


def create_task_for_inbound(connection, *, tenant_id: str, inbound_id: str) -> dict | None:
    """Open the follow-up task for one inbound message, idempotently.

    Returns the task (new or existing), or None when the message needs no
    task — today only ``unresolved`` identities, which belong to no tenant.
    STOP/HELP/reply each map to their own kind so staff see why at a glance.
    """
    inbound = connection.execute(
        text("""SELECT id::text AS id, from_value, channel, keyword, action, body,
                       received_at FROM inbound_messages
                WHERE tenant_id=:tenant AND id=:id"""),
        {"tenant": tenant_id, "id": inbound_id}).mappings().one_or_none()
    if inbound is None:
        raise InboxModuleError("unknown inbound message")
    if inbound["action"] == "unresolved":
        return None
    kind = TASK_KIND_BY_ACTION.get(inbound["action"], "inbound_reply")
    resolved = resolve_client_for_value(
        connection, tenant_id=tenant_id,
        contact_value=inbound["from_value"].strip().lower())
    client_id = resolved["client_id"] if resolved else None
    title = TASK_TITLE_BY_KIND[kind]
    detail = (f"{inbound['channel']} {inbound['keyword']} from {inbound['from_value']}: "
              f"{_preview(inbound['body'])}")
    row = connection.execute(
        text("""INSERT INTO follow_up_tasks
                (tenant_id, id, client_id, kind, title, detail,
                 source_type, source_id)
                VALUES (:tenant, :id, :client, :kind, :title, :detail,
                        'inbound_message', :source)
                ON CONFLICT (tenant_id, source_type, source_id) DO NOTHING
                RETURNING id::text AS id, kind, title, status"""),
        {"tenant": tenant_id, "id": str(uuid.uuid4()), "client": client_id,
         "kind": kind, "title": title, "detail": detail, "source": inbound["id"]},
    ).mappings().one_or_none()
    if row is not None:
        return dict(row)
    existing = connection.execute(
        text("""SELECT id::text AS id, kind, title, status FROM follow_up_tasks
                WHERE tenant_id=:tenant AND source_type='inbound_message'
                  AND source_id=:source"""),
        {"tenant": tenant_id, "source": inbound["id"]}).mappings().one()
    return dict(existing)


def list_inbox(connection, *, tenant_id: str, status: str = "open",
               limit: int = 50) -> list[dict]:
    """List follow-up tasks with their linked client and message preview."""
    if status not in ("open", "done", "dismissed", "all"):
        raise InboxModuleError(f"unknown status filter {status!r}")
    filt = "" if status == "all" else "AND t.status = :status"
    rows = connection.execute(
        text(f"""SELECT t.id::text AS id, t.client_id::text AS client_id,
                        t.kind, t.title, t.detail, t.source_type,
                        t.source_id::text AS source_id, t.status,
                        t.created_at, c.name AS client_name,
                        m.from_value, m.channel, m.keyword, m.received_at
                 FROM follow_up_tasks t
                 LEFT JOIN clients c ON c.tenant_id=t.tenant_id AND c.id=t.client_id
                 LEFT JOIN inbound_messages m ON m.tenant_id=t.tenant_id
                                             AND m.id=t.source_id
                 WHERE t.tenant_id=:tenant {filt}
                 ORDER BY t.created_at DESC LIMIT :limit"""),
        {"tenant": tenant_id, "status": status, "limit": limit}).mappings().all()
    items = []
    for r in rows:
        items.append({
            "id": r["id"], "client_id": r["client_id"], "client_name": r["client_name"],
            "kind": r["kind"], "title": r["title"], "detail": r["detail"],
            "source_type": r["source_type"], "source_id": r["source_id"],
            "status": r["status"], "created_at": r["created_at"].isoformat(),
            "from_value": r["from_value"], "channel": r["channel"],
            "keyword": r["keyword"],
            "received_at": r["received_at"].isoformat() if r["received_at"] else None,
        })
    return items


def resolve_task(connection, *, tenant_id: str, task_id: str, resolution: str,
                 resolved_by: str, note: str | None = None) -> dict:
    """Resolve an open task as done or dismissed, with who/when/why."""
    if resolution not in ("done", "dismissed"):
        raise InboxModuleError("resolution must be done or dismissed")
    row = connection.execute(
        text("""UPDATE follow_up_tasks
                SET status=:resolution, resolved_by=:by, resolved_at=now(),
                    resolution_note=:note, version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id AND status='open'
                RETURNING id::text AS id, status"""),
        {"tenant": tenant_id, "id": task_id, "resolution": resolution,
         "by": resolved_by, "note": (note or "")[:500]}).mappings().one_or_none()
    if row is None:
        raise InboxModuleError("unknown task or already resolved")
    return dict(row)


class InboundIn(BaseModel):
    sender_identity: str = Field(min_length=4, max_length=20)
    from_value: str = Field(min_length=3, max_length=320)
    body: str = Field(min_length=1, max_length=2000)
    channel: str = "sms"


class TaskResolveIn(BaseModel):
    resolution: str
    note: str | None = Field(default=None, max_length=500)


def register_inbox_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    @app.post("/v1/tenants/{tenant_id}/inbound/{channel}", tags=["communications"])
    async def receive_inbound(tenant_id: uuid.UUID, channel: str, request: Request) -> dict:
        """Provider inbound webhook. Authenticates by HMAC signature only."""
        if channel not in ("sms", "whatsapp"):
            raise HTTPException(status_code=422, detail={"code": "unknown_channel"})
        from modules.finance import check_provider_rate
        check_provider_rate(str(tenant_id), "provider_inbound")
        raw_body = await request.body()
        try:
            payload = InboundIn.model_validate_json(raw_body)
        except Exception:
            raise HTTPException(status_code=422, detail={"code": "unreadable_inbound"})
        if payload.channel != channel:
            raise HTTPException(status_code=422, detail={"code": "channel_mismatch"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"),
                    {"t": str(tenant_id)})
                secret = connection.execute(
                    text("""SELECT secret FROM provider_webhook_secrets
                            WHERE tenant_id=:t AND channel=:c"""),
                    {"t": str(tenant_id), "c": channel}).scalar_one_or_none()
                signature = request.headers.get("x-provider-signature", "")
                if secret is None or not verify_callback_signature(
                        secret, raw_body, signature):
                    raise HTTPException(status_code=401,
                                        detail={"code": "inbound_rejected"})
                handled = handle_inbound_sms(
                    connection, sender_identity=payload.sender_identity,
                    from_value=payload.from_value, body=payload.body,
                    channel=channel)
                task = None
                if handled["tenant_id"] is not None:
                    created = create_task_for_inbound(
                        connection, tenant_id=handled["tenant_id"],
                        inbound_id=handled["inbound_id"])
                    task = created["id"] if created else None
            return {"action": handled["action"], "task_id": task}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/inbox", tags=["communications"])
    def read_inbox(tenant_id: uuid.UUID, status: str = "open",
                   identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "inbox.read")
                try:
                    items = list_inbox(connection, tenant_id=str(tenant_id),
                                       status=status)
                except InboxModuleError as exc:
                    raise HTTPException(status_code=422, detail={"code": "invalid_filter",
                                                                 "message": str(exc)})
            return {"items": items}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/inbox/{task_id}/resolve", tags=["communications"])
    def close_task(tenant_id: uuid.UUID, task_id: uuid.UUID, payload: TaskResolveIn,
                   request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, user_id, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                try:
                    result = resolve_task(connection, tenant_id=str(tenant_id),
                                          task_id=str(task_id),
                                          resolution=payload.resolution,
                                          resolved_by=str(user_id), note=payload.note)
                except InboxModuleError as exc:
                    raise HTTPException(status_code=422, detail={"code": "resolve_failed",
                                                                 "message": str(exc)})
                insert_audit(connection, tenant_id, identity, action="inbox.task_resolved",
                             resource_type="follow_up_task", resource_id=result["id"],
                             reason=f"{result['status']}: {payload.note or ''}"[:240],
                             request_id=request.state.request_id,
                             changed_fields=["status"])
            return result
        finally:
            engine.dispose()
