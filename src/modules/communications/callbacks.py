"""Signed provider callbacks, retries and dead letters (FR-043, FR-047; TASK-030).

Delivery truth comes only from provider evidence, and provider acceptance is
not delivery:

- AC-1: callbacks authenticate by per-tenant HMAC secret (never by staff
  session). The (communication, provider event id) unique key makes duplicate
  and out-of-order redelivery converge: the first event wins, later copies
  report ``duplicate`` without touching state. An event for an unknown
  reference becomes an ``unknown_reference`` dead letter for reconciliation
  instead of failing silently.
- AC-2: an adapter raise schedules a retry with exponential backoff, claimed
  later by exactly one worker (``SELECT ... FOR UPDATE SKIP LOCKED``). The
  attempt ceiling moves the message to ``exhausted_retries``. A crashed
  worker's ``sending`` rows are reclaimed only after a staleness timeout, so a
  retry never blindly sends twice while the first attempt may still be in
  flight. Manual requeues are audited by the caller (route layer).

State rank (success evidence only moves forward): accepted (1) < delivered (2)
< read (3). ``failed`` evidence applies unless success was already recorded —
success wins over a late failure report.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

__all__ = [
    "MAX_ATTEMPTS",
    "RETRY_BASE_SECONDS",
    "SENDING_STALE_AFTER",
    "claim_due_retries",
    "ingest_callback",
    "queue_retry",
    "reclaim_stale_sending",
    "requeue_dead_letter",
    "sign_callback_body",
    "verify_callback_signature",
]

EVENTS = ("accepted", "delivered", "read", "failed")
EVENT_RANK = {"accepted": 1, "delivered": 2, "read": 3}
# Adapter raises back off 60s, 120s, 240s ... capped by the attempt ceiling.
RETRY_BASE_SECONDS = 60
MAX_ATTEMPTS = 5
# A 'sending' row older than this without progress belonged to a dead worker.
SENDING_STALE_AFTER = timedelta(minutes=5)


class CallbackModuleError(Exception):
    """Base exception for callback operations."""


def sign_callback_body(secret: str, body: bytes) -> str:
    """HMAC-SHA256 hex signature the provider attaches to every callback."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def verify_callback_signature(secret: str, body: bytes, signature: str) -> bool:
    """Constant-time signature check; malformed signatures simply fail."""
    if not secret or not signature:
        return False
    try:
        expected = sign_callback_body(secret, body)
    except Exception:
        return False
    return hmac.compare_digest(expected, signature.strip().lower())


def _webhook_secret(connection, *, tenant_id: str, channel: str) -> str:
    row = connection.execute(
        text("""SELECT secret FROM provider_webhook_secrets
                WHERE tenant_id=:tenant AND channel=:channel"""),
        {"tenant": tenant_id, "channel": channel}).scalar_one_or_none()
    if row is None:
        secret = secrets.token_hex(32)
        connection.execute(
            text("""INSERT INTO provider_webhook_secrets (tenant_id, channel, secret)
                    VALUES (:tenant, :channel, :secret)
                    ON CONFLICT (tenant_id, channel) DO NOTHING"""),
            {"tenant": tenant_id, "channel": channel, "secret": secret})
        row = connection.execute(
            text("""SELECT secret FROM provider_webhook_secrets
                    WHERE tenant_id=:tenant AND channel=:channel"""),
            {"tenant": tenant_id, "channel": channel}).scalar_one()
    return row


