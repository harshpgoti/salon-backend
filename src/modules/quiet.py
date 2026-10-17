"""Quiet hours, channel preferences and safe fallback (FR-046/FR-047; TASK-032).

- AC-1: the tenant quiet window, the per-recipient daily cap and the
  per-client primary/fallback channels are enforced before every send. Due
  sends inside quiet defer to its end (still useful) or expire past it (the
  expired gate catches them next run); over-cap sends defer a day. The
  emergency stop defers everything, visibly.
- AC-2: fallback fires only after a provider's explicit definitive rejection,
  and only to a separately eligible channel (its own consent and suppression
  state). An ambiguous provider timeout takes the retry path, including after
  retries exhaust, so nothing blasts a second channel on a maybe. Manual
  retries refuse opted-out contacts. Consent writes and dispatch checks share
  the dispatch transaction, so the unsubscribe race cannot slip between them.

Product defaults (no rows): primary email, no fallback, 21:00-08:00 quiet,
no cap, stop off.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

__all__ = [
    "DEFAULT_PRIMARY_CHANNEL",
    "QuietModuleError",
    "defer_past_quiet",
    "load_messaging_settings",
    "register_quiet_routes",
    "resolve_primary_channel",
    "try_fallback_on_exhaustion",
]

DEFAULT_PRIMARY_CHANNEL = "email"
DEFAULT_QUIET_START = time(21, 0)
DEFAULT_QUIET_END = time(8, 0)


class QuietModuleError(Exception):
    """Base exception for quiet-hours operations."""


def load_messaging_settings(connection, *, tenant_id: str) -> dict:
    """Tenant settings; absent rows use the product quiet-window default.

    A persisted row with both quiet values NULL is the explicit opt-out.
    """
    row = connection.execute(
        text("""SELECT quiet_start, quiet_end, emergency_stop,
                       max_per_recipient_per_day FROM messaging_settings
                WHERE tenant_id=:tenant"""),
        {"tenant": tenant_id}).mappings().one_or_none()
    if row is None:
        return {"quiet_start": DEFAULT_QUIET_START, "quiet_end": DEFAULT_QUIET_END,
                "emergency_stop": False, "max_per_recipient_per_day": None}
    return {"quiet_start": row["quiet_start"], "quiet_end": row["quiet_end"],
            "emergency_stop": row["emergency_stop"],
            "max_per_recipient_per_day": row["max_per_recipient_per_day"]}


def resolve_primary_channel(connection, *, tenant_id: str, client_id: str,
                            purpose: str) -> dict:
    """Primary + fallback channel for one client and purpose.

    Specific purpose row wins, else the 'default' row, else the product
    default (email, no fallback).
    """
    row = connection.execute(
        text("""SELECT primary_channel, fallback_channel FROM client_channel_prefs
                WHERE tenant_id=:tenant AND client_id=:client AND purpose=:purpose"""),
        {"tenant": tenant_id, "client": client_id, "purpose": purpose},
    ).mappings().one_or_none()
    if row is None:
        row = connection.execute(
            text("""SELECT primary_channel, fallback_channel FROM client_channel_prefs
                    WHERE tenant_id=:tenant AND client_id=:client AND purpose='default'"""),
            {"tenant": tenant_id, "client": client_id}).mappings().one_or_none()
    if row is None:
        return {"primary_channel": DEFAULT_PRIMARY_CHANNEL, "fallback_channel": None,
                "configured": False}
    return {"primary_channel": row["primary_channel"],
            "fallback_channel": row["fallback_channel"], "configured": True}


def _location_now(connection, *, tenant_id: str, now: datetime):
    """Current local time in the tenant's primary location timezone."""
    tzname = connection.execute(
        text("""SELECT timezone FROM locations
                WHERE tenant_id=:tenant ORDER BY created_at LIMIT 1"""),
        {"tenant": tenant_id}).scalar_one_or_none() or "UTC"
    from zoneinfo import ZoneInfo
    try:
        return now.astimezone(ZoneInfo(tzname)), tzname
    except Exception:
        return now.astimezone(ZoneInfo("UTC")), "UTC"


def in_quiet_hours(quiet_start: time | None, quiet_end: time | None, local_now) -> bool:
    """True inside the window, including overnight wraps (21:00-08:00).

    Both values must be set; a persisted pair of None values explicitly turns
    the default quiet window off.
    """
    if quiet_start is None or quiet_end is None:
        return False
    current = local_now.time()
    if quiet_start <= quiet_end:
        return quiet_start <= current < quiet_end
    return current >= quiet_start or current < quiet_end


