"""Durable communication outbox for booking events (FR-039, FR-042; TASK-027).

Three concerns, one module:

- **Record atomically.** `record_booking_communication` writes a `communications`
  row inside the booking transaction. A rolled-back booking never leaves an orphan
  message, and a replayed booking command resolves to the same row through the
  `dedupe_key` unique constraint.

- **Dispatch separately.** `dispatch_due_communications` is the worker step. It
  re-reads the current appointment version and skips any communication whose
  `expected_entity_version` is stale (the appointment was rescheduled or cancelled
  after the message was recorded). It also skips reminders whose `expires_at`
  has passed (FR-042). Provider failure never touches the booking — the adapter
  records or fails on its own.

    - **Schedule reminders.** `schedule_booking_reminders` creates two reminder
    communications per appointment (24 h and 2 h before start) during the same
    transaction that creates or reschedules the booking.

- **Honour consent.** Before each send the dispatcher re-checks suppressions
  (FR-041) and, for ``marketing`` purposes, the current consent state (FR-040).
  A blocked message moves to ``suppressed`` with a reason; the adapter is never
  called for it.

The delivery adapter is injected, so the module works with the existing
`RecordingDeliveryAdapter` today and any real provider adapter later.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from modules.communications.consent import check_consented, check_suppressed

# Reminder offsets in hours before the appointment start (FR-042 default).
REMINDER_OFFSETS_HOURS = (24, 2)


def _dedupe_key(source_id: str, purpose: str, entity_version: int) -> str:
    """Business dedupe key: one message per appointment, purpose and version."""
    return f"appointment:{source_id}:{purpose}:v{entity_version}"


def record_booking_communication(
    connection,
    *,
    tenant_id: str,
    appointment_id: str,
    client_id: str,
    contact_value: str,
    channel: str,
    purpose: str,
    entity_version: int,
    template_id: str | None = None,
    template_version: int | None = None,
    scheduled_at: datetime | None = None,
    expires_at: datetime | None = None,
) -> dict | None:
    """Record one communication row inside the caller's transaction.

    Returns the inserted row (as a dict) or ``None`` when the dedupe key already
    exists — a replayed booking command must not create a second message.
    """
    key = _dedupe_key(appointment_id, purpose, entity_version)
    row = connection.execute(
        text("""INSERT INTO communications
                (tenant_id, id, client_id, contact_value, channel, purpose,
                 template_id, template_version, source_type, source_id,
                 expected_entity_version, state, scheduled_at, expires_at, dedupe_key)
                VALUES (:tenant, :id, :client, :contact, :channel, :purpose,
                        :template, :template_version, 'appointment', :source,
                        :entity_version, 'recorded', :scheduled, :expires, :dedupe)
                ON CONFLICT (tenant_id, dedupe_key) DO NOTHING
                RETURNING id, state, dedupe_key, expected_entity_version"""),
        {"tenant": tenant_id, "id": str(uuid.uuid4()), "client": client_id,
         "contact": contact_value, "channel": channel, "purpose": purpose,
         "template": template_id, "template_version": template_version,
         "source": appointment_id, "entity_version": entity_version,
         "scheduled": scheduled_at, "expires": expires_at, "dedupe": key},
    ).mappings().one_or_none()
    return dict(row) if row else None


def schedule_booking_reminders(
    connection,
    *,
    tenant_id: str,
    appointment_id: str,
    client_id: str,
    contact_value: str,
    channel: str,
    entity_version: int,
    starts_at: datetime,
) -> list[dict]:
    """Create reminder communications for an appointment (FR-042).

    Called inside the booking/reschedule transaction. Each reminder is a separate
    row with its own dedupe key, so rescheduling to a new time creates fresh
    reminders while the old ones become stale and are skipped at dispatch.
    """
    rows: list[dict] = []
    for hours in REMINDER_OFFSETS_HOURS:
        purpose = f"appointment_reminder_{hours}h"
        scheduled = starts_at - timedelta(hours=hours)
        row = record_booking_communication(
            connection,
            tenant_id=tenant_id,
            appointment_id=appointment_id,
            client_id=client_id,
            contact_value=contact_value,
            channel=channel,
            purpose=purpose,
            entity_version=entity_version,
            scheduled_at=scheduled,
            expires_at=starts_at,
        )
        if row:
            rows.append(row)
    return rows


def dispatch_due_communications(
    connection,
    *,
    tenant_id: str,
    adapter,
    now: datetime | None = None,
    limit: int = 50,
) -> dict:
    """Dispatch recorded communications whose scheduled time has arrived.

    For each candidate the current appointment version is re-read. If it no
    longer matches ``expected_entity_version`` the row is marked
    ``skipped_stale``. If ``expires_at`` has passed the row is marked
    ``skipped_expired``. Then suppressions are re-checked (FR-041) and, for
    ``marketing`` purposes, the current consent state (FR-040); a blocked
    message is marked ``suppressed`` and the adapter is never called for it.
    Otherwise the adapter is called and the row advances to ``dispatched``
    (or ``failed`` on a provider error).

    Returns a summary dict: ``{"dispatched": n, "skipped_stale": n,
    "skipped_expired": n, "suppressed": n, "failed": n}``.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    candidates = connection.execute(
        text("""SELECT id, client_id, contact_value, channel, purpose,
                       template_id, template_version, source_id,
                       expected_entity_version, expires_at
                FROM communications
                WHERE tenant_id=:tenant AND state='recorded'
                  AND (scheduled_at IS NULL OR scheduled_at <= :now)
                ORDER BY scheduled_at NULLS FIRST
                LIMIT :limit"""),
        {"tenant": tenant_id, "now": now.isoformat(), "limit": limit},
    ).mappings().all()

    summary = {"dispatched": 0, "skipped_stale": 0, "skipped_expired": 0,
               "suppressed": 0, "failed": 0}
    for comm in candidates:
        # Re-read the current appointment version.
        current = connection.execute(
            text("""SELECT version FROM appointments
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": comm["source_id"]},
        ).scalar_one_or_none()

        if current is None or current != comm["expected_entity_version"]:
            connection.execute(
                text("""UPDATE communications
                        SET state='skipped_stale',
                            skip_reason=:reason, version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm["id"],
                 "reason": f"appointment version changed (expected {comm['expected_entity_version']}, current {current})"})
            summary["skipped_stale"] += 1
            continue

        if comm["expires_at"] is not None and comm["expires_at"] <= now:
            connection.execute(
                text("""UPDATE communications
                        SET state='skipped_expired',
                            skip_reason=:reason, version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm["id"],
                 "reason": "reminder window passed before dispatch"})
            summary["skipped_expired"] += 1
            continue

        # AC-2 / FR-041: an opt-out written after the message was recorded —
        # the unsubscribe/STOP race — blocks this send. Suppression is keyed by
        # contact value, so it holds even if the client row changed since.
        if check_suppressed(connection, tenant_id=tenant_id,
                            contact_value=comm["contact_value"],
                            channel=comm["channel"]):
            connection.execute(
                text("""UPDATE communications
                        SET state='suppressed',
                            skip_reason=:reason, version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm["id"],
                 "reason": "contact opted out for this channel before dispatch"})
            summary["suppressed"] += 1
            continue

        # AC-1 / FR-040: marketing sends need a current grant; a later
        # revocation always wins over an earlier grant.
        if comm["purpose"] == "marketing" and not check_consented(
                connection, tenant_id=tenant_id, client_id=comm["client_id"],
                channel=comm["channel"], purpose=comm["purpose"], now=now):
            connection.execute(
                text("""UPDATE communications
                        SET state='suppressed',
                            skip_reason=:reason, version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm["id"],
                 "reason": "no current marketing consent for this client/channel"})
            summary["suppressed"] += 1
            continue

        # Dispatch through the injected adapter.
        try:
            receipt = adapter.send(
                channel=comm["channel"],
                recipient=comm["contact_value"],
                template=comm["purpose"],
            )
            connection.execute(
                text("""UPDATE communications
                        SET state='dispatched', dispatched_at=now(),
                            provider_ref=:ref, version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm["id"],
                 "ref": receipt.get("id") if isinstance(receipt, dict) else None})
            summary["dispatched"] += 1
        except Exception as exc:  # noqa: BLE001 — adapter failure is classified, not fatal
            connection.execute(
                text("""UPDATE communications
                        SET state='failed', skip_reason=:reason,
                            version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm["id"],
                 "reason": str(exc)[:240]})
            summary["failed"] += 1

    return summary