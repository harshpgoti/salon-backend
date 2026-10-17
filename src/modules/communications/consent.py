"""Communication consent evidence and opt-out suppression (FR-040, FR-041; TASK-028).

Two facts, both tenant-owned:

- **consents** - a versioned history of grants and revocations per client,
  channel and purpose. Each row carries proof (source, wording version,
  reference) so a later audit can show *why* a message was allowed.
- **suppressions** - one row per opted-out contact value and channel. Keyed by
  the normalized contact *value* (not the client row) so an unsubscribe or
  STOP keeps blocking sends even if the client record is later edited, merged
  or re-imported.

AC-1: booking and import flows may record consent for transactional purposes
only. A booking/import source asking for a ``marketing`` purpose is rejected
here — marketing reach requires an explicit grant (source ``client_self``,
``staff`` or ``marketing_form``), never a silent side effect of booking.

AC-2: the dispatcher re-checks suppressions immediately before every send
(see ``outbox.dispatch_due_communications``). A suppression written after a
message was recorded — the unsubscribe/STOP race — moves that message to
``suppressed`` instead of dispatching it.
"""

from __future__ import annotations

import uuid
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

__all__ = [
    "check_consented",
    "check_suppressed",
    "normalize_contact_value",
    "record_consent",
    "record_suppression",
    "issue_email_unsubscribe_url",
    "lock_contact_channel",
    "register_consent_routes",
]

CHANNELS = ("email", "sms", "whatsapp")
CONSENT_STATUSES = ("granted", "revoked")
SUPPRESSION_REASONS = ("unsubscribe", "stop", "bounce", "complaint", "channel_unavailable")
# Sources that may only ever record transactional consent, never marketing.
IMPLIED_SOURCES = ("booking", "import")
MARKETING_PURPOSE = "marketing"
# Purposes that need a marketing grant to send. closure_broadcast rides the
# same grant: location-wide closure news follows purpose policy (TASK-033).
CONSENT_GATED_PURPOSES = frozenset({"marketing", "closure_broadcast"})


class ConsentModuleError(Exception):
    """Base exception for consent operations."""


class SuppressionModuleError(Exception):
    """Base exception for suppression operations."""


class PublicUnsubscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


def normalize_contact_value(value: str) -> str:
    """Display-level normalization for suppression keys.

    Lower-cases and strips surrounding whitespace. Phone digits are unaffected
    by lower-casing; canonical CRM normalization owns anything stricter. The
    same function is applied on record and on check, so both sides agree.
    """
    return value.strip().lower()


def lock_contact_channel(connection, *, tenant_id: str, contact_value: str, channel: str) -> None:
    """Serialize grants, opt-outs and provider authorization for one destination."""
    value = normalize_contact_value(contact_value)
    lock_key = f"{tenant_id}:{channel}:{value}"
    connection.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": lock_key},
    )