def defer_past_quiet(connection, *, tenant_id: str, communication_id: str,
                     now: datetime) -> datetime:
    """Push a due send to the quiet end; returns the new scheduled time."""
    settings = load_messaging_settings(connection, tenant_id=tenant_id)
    local_now, _ = _location_now(connection, tenant_id=tenant_id, now=now)
    end = datetime.combine(local_now.date(), settings["quiet_end"],
                           tzinfo=local_now.tzinfo)
    if end <= local_now:
        end = end + timedelta(days=1)
    connection.execute(
        text("""UPDATE communications SET scheduled_at=:at,
                       version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id"""),
        {"tenant": tenant_id, "id": communication_id, "at": end.isoformat()})
    return end


def try_fallback_on_exhaustion(connection, *, tenant_id: str, comm: dict,
                               now: datetime) -> dict | None:
    """Record a fallback after the provider definitively rejected a send (AC-2).

    Returns the new communication row, or None when no fallback applies:
    none configured, same channel, or the fallback channel ineligible
    (consent/suppression checked separately here — never inherited from the
    failed channel). Ambiguous outcomes never reach this function, even after
    retries exhaust; they remain in the dead-letter/manual-review path.
    """
    from modules.communications.consent import check_consented, check_suppressed
    prefs = resolve_primary_channel(connection, tenant_id=tenant_id,
                                    client_id=comm["client_id"],
                                    purpose=comm["purpose"])
    fallback = prefs["fallback_channel"]
    if not fallback or fallback == comm["channel"]:
        return None
    # The fallback address fits its channel: a phone number for SMS/WhatsApp,
    # an email for email — falling back to the primary contact only when no
    # kind-matching contact exists.
    want_kind = "phone" if fallback in ("sms", "whatsapp") else "email"
    contact = connection.execute(
        text("""SELECT normalized_value FROM client_contacts
                WHERE tenant_id=:tenant AND client_id=:client AND kind=:kind
                ORDER BY is_primary DESC, created_at LIMIT 1"""),
        {"tenant": tenant_id, "client": comm["client_id"], "kind": want_kind},
    ).mappings().one_or_none()
    if contact is None:
        return None
    if not check_consented(connection, tenant_id=tenant_id, client_id=comm["client_id"],
                           channel=fallback, purpose=comm["purpose"], now=now):
        return None
    if check_suppressed(connection, tenant_id=tenant_id,
                        contact_value=contact["normalized_value"], channel=fallback):
        return None
    key = (f"{comm['source_type']}:{comm['source_id']}:{comm['purpose']}"
           f":v{comm['expected_entity_version']}:{fallback}:{comm['client_id']}:fallback")
    row = connection.execute(
        text("""INSERT INTO communications
                (tenant_id, id, client_id, contact_value, channel, purpose,
                 source_type, source_id, expected_entity_version, fallback_of, state,
                 scheduled_at, expires_at, dedupe_key, trace_id)
                VALUES (:tenant, :id, :client, :contact, :channel, :purpose,
                        :source_type, :source, :version, :fallback_of, 'recorded',
                        :scheduled, :expires, :dedupe, :trace)
                ON CONFLICT (tenant_id, dedupe_key) DO NOTHING
                RETURNING id::text AS id"""),
        {"tenant": tenant_id, "id": str(uuid.uuid4()), "client": comm["client_id"],
         "contact": contact["normalized_value"], "channel": fallback,
         "purpose": comm["purpose"], "source_type": comm["source_type"],
         "source": comm["source_id"], "fallback_of": comm["id"],
         "version": comm["expected_entity_version"], "scheduled": now.isoformat(),
         "expires": comm["expires_at"].isoformat() if comm["expires_at"] else None,
         "dedupe": key, "trace": comm.get("trace_id")},
    ).mappings().one_or_none()
    return {"id": row["id"], "channel": fallback} if row else None


