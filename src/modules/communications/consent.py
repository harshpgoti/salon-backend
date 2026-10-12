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
from datetime import datetime, timezone

from sqlalchemy import text

__all__ = [
    "check_consented",
    "check_suppressed",
    "normalize_contact_value",
    "record_consent",
    "record_suppression",
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


def normalize_contact_value(value: str) -> str:
    """Display-level normalization for suppression keys.

    Lower-cases and strips surrounding whitespace. Phone digits are unaffected
    by lower-casing; canonical CRM normalization owns anything stricter. The
    same function is applied on record and on check, so both sides agree.
    """
    return value.strip().lower()


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
    row = connection.execute(
        text("""INSERT INTO consents
                (tenant_id, id, client_id, contact_id, channel, purpose,
                 source, wording_version, status, proof_reference,
                 expires_at, lawful_basis_review)
                VALUES (:tenant, :id, :client, :contact, :channel, :purpose,
                        :source, :wording, :status, :proof, :expires, :review)
                RETURNING id, status, granted_at"""),
        {"tenant": tenant_id, "id": str(uuid.uuid4()), "client": client_id,
         "contact": contact_id, "channel": channel, "purpose": purpose,
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
    found = connection.execute(
        text("""SELECT 1 FROM suppressions
                WHERE tenant_id=:tenant AND contact_value=:value AND channel=:channel"""),
        {"tenant": tenant_id, "value": value, "channel": channel},
    ).scalar_one_or_none()
    return found is not None


def check_consented(
    connection, *, tenant_id: str, client_id: str, channel: str, purpose: str,
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
    row = connection.execute(
        text("""SELECT status, expires_at FROM consents
                WHERE tenant_id=:tenant AND client_id=:client
                  AND channel=:channel AND purpose=:purpose
                ORDER BY granted_at DESC, ctid DESC LIMIT 1"""),
        {"tenant": tenant_id, "client": client_id,
         "channel": channel, "purpose": MARKETING_PURPOSE},
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
