"""Request tracing and operational metrics (FR-077/FR-078; TASK-053).

Every request carries a trace id (minted or echoed from ``X-Trace-ID``) from
the public edge through booking, outbox, worker and provider callback. Spans
land in ``trace_events`` so case diagnostics can replay the whole journey by
id (AC-1). The sanitizer is an allowlist: identifiers, states, counts and
timestamps pass; contact values, message bodies, tokens, secrets, emails,
phones and URLs never reach telemetry (AC-2) — a test asserts the block list
against a hostile payload.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import text

__all__ = [
    "TRACE_HEADER",
    "TraceModuleError",
    "metrics_snapshot",
    "new_trace_id",
    "record_trace_event",
    "register_trace_routes",
    "sanitize_for_trace",
]

TRACE_HEADER = "X-Trace-ID"

# Allowlist: the only keys telemetry may carry. Everything else is dropped.
_UUID_KEYS = frozenset({
    "appointment_id", "client_id", "communication_id", "grant_id", "hold_id",
    "location_id", "service_id", "source_id", "staff_id", "tenant_id",
})
_ENUMS = {
    "channel": frozenset({"email", "sms", "whatsapp"}),
    "event": frozenset({"delivered", "failed", "read"}),
    "purpose": frozenset({
        "appointment_reminder_2h", "appointment_reminder_24h", "booking_cancelled",
        "booking_confirmation", "booking_rescheduled", "closure_broadcast",
        "closure_notice", "marketing",
    }),
    "source": frozenset({"appointment"}),
    "state": frozenset({
        "delivered", "dispatched", "failed", "read", "recorded", "sending",
        "skipped_expired", "skipped_stale", "suppressed", "unknown",
    }),
}
_COUNTER_KEYS = frozenset({"attempt", "count", "segments", "status_code"})
_TRACE_SPANS = frozenset({"outbox_record", "worker_send", "provider_callback"})


class TraceModuleError(Exception):
    """Base exception for trace operations."""


def new_trace_id() -> str:
    return str(uuid.uuid4())


def sanitize_for_trace(detail: dict) -> dict:
    """Keep only validated identifiers, bounded counters and known enums."""
    if not isinstance(detail, dict):
        return {}
    clean: dict = {}
    for key, value in (detail or {}).items():
        if key in _UUID_KEYS and isinstance(value, (str, uuid.UUID)):
            try:
                clean[key] = str(uuid.UUID(str(value)))
            except (ValueError, AttributeError):
                continue
        elif key in _ENUMS and isinstance(value, str) and value in _ENUMS[key]:
            clean[key] = value
        elif key in _COUNTER_KEYS and type(value) is int:
            upper_bound = 599 if key == "status_code" else 1000000
            if 0 <= value <= upper_bound:
                clean[key] = value
    return clean


def record_trace_event(connection, *, tenant_id: str, trace_id: str,
                       span: str, detail: dict | None = None) -> str:
    """Append one sanitized span; returns the event id."""
    tenant_id = str(uuid.UUID(tenant_id))
    trace_id = str(uuid.UUID(trace_id))
    if span not in _TRACE_SPANS:
        raise ValueError("unknown trace span")
    event_id = str(uuid.uuid4())
    connection.execute(
        text("""INSERT INTO trace_events (tenant_id, id, trace_id, span, detail)
                VALUES (:tenant, :id, :trace, :span, :detail)"""),
        {"tenant": tenant_id, "id": event_id, "trace": trace_id,
         "span": span, "detail": json.dumps(sanitize_for_trace(detail or {}))})
    return event_id


def trace_timeline(connection, *, tenant_id: str, trace_id: str,
                   limit: int = 200) -> list[dict]:
    """Replay a request's journey for case diagnostics (AC-1)."""
    rows = connection.execute(
        text("""SELECT span, detail, created_at FROM trace_events
                WHERE tenant_id=:tenant AND trace_id=:trace
                ORDER BY created_at LIMIT :limit"""),
        {"tenant": tenant_id, "trace": trace_id, "limit": limit}).mappings().all()
    return [{"span": r["span"], "detail": dict(r["detail"]),
             "created_at": r["created_at"].isoformat()} for r in rows]