def record_consent(
    connection,
    *,
    tenant_id: str,
    client_id: str,
    contact_id: str,
    channel: str,
    purpose: str,
    source: str,
    wording_version: str,
    status: str = "granted",
    proof_reference: str | None = None,
    expires_at: datetime | None = None,
    lawful_basis_review: str | None = None,
) -> dict:
    """Append one consent row (grant or revocation) with its proof.

    History is append-only: revoking writes a new ``revoked`` row rather than
    editing the grant, so the evidence trail survives. Raises
    :class:`ConsentModuleError` when a booking/import source asks for a
    marketing purpose (AC-1: no silent opt-in).
    """
    if channel not in CHANNELS:
        raise ConsentModuleError(f"unknown channel {channel!r}")
    if status not in CONSENT_STATUSES:
        raise ConsentModuleError(f"unknown status {status!r}")
    if source in IMPLIED_SOURCES and purpose == MARKETING_PURPOSE:
        raise ConsentModuleError(
            f"source {source!r} cannot grant marketing consent; "
            "booking and import never opt a client in silently (AC-1)")
    contact = connection.execute(
        text("""SELECT kind, lower(btrim(normalized_value)) AS contact_value
                FROM client_contacts
                WHERE tenant_id=:tenant AND client_id=:client AND id=:contact
                FOR SHARE"""),
        {"tenant": tenant_id, "client": client_id, "contact": contact_id},
    ).mappings().one_or_none()
    expected_kind = "email" if channel == "email" else "phone"
    if contact is None or contact["kind"] != expected_kind:
        raise ConsentModuleError("contact does not belong to this client and channel")
    lock_contact_channel(connection, tenant_id=tenant_id,
                         contact_value=contact["contact_value"], channel=channel)
    event_sequence = connection.execute(
        text("""SELECT COALESCE(MAX(event_sequence), 0) + 1 FROM consents
                WHERE tenant_id=:tenant AND client_id=:client AND contact_id=:contact
                  AND channel=:channel AND purpose=:purpose"""),
        {"tenant": tenant_id, "client": client_id, "contact": contact_id,
         "channel": channel, "purpose": purpose},
    ).scalar_one()
    row = connection.execute(
        text("""INSERT INTO consents
                (tenant_id, id, client_id, contact_id, contact_value_snapshot, channel, purpose,
                 event_sequence,
                 source, wording_version, status, proof_reference,
                 expires_at, lawful_basis_review, revoked_at)
                VALUES (:tenant, :id, :client, :contact, :contact_value, :channel, :purpose,
                        :event_sequence,
                        :source, :wording, :status, :proof, :expires, :review,
                        CASE WHEN :status='revoked' THEN now() ELSE NULL END)
                RETURNING id, status, granted_at"""),
        {"tenant": tenant_id, "id": str(uuid.uuid4()), "client": client_id,
         "contact": contact_id, "contact_value": contact["contact_value"],
         "channel": channel, "purpose": purpose,
         "event_sequence": event_sequence,
         "source": source, "wording": wording_version, "status": status,
         "proof": proof_reference,
         "expires": expires_at.isoformat() if expires_at else None,
         "review": lawful_basis_review},
    ).mappings().one()
    return dict(row)


def record_suppression(
    connection,
    *,
    tenant_id: str,
    client_id: str | None,
    contact_value: str,
    channel: str,
    reason: str,
    source: str = "client_self",
) -> dict:
    """Record an opt-out, refreshing it when the same value opts out again.

    The unique key is (tenant, normalized contact value, channel), so a
    repeated STOP/unsubscribe refreshes the row instead of duplicating it, and
    the block survives client edits, merges and re-imports. ``client_id`` may
    be None for inbound STOP from a number the salon never stored (TASK-029):
    the value block is what protects future sends.
    """
    if channel not in CHANNELS:
        raise SuppressionModuleError(f"unknown channel {channel!r}")
    if reason not in SUPPRESSION_REASONS:
        raise SuppressionModuleError(f"unknown reason {reason!r}")
    value = normalize_contact_value(contact_value)
    lock_contact_channel(connection, tenant_id=tenant_id,
                         contact_value=value, channel=channel)
    row = connection.execute(
        text("""INSERT INTO suppressions
                (tenant_id, id, client_id, contact_value, channel, reason, source)
                VALUES (:tenant, :id, :client, :value, :channel, :reason, :source)
                ON CONFLICT (tenant_id, contact_value, channel)
                DO UPDATE SET reason=EXCLUDED.reason, source=EXCLUDED.source,
                              suppressed_at=now(), version=suppressions.version+1,
                              updated_at=now()
                RETURNING id, contact_value, channel, reason, suppressed_at"""),
        {"tenant": tenant_id, "id": str(uuid.uuid4()), "client": client_id,
         "value": value, "channel": channel, "reason": reason, "source": source},
    ).mappings().one()
    return dict(row)


def check_suppressed(connection, *, tenant_id: str, contact_value: str, channel: str) -> bool:
    """Return True when this contact value may not be sent on this channel."""
    value = normalize_contact_value(contact_value)
    lock_contact_channel(connection, tenant_id=tenant_id,
                         contact_value=value, channel=channel)
    found = connection.execute(
        text("""SELECT 1 FROM suppressions
                WHERE tenant_id=:tenant AND contact_value=:value AND channel=:channel"""),
        {"tenant": tenant_id, "value": value, "channel": channel},
    ).scalar_one_or_none()
    return found is not None