def ingest_callback(
    connection,
    *,
    tenant_id: str,
    channel: str,
    raw_body: bytes,
    signature: str,
    event: str,
    provider_event_id: str,
    communication_id: str | None = None,
    provider_ref: str | None = None,
    occurred_at: datetime | None = None,
) -> dict:
    """Verify, dedupe and apply one provider delivery event.

    Returns ``{"status": ...}`` where status is ``applied`` (state may have
    moved), ``duplicate`` (seen before; nothing changed) or ``reconciled``
    (unknown reference; a dead letter now tracks it). Raises
    :class:`CallbackModuleError` on bad signature or unknown event — the
    caller maps those to 401/422, never to a state change.
    """
    if event not in EVENTS:
        raise CallbackModuleError(f"unknown event {event!r}")
    secret = _webhook_secret(connection, tenant_id=tenant_id, channel=channel)
    if not verify_callback_signature(secret, raw_body, signature):
        raise CallbackModuleError("callback signature mismatch")
    if not provider_event_id:
        raise CallbackModuleError("provider event id is required")

    comm_id = None
    comm_trace = None
    if communication_id is not None:
        found = connection.execute(
            text("""SELECT id::text AS id, state, provider_ref, trace_id::text AS trace_id
                    FROM communications
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": communication_id}).mappings().one_or_none()
        if found is not None:
            comm_id = found["id"]
            current_state, current_ref = found["state"], found["provider_ref"]
            comm_trace = found["trace_id"]
    if comm_id is None and provider_ref is not None:
        found = connection.execute(
            text("""SELECT id::text AS id, state, provider_ref, trace_id::text AS trace_id
                    FROM communications
                    WHERE tenant_id=:tenant AND provider_ref=:ref
                    ORDER BY created_at DESC LIMIT 1"""),
            {"tenant": tenant_id, "ref": provider_ref}).mappings().one_or_none()
        if found is not None:
            comm_id = found["id"]
            current_state, current_ref = found["state"], found["provider_ref"]
            comm_trace = found["trace_id"]
    if comm_id is None:
        # Unknown acceptance: never invent delivery, never drop the evidence.
        connection.execute(
            text("""INSERT INTO dead_letters (tenant_id, id, provider_ref, reason, attempts)
                    VALUES (:tenant, :id, :ref, 'unknown_reference', 0)
                    ON CONFLICT DO NOTHING"""),
            {"tenant": tenant_id, "id": str(uuid.uuid4()), "ref": provider_ref},
        )
        return {"status": "reconciled", "reason": "unknown_reference"}

    inserted = connection.execute(
        text("""INSERT INTO provider_callbacks
                (tenant_id, id, communication_id, provider_ref, event,
                 provider_event_id, occurred_at)
                VALUES (:tenant, :id, :comm, :ref, :event, :peid, :at)
                ON CONFLICT (tenant_id, communication_id, provider_event_id)
                DO NOTHING RETURNING id::text AS id"""),
        {"tenant": tenant_id, "id": str(uuid.uuid4()), "comm": comm_id,
         "ref": current_ref, "event": event, "peid": provider_event_id,
         "at": (occurred_at or datetime.now(timezone.utc)).isoformat()},
    ).mappings().one_or_none()
    if inserted is None:
        return {"status": "duplicate", "communication_id": comm_id}
    # TASK-053 / FR-077: the provider hop joins the request's trace.
    if comm_trace is not None:
        from modules.trace import record_trace_event
        record_trace_event(connection, tenant_id=tenant_id, trace_id=comm_trace,
                           span="provider_callback",
                           detail={"communication_id": comm_id, "event": event})

    # Success evidence moves forward only; a late failure never un-delivers.
    if event == "failed":
        if current_state not in ("delivered", "read"):
            connection.execute(
                text("""UPDATE communications SET state='failed',
                               skip_reason='provider reported failure',
                               version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm_id})
            return {"status": "applied", "communication_id": comm_id, "state": "failed"}
        return {"status": "applied", "communication_id": comm_id, "state": current_state}
    new_state = event if event in ("delivered", "read") else current_state
    if event == "accepted" and current_state in ("recorded", "retry_scheduled", "sending"):
        new_state = "dispatched"
    rank_now = EVENT_RANK.get(current_state, 0)
    rank_new = EVENT_RANK.get(new_state if new_state != "dispatched" else "accepted", 0)
    if new_state == "dispatched" or rank_new >= rank_now:
        connection.execute(
            text("""UPDATE communications SET state=:state,
                           version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": comm_id, "state": new_state})
        return {"status": "applied", "communication_id": comm_id, "state": new_state}
    return {"status": "applied", "communication_id": comm_id, "state": current_state}


def queue_retry(connection, *, tenant_id: str, communication_id: str,
                error: str, now: datetime | None = None) -> dict:
    """Schedule the next attempt after an adapter failure, or dead-letter it.

    Attempt numbers are per communication; backoff doubles from 60s. At the
    ceiling the message moves to ``failed``/``exhausted_retries`` instead of
    retrying forever.
    """
    moment = now or datetime.now(timezone.utc)
    last = connection.execute(
        text("""SELECT COALESCE(MAX(attempt), 0) AS n FROM dispatch_attempts
                WHERE tenant_id=:tenant AND communication_id=:comm"""),
        {"tenant": tenant_id, "comm": communication_id}).scalar_one()
    attempt = int(last) + 1
    if attempt > MAX_ATTEMPTS:
        connection.execute(
            text("""INSERT INTO dispatch_attempts
                    (tenant_id, id, communication_id, attempt, state, error)
                    VALUES (:tenant, :id, :comm, :n, 'exhausted', :error)"""),
            {"tenant": tenant_id, "id": str(uuid.uuid4()),
             "comm": communication_id, "n": attempt, "error": error[:240]})
        connection.execute(
            text("""INSERT INTO dead_letters (tenant_id, id, communication_id,
                                              reason, attempts)
                    VALUES (:tenant, :id, :comm, 'exhausted_retries', :n)
                    ON CONFLICT (tenant_id, communication_id)
                    WHERE communication_id IS NOT NULL AND resolved_at IS NULL
                    DO NOTHING"""),
            {"tenant": tenant_id, "id": str(uuid.uuid4()),
             "comm": communication_id, "n": attempt - 1})
        connection.execute(
            text("""UPDATE communications SET state='failed',
                           skip_reason='retry attempts exhausted',
                           version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": communication_id})
        return {"status": "exhausted", "attempt": attempt - 1}
    not_before = moment + timedelta(seconds=RETRY_BASE_SECONDS * (2 ** (attempt - 1)))
    connection.execute(
        text("""UPDATE dispatch_attempts SET state='superseded', updated_at=now()
                WHERE tenant_id=:tenant AND communication_id=:comm AND state='queued'"""),
        {"tenant": tenant_id, "comm": communication_id})
    connection.execute(
        text("""INSERT INTO dispatch_attempts
                (tenant_id, id, communication_id, attempt, state, not_before, error)
                VALUES (:tenant, :id, :comm, :n, 'queued', :nb, :error)"""),
        {"tenant": tenant_id, "id": str(uuid.uuid4()), "comm": communication_id,
         "n": attempt, "nb": not_before.isoformat(), "error": error[:240]})
    connection.execute(
        text("""UPDATE communications SET state='retry_scheduled',
                       version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id"""),
        {"tenant": tenant_id, "id": communication_id})
    return {"status": "retry_scheduled", "attempt": attempt,
            "not_before": not_before.isoformat()}


def claim_due_retries(connection, *, tenant_id: str,
                      now: datetime | None = None, limit: int = 25) -> list[dict]:
    """Claim due retry attempts for this worker; each row goes to one worker.

    ``FOR UPDATE SKIP LOCKED`` is the whole guarantee: two workers racing the
    same queue never hold the same row, so a retry never sends twice.
    """
    moment = (now or datetime.now(timezone.utc)).isoformat()
    rows = connection.execute(
        text("""UPDATE dispatch_attempts SET state='sending', updated_at=now()
                WHERE id IN (
                    SELECT id FROM dispatch_attempts
                    WHERE tenant_id=:tenant AND state='queued' AND not_before <= :now
                    ORDER BY not_before LIMIT :limit
                    FOR UPDATE SKIP LOCKED)
                RETURNING id::text AS id, communication_id::text AS communication_id,
                          attempt"""),
        {"tenant": tenant_id, "now": moment, "limit": limit},
    ).mappings().all()
    claimed = [dict(r) for r in rows]
    if claimed:
        connection.execute(
            text("""UPDATE communications SET state='sending',
                           version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND id = ANY(:comms)"""),
            {"tenant": tenant_id, "comms": [c["communication_id"] for c in claimed]})
    return claimed


def reclaim_stale_sending(connection, *, tenant_id: str,
                          now: datetime | None = None) -> int:
    """Return long-stuck 'sending' work to the queue after a worker death.

    Only rows untouched for longer than SENDING_STALE_AFTER are reclaimed, so
    a live worker's in-flight attempt is never stolen mid-send. This covers
    both retry attempts and first-dispatch claims (the latter have no attempt
    row, so they return to 'recorded'; rows with a live sending attempt are
    reclaimed through the attempt path above and never double-handled).
    """
    moment = now or datetime.now(timezone.utc)
    cutoff = (moment - SENDING_STALE_AFTER).isoformat()
    rows = connection.execute(
        text("""UPDATE dispatch_attempts SET state='queued', updated_at=now()
                WHERE tenant_id=:tenant AND state='sending' AND updated_at <= :cutoff
                RETURNING communication_id::text AS communication_id"""),
        {"tenant": tenant_id, "cutoff": cutoff},
    ).mappings().all()
    comms = [r["communication_id"] for r in rows]
    if comms:
        connection.execute(
            text("""UPDATE communications SET state='retry_scheduled',
                           version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND id = ANY(:comms)"""),
            {"tenant": tenant_id, "comms": comms})
    orphans = connection.execute(
        text("""UPDATE communications SET state='recorded',
                       version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND state='sending' AND updated_at <= :cutoff
                  AND NOT EXISTS (
                      SELECT 1 FROM dispatch_attempts a
                      WHERE a.tenant_id=communications.tenant_id
                        AND a.communication_id=communications.id
                        AND a.state='sending')
                RETURNING id"""),
        {"tenant": tenant_id, "cutoff": cutoff}).mappings().all()
    return len(comms) + len(orphans)


def requeue_dead_letter(connection, *, tenant_id: str, communication_id: str) -> dict:
    """Resolve one open dead letter and requeue its communication (audited upstream).

    Only exhausted retries are requeueable — unknown references have no
    communication to send. A human reviewed the failure, so this schedules one
    fresh attempt immediately (60s backoff); if it also fails the ceiling
    applies again and a new dead letter opens. History is kept, so the audit
    trail never loses the earlier attempts.
    """
    row = connection.execute(
        text("""UPDATE dead_letters SET resolved_at=now()
                WHERE tenant_id=:tenant AND communication_id=:comm
                  AND reason='exhausted_retries' AND resolved_at IS NULL
                RETURNING id::text AS id"""),
        {"tenant": tenant_id, "comm": communication_id}).mappings().one_or_none()
    if row is None:
        raise CallbackModuleError("no open exhausted-retries dead letter for this message")
    # FR-047: manual retries are blocked for opted-out contacts — the human
    # reviews the dead letter, but consent still rules.
    comm = connection.execute(
        text("""SELECT contact_value, channel FROM communications
                WHERE tenant_id=:tenant AND id=:comm"""),
        {"tenant": tenant_id, "comm": communication_id}).mappings().one_or_none()
    if comm is not None:
        from modules.communications.consent import check_suppressed
        if check_suppressed(connection, tenant_id=tenant_id,
                            contact_value=comm["contact_value"],
                            channel=comm["channel"]):
            raise CallbackModuleError("contact opted out; manual retry blocked")
    last = connection.execute(
        text("""SELECT COALESCE(MAX(attempt), 0) AS n FROM dispatch_attempts
                WHERE tenant_id=:tenant AND communication_id=:comm"""),
        {"tenant": tenant_id, "comm": communication_id}).scalar_one()
    connection.execute(
        text("""INSERT INTO dispatch_attempts
                (tenant_id, id, communication_id, attempt, state, not_before, error)
                VALUES (:tenant, :id, :comm, :n, 'queued',
                        :nb, 'manual requeue after exhausted retries')"""),
        {"tenant": tenant_id, "id": str(uuid.uuid4()), "comm": communication_id,
         "n": int(last) + 1,
         "nb": (datetime.now(timezone.utc) + timedelta(seconds=RETRY_BASE_SECONDS)).isoformat()})
    connection.execute(
        text("""UPDATE communications SET state='retry_scheduled',
                       version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id"""),
        {"tenant": tenant_id, "id": communication_id})
    return {"status": "retry_scheduled", "attempt": int(last) + 1}


class CallbackIn(BaseModel):
    event: str
    provider_event_id: str = Field(min_length=1, max_length=120)
    communication_id: str | None = None
    provider_ref: str | None = None
    occurred_at: datetime | None = None


def register_callback_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    @app.post("/v1/tenants/{tenant_id}/provider-callbacks/{channel}", tags=["communications"])
    async def receive_callback(tenant_id: uuid.UUID, channel: str, request: Request) -> dict:
        """Provider delivery callback. Authenticates by HMAC signature only."""
        if channel not in ("email", "sms", "whatsapp"):
            raise HTTPException(status_code=422, detail={"code": "unknown_channel"})
        from modules.finance import check_provider_rate
        check_provider_rate(str(tenant_id), "provider_callbacks")
        raw_body = await request.body()
        try:
            payload = CallbackIn.model_validate_json(raw_body)
        except Exception:
            raise HTTPException(status_code=422, detail={"code": "unreadable_callback"})
        signature = request.headers.get("x-provider-signature", "")
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                # Scope from the path tenant (the only tenant this transaction
                # may touch). Nothing is committed unless the HMAC verifies, so
                # a forged call reads and writes nothing durable.
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"),
                    {"t": str(tenant_id)})
                try:
                    result = ingest_callback(
                        connection, tenant_id=str(tenant_id), channel=channel,
                        raw_body=raw_body,
                        signature=signature, event=payload.event,
                        provider_event_id=payload.provider_event_id,
                        communication_id=payload.communication_id,
                        provider_ref=payload.provider_ref,
                        occurred_at=payload.occurred_at)
                except CallbackModuleError as exc:
                    message = str(exc)
                    raise HTTPException(
                        status_code=401 if "signature" in message else 422,
                        detail={"code": "callback_rejected", "message": message})
            return result
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/dead-letters", tags=["communications"])
    def list_dead_letters(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                rows = connection.execute(
                    text("""SELECT id::text AS id, communication_id::text AS communication_id,
                                   provider_ref, reason, attempts, created_at
                            FROM dead_letters
                            WHERE tenant_id=:tenant AND resolved_at IS NULL
                            ORDER BY created_at"""),
                    {"tenant": str(tenant_id)}).mappings().all()
            return {"items": [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/dead-letters/{communication_id}/retry", tags=["communications"])
    def retry_dead_letter(tenant_id: uuid.UUID, communication_id: uuid.UUID,
                          request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                try:
                    result = requeue_dead_letter(connection, tenant_id=str(tenant_id),
                                                 communication_id=str(communication_id))
                except CallbackModuleError as exc:
                    raise HTTPException(status_code=422, detail={"code": "retry_rejected",
                                                                 "message": str(exc)})
                insert_audit(connection, tenant_id, identity, action="dead_letter.retried",
                             resource_type="communication", resource_id=communication_id,
                             reason="manual requeue from dead letters",
                             request_id=request.state.request_id,
                             changed_fields=["state"])
            return result
        finally:
            engine.dispose()