def metrics_snapshot(connection, *, tenant_id: str | None = None,
                     now: datetime | None = None) -> dict:
    """Operational counters for FR-078 monitoring (computed, no new state).

    Tenant-scoped when a tenant is given (front-desk mistakes surface per
    salon); company-global when it is not (called through the definer below
    by company roles only).
    """
    moment = now or datetime.now(timezone.utc)
    day_ago = (moment - timedelta(hours=24)).isoformat()
    scope = "" if tenant_id is None else "AND tenant_id=:tenant"
    params = {} if tenant_id is None else {"tenant": tenant_id}

    def one(sql: str):
        return connection.execute(text(sql), {**params, "day_ago": day_ago}).scalar_one()

    return {
        "generated_at": moment.isoformat(),
        "booking_commits_24h": one(
            f"SELECT count(*) FROM appointments WHERE created_at >= :day_ago {scope}"),
        "communications_recorded_24h": one(
            f"SELECT count(*) FROM communications WHERE created_at >= :day_ago {scope}"),
        "callback_failures_24h": one(
            f"""SELECT count(*) FROM provider_callbacks pc
                JOIN communications c ON c.tenant_id=pc.tenant_id AND c.id=pc.communication_id
                WHERE pc.created_at >= :day_ago AND pc.event='failed' {scope.replace('tenant_id', 'pc.tenant_id')}"""),
        "open_retry_queue": one(
            f"SELECT count(*) FROM dispatch_attempts WHERE state='queued' {scope}"),
        "open_dead_letters": one(
            f"SELECT count(*) FROM dead_letters WHERE resolved_at IS NULL {scope}"),
        "failed_communications": one(
            f"SELECT count(*) FROM communications WHERE state='failed' {scope}"),
        "spend_24h_minor": one(
            f"SELECT COALESCE(SUM(cost_minor), 0) FROM message_costs WHERE recorded_at >= :day_ago {scope}"),
    }


def register_trace_routes(
    app: FastAPI,
    *,
    require_company_identity: Callable,
) -> None:
    from modules.company import _require_company, company_scope

    @app.get("/v1/company/traces/{trace_id}", tags=["company"])
    def read_trace(trace_id: uuid.UUID, tenant_id: uuid.UUID, grant_id: uuid.UUID,
                   identity=Depends(require_company_identity)) -> dict:
        """Case diagnostics: replay one request's journey (AC-1).

        Reads ride the same grant discipline as diagnostics: an active grant
        for the tenant must cover the read, and the read is logged where the
        tenant can see it.
        """
        from modules.support import _grant_valid
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity, lock=True)
                _require_company(role, "company.support.manage")
                grant = _grant_valid(connection, grant_id=str(grant_id),
                                     tenant_id=str(tenant_id), identity=identity)
                if grant is None:
                    raise HTTPException(status_code=403, detail={
                        "code": "grant_not_authorized",
                        "message": "no active grant covers this read"})
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"),
                    {"t": str(tenant_id)})
                timeline = trace_timeline(connection, tenant_id=str(tenant_id),
                                          trace_id=str(trace_id))
                connection.execute(
                    text("""INSERT INTO support_grant_uses
                            (grant_id, tenant_id, actor_issuer, actor_subject, action)
                            VALUES (:grant, :tenant, :issuer, :subject, 'trace_read')"""),
                    {"grant": str(grant_id), "tenant": str(tenant_id),
                     "issuer": identity.issuer, "subject": identity.subject})
            return {"trace_id": str(trace_id), "tenant_id": str(tenant_id),
                    "spans": timeline}
        finally:
            engine.dispose()