def check_consented(
    connection, *, tenant_id: str, client_id: str, channel: str, purpose: str,
    contact_id: str | None = None, contact_value: str | None = None,
    now: datetime | None = None,
) -> bool:
    """Return True when a send of this purpose is currently authorized.

    Transactional purposes (anything outside CONSENT_GATED_PURPOSES) need no
    consent row. Gated purposes need the latest marketing consent row for
    this client/channel to be an unexpired grant — a later revocation always
    wins over an earlier grant. The grant itself is always recorded under
    purpose 'marketing'; closure_broadcast rides that same grant.
    """
    if purpose not in CONSENT_GATED_PURPOSES:
        return True
    expected_kind = "email" if channel == "email" else "phone"
    if contact_value is None:
        current_contact = connection.execute(
            text("""SELECT id::text AS id, normalized_value
                    FROM client_contacts
                    WHERE tenant_id=:tenant AND client_id=:client AND kind=:kind
                    ORDER BY is_primary DESC, created_at LIMIT 1"""),
            {"tenant": tenant_id, "client": client_id, "kind": expected_kind},
        ).mappings().one_or_none()
        if current_contact is None:
            return False
        contact_id = contact_id or current_contact["id"]
        contact_value = current_contact["normalized_value"]
    value = normalize_contact_value(contact_value)
    lock_contact_channel(connection, tenant_id=tenant_id,
                         contact_value=value, channel=channel)
    row = connection.execute(
        text("""SELECT c.status, c.expires_at FROM consents c
                JOIN client_contacts cc
                  ON cc.tenant_id=c.tenant_id AND cc.client_id=c.client_id
                 AND cc.id=c.contact_id AND cc.kind=:kind
                WHERE c.tenant_id=:tenant AND c.client_id=:client
                  AND c.channel=:channel AND c.purpose=:purpose
                  AND (CAST(:contact_id AS uuid) IS NULL
                       OR c.contact_id=CAST(:contact_id AS uuid))
                  AND lower(btrim(cc.normalized_value))=:contact_value
                  AND c.contact_value_snapshot=:contact_value
                ORDER BY c.event_sequence DESC LIMIT 1"""),
        {"tenant": tenant_id, "client": client_id, "channel": channel,
         "purpose": MARKETING_PURPOSE, "contact_id": contact_id,
         "contact_value": value, "kind": expected_kind},
    ).mappings().one_or_none()
    if row is None or row["status"] != "granted":
        return False
    if row["expires_at"] is not None:
        moment = now if now is not None else datetime.now(timezone.utc)
        expires = row["expires_at"]
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires <= moment:
            return False
    return True


def issue_email_unsubscribe_url(
    connection, *, tenant_id: str, client_id: str, contact_value: str,
    base_url: str | None = None, now: datetime | None = None,
) -> str:
    """Create a one-year email opt-out capability; persist only its SHA-256 hash."""
    base = (base_url if base_url is not None else os.getenv("WEB_BASE_URL", "")).strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise ConsentModuleError("a public WEB_BASE_URL is required for email delivery")
    value = normalize_contact_value(contact_value)
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    moment = now or datetime.now(timezone.utc)
    connection.execute(text("""DELETE FROM email_unsubscribe_capabilities
        WHERE ctid IN (SELECT ctid FROM email_unsubscribe_capabilities
                       WHERE tenant_id=:tenant AND expires_at<=:now
                       ORDER BY expires_at LIMIT 100)"""),
        {"tenant": tenant_id, "now": moment})
    connection.execute(text("""INSERT INTO email_unsubscribe_capabilities
        (tenant_id, token_hash, client_id, contact_value, channel, expires_at)
        VALUES (:tenant, :hash, :client, :value, 'email', :expires)"""),
        {"tenant": tenant_id, "hash": token_hash, "client": client_id,
         "value": value, "expires": moment + timedelta(days=365)})
    # Keep the bearer secret out of HTTP request/referrer logs; the page reads
    # the fragment and POSTs it once the client chooses to unsubscribe.
    return f"{base}/unsubscribe#token={quote(token, safe='')}"


def register_consent_routes(app: FastAPI) -> None:
    """Register the public, identity-free email unsubscribe capability endpoint."""

    @app.post("/v1/public/communications/unsubscribe", status_code=204, tags=["communications"])
    def public_unsubscribe(payload: PublicUnsubscribeRequest) -> Response:
        token_hash = hashlib.sha256(payload.token.encode("ascii")).hexdigest()
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                applied = connection.execute(
                    text("SELECT public.apply_email_unsubscribe(:token_hash)"),
                    {"token_hash": token_hash},
                ).scalar_one()
            if not applied:
                raise HTTPException(status_code=404,
                                    detail={"code": "unsubscribe_link_invalid_or_expired"})
            return Response(status_code=204, headers={
                "Cache-Control": "no-store",
                "Referrer-Policy": "no-referrer",
            })
        finally:
            engine.dispose()