class SettingsIn(BaseModel):
    # Both or neither: a half window defers nothing and means nothing.
    quiet_start: str | None = Field(default=None, pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$")
    quiet_end: str | None = Field(default=None, pattern="^([01][0-9]|2[0-3]):[0-5][0-9]$")
    emergency_stop: bool = False
    max_per_recipient_per_day: int | None = Field(default=None, ge=1)


class PrefsIn(BaseModel):
    purpose: str = Field(min_length=1, max_length=80, default="default")
    primary_channel: str = Field(pattern="^(email|sms|whatsapp)$")
    fallback_channel: str | None = Field(default=None, pattern="^(email|sms|whatsapp)$")


def register_quiet_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    @app.get("/v1/tenants/{tenant_id}/messaging/settings", tags=["communications"])
    def read_settings(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                settings = load_messaging_settings(connection, tenant_id=str(tenant_id))
            start = settings["quiet_start"]
            end = settings["quiet_end"]
            return {"quiet_start": start.strftime("%H:%M") if start else None,
                    "quiet_end": end.strftime("%H:%M") if end else None,
                    "emergency_stop": settings["emergency_stop"],
                    "max_per_recipient_per_day": settings["max_per_recipient_per_day"]}
        finally:
            engine.dispose()

    @app.put("/v1/tenants/{tenant_id}/messaging/settings", tags=["communications"])
    def write_settings(tenant_id: uuid.UUID, payload: SettingsIn,
                       request: Request, identity=Depends(require_identity)) -> dict:
        if (payload.quiet_start is None) != (payload.quiet_end is None):
            raise HTTPException(status_code=422, detail={
                "code": "half_quiet_window",
                "message": "quiet start and end come together, or neither (off)"})
        if (payload.quiet_start is not None
                and payload.quiet_start == payload.quiet_end):
            raise HTTPException(status_code=422, detail={
                "code": "meaningless_quiet_window",
                "message": "quiet start and end must differ"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "communications.manage")
                connection.execute(
                    text("""INSERT INTO messaging_settings
                            (tenant_id, quiet_start, quiet_end, emergency_stop,
                             max_per_recipient_per_day)
                            VALUES (:tenant, :start, :end, :stop, :cap)
                            ON CONFLICT (tenant_id) DO UPDATE SET
                                quiet_start=EXCLUDED.quiet_start,
                                quiet_end=EXCLUDED.quiet_end,
                                emergency_stop=EXCLUDED.emergency_stop,
                                max_per_recipient_per_day=EXCLUDED.max_per_recipient_per_day,
                                version=messaging_settings.version+1, updated_at=now()"""),
                    {"tenant": str(tenant_id), "start": payload.quiet_start,
                     "end": payload.quiet_end, "stop": payload.emergency_stop,
                     "cap": payload.max_per_recipient_per_day})
                insert_audit(connection, tenant_id, identity, action="messaging.settings_set",
                             resource_type="messaging_settings", resource_id=str(tenant_id),
                             reason=f"quiet {payload.quiet_start}-{payload.quiet_end} "
                                    f"stop={payload.emergency_stop}",
                             request_id=request.state.request_id,
                             changed_fields=["quiet_start", "quiet_end", "emergency_stop"])
            return {"quiet_start": payload.quiet_start, "quiet_end": payload.quiet_end,
                    "emergency_stop": payload.emergency_stop,
                    "max_per_recipient_per_day": payload.max_per_recipient_per_day}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/clients/{client_id}/channel-prefs", tags=["communications"])
    def read_prefs(tenant_id: uuid.UUID, client_id: uuid.UUID,
                   identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.read")
                rows = connection.execute(
                    text("""SELECT purpose, primary_channel, fallback_channel
                            FROM client_channel_prefs
                            WHERE tenant_id=:tenant AND client_id=:client"""),
                    {"tenant": str(tenant_id), "client": str(client_id)}).mappings().all()
            return {"items": [dict(r) for r in rows]}
        finally:
            engine.dispose()

    @app.put("/v1/tenants/{tenant_id}/clients/{client_id}/channel-prefs", tags=["communications"])
    def write_prefs(tenant_id: uuid.UUID, client_id: uuid.UUID, payload: PrefsIn,
                    request: Request, identity=Depends(require_identity)) -> dict:
        if payload.fallback_channel == payload.primary_channel:
            raise HTTPException(status_code=422, detail={
                "code": "self_fallback",
                "message": "fallback must differ from primary (or be absent)"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                owned = connection.execute(
                    text("SELECT 1 FROM clients WHERE tenant_id=:t AND id=:c"),
                    {"t": str(tenant_id), "c": str(client_id)}).scalar_one_or_none()
                if owned is None:
                    raise HTTPException(status_code=404, detail={"code": "client_not_found"})
                connection.execute(
                    text("""INSERT INTO client_channel_prefs
                            (tenant_id, client_id, purpose, primary_channel, fallback_channel)
                            VALUES (:tenant, :client, :purpose, :primary, :fallback)
                            ON CONFLICT (tenant_id, client_id, purpose) DO UPDATE SET
                                primary_channel=EXCLUDED.primary_channel,
                                fallback_channel=EXCLUDED.fallback_channel,
                                version=client_channel_prefs.version+1, updated_at=now()"""),
                    {"tenant": str(tenant_id), "client": str(client_id),
                     "purpose": payload.purpose, "primary": payload.primary_channel,
                     "fallback": payload.fallback_channel})
                insert_audit(connection, tenant_id, identity, action="client.channel_prefs_set",
                             resource_type="client", resource_id=client_id,
                             reason=f"{payload.purpose}: {payload.primary_channel}",
                             request_id=request.state.request_id,
                             changed_fields=["primary_channel", "fallback_channel"])
            return {"client_id": str(client_id), "purpose": payload.purpose,
                    "primary_channel": payload.primary_channel,
                    "fallback_channel": payload.fallback_channel}
        finally:
            engine.dispose()
