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

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from modules.communications.consent import (
    CONSENT_GATED_PURPOSES,
    check_consented,
    check_suppressed,
    issue_email_unsubscribe_url,
)
from modules.communications.senders import get_verified_sender, record_message_cost

# Reminder offsets in hours before the appointment start (FR-042 default).
REMINDER_OFFSETS_HOURS = (24, 2)


class KnownDeliveryFailure(Exception):
    """A provider rejection known not to have accepted the message.

    Adapters must raise this only for a definitive terminal response. Network
    errors, timeouts and unclassified failures remain ambiguous and retryable.
    """


def _dedupe_key(source_id: str, purpose: str, entity_version: int, channel: str,
                suffix: str = "", client_id: str | None = None) -> str:
    """Business dedupe key: one message per source, purpose, version, channel and client."""
    key = f"appointment:{source_id}:{purpose}:v{entity_version}:{channel}"
    if client_id is not None:
        key = f"{key}:{client_id}"
    return f"{key}:{suffix}" if suffix else key


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
    trace_id: str | None = None,
    source_type: str = "appointment",
    dedupe_suffix: str = "",
    reply_body: str | None = None,
    template_subject_snapshot: str | None = None,
    template_body_snapshot: str | None = None,
) -> dict | None:
    """Record one communication row inside the caller's transaction.

    Returns the inserted row (as a dict) or ``None`` when the dedupe key already
    exists — a replayed booking command must not create a second message.
    """
    if source_type not in ("appointment", "location_broadcast", "rebooking_cycle",
                           "campaign", "campaign_test"):
        raise ValueError(f"unknown source_type {source_type!r}")
    if reply_body is not None and source_type != "rebooking_cycle":
        raise ValueError("custom message bodies are restricted to rebooking prompts")
    if ((template_subject_snapshot is not None or template_body_snapshot is not None)
            and source_type not in ("campaign", "campaign_test")):
        raise ValueError("template content snapshots are restricted to campaigns")
    key = _dedupe_key(appointment_id, purpose, entity_version, channel, dedupe_suffix,
                      client_id)
    row = connection.execute(
        text("""INSERT INTO communications
                (tenant_id, id, client_id, contact_value, channel, purpose,
                 template_id, template_version, source_type, source_id,
                 expected_entity_version, state, scheduled_at, expires_at, dedupe_key,
                 trace_id, reply_body, template_subject_snapshot, template_body_snapshot)
                VALUES (:tenant, :id, :client, :contact, :channel, :purpose,
                        :template, :template_version, :source_type, :source,
                        :entity_version, 'recorded', :scheduled, :expires, :dedupe,
                        :trace, :reply_body, :template_subject_snapshot,
                        :template_body_snapshot)
                ON CONFLICT (tenant_id, dedupe_key) DO NOTHING
                RETURNING id, state, dedupe_key, expected_entity_version"""),
        {"tenant": tenant_id, "id": str(uuid.uuid4()), "client": client_id,
         "contact": contact_value, "channel": channel, "purpose": purpose,
         "template": template_id, "template_version": template_version,
         "source_type": source_type, "source": appointment_id,
         "entity_version": entity_version,
         "scheduled": scheduled_at, "expires": expires_at, "dedupe": key,
         "trace": trace_id, "reply_body": reply_body,
         "template_subject_snapshot": template_subject_snapshot,
         "template_body_snapshot": template_body_snapshot},
    ).mappings().one_or_none()
    if row is not None and trace_id is not None:
        from modules.trace import record_trace_event
        record_trace_event(connection, tenant_id=tenant_id, trace_id=trace_id,
                           span="outbox_record",
                           detail={"communication_id": row["id"], "purpose": purpose,
                                   "channel": channel, "source": "appointment",
                                   "source_id": appointment_id})
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
    trace_id: str | None = None,
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
            trace_id=trace_id,
        )
        if row:
            rows.append(row)
    return rows


def record_appointment_email_update(connection, *, tenant_id: str,
                                    appointment_id: str,
                                    purpose: str,
                                    trace_id: str | None = None) -> dict | None:
    """Record an appointment email and its reminders inside the booking transaction.

    The contact and current entity version are snapshotted from the appointment's own
    tenant-scoped rows. Appointments without an email simply have no email outbox work.
    """
    if purpose not in {"booking_confirmation", "booking_rescheduled", "booking_cancelled"}:
        raise ValueError(f"unsupported appointment email purpose {purpose!r}")
    appointment = connection.execute(
        text("""SELECT a.client_id::text AS client_id, a.version, a.starts_at,
                       c.normalized_value AS email
                FROM appointments a
                LEFT JOIN LATERAL (
                    SELECT normalized_value FROM client_contacts
                    WHERE tenant_id=a.tenant_id AND client_id=a.client_id AND kind='email'
                    ORDER BY is_primary DESC, created_at, id LIMIT 1
                ) c ON true
                WHERE a.tenant_id=:tenant AND a.id=:appointment"""),
        {"tenant": tenant_id, "appointment": appointment_id}).mappings().one_or_none()
    if appointment is None or not appointment["email"]:
        return None
    result = {"confirmation": record_booking_communication(
        connection, tenant_id=tenant_id, appointment_id=appointment_id,
        client_id=appointment["client_id"], contact_value=appointment["email"],
        channel="email", purpose=purpose, entity_version=appointment["version"],
        trace_id=trace_id),
        "reminders": []}
    if purpose in {"booking_confirmation", "booking_rescheduled"}:
        result["reminders"] = schedule_booking_reminders(
            connection, tenant_id=tenant_id, appointment_id=appointment_id,
            client_id=appointment["client_id"], contact_value=appointment["email"],
            channel="email", entity_version=appointment["version"],
            starts_at=appointment["starts_at"], trace_id=trace_id)
    return result


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
    (or follows retry handling on an unknown provider outcome). Only an adapter
    ``KnownDeliveryFailure`` may trigger an alternate-channel fallback.

    Returns a summary dict: ``{"dispatched": n, "skipped_stale": n,
    "skipped_expired": n, "suppressed": n, "failed": n}``.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    # Worker-safe claim first: concurrent dispatchers split the due rows via
    # SKIP LOCKED, so no message is ever picked up twice — not on first send,
    # not on retry. Gates below may still reroute a claimed row.
    candidates = connection.execute(
        text("""UPDATE communications SET state='sending',
                       version=version+1, updated_at=now()
                WHERE id IN (
                    SELECT id FROM communications
                    WHERE tenant_id=:tenant AND state='recorded'
                      AND (scheduled_at IS NULL OR scheduled_at <= :now)
                    ORDER BY scheduled_at NULLS FIRST
                    LIMIT :limit
                    FOR UPDATE SKIP LOCKED)
                RETURNING id, client_id, contact_value, channel, purpose,
                          template_id, template_version, source_type, source_id,
                          expected_entity_version, fallback_of, expires_at, reply_body,
                          template_subject_snapshot, template_body_snapshot,
                          trace_id::text AS trace_id"""),
        {"tenant": tenant_id, "now": now.isoformat(), "limit": limit},
    ).mappings().all()

    summary = {"dispatched": 0, "skipped_stale": 0, "skipped_expired": 0,
               "suppressed": 0, "retry_scheduled": 0, "failed": 0,
               "deferred": 0, "fallback_created": 0}
    for candidate in candidates:
        comm = dict(candidate)
        try:
            _route_candidate(connection, tenant_id=tenant_id, comm=comm,
                             adapter=adapter, summary=summary, now=now)
        except KnownDeliveryFailure:
            _record_known_delivery_failure(connection, tenant_id=tenant_id,
                                           comm=comm, summary=summary, now=now)
        except Exception as exc:  # noqa: BLE001 — unknown outcome stays retryable
            # TASK-030 / FR-043: a provider raise schedules a backoff retry
            # instead of failing terminally; the booking is never touched.
            from modules.communications.callbacks import queue_retry
            outcome = queue_retry(connection, tenant_id=tenant_id,
                                  communication_id=comm["id"],
                                  error=str(exc), now=now)
            if outcome["status"] == "retry_scheduled":
                summary["retry_scheduled"] += 1
            else:
                summary["failed"] += 1

    return summary


def _sms_template_body_chars(connection, *, tenant_id: str, comm) -> int | None:
    """Measure the tenant-owned template snapshot without exposing its content."""
    if comm["channel"] != "sms":
        return None
    body = comm.get("template_body_snapshot")
    if comm.get("template_id") is not None:
        body = connection.execute(
            text("""SELECT body FROM message_templates
                    WHERE tenant_id=:tenant AND id=:id AND version=:version"""),
            {"tenant": tenant_id, "id": comm["template_id"],
             "version": comm.get("template_version")},
        ).scalar_one_or_none()
    if body is None and comm.get("source_type") == "campaign":
        body = connection.execute(
            text("""SELECT snapshot_template_body FROM offer_campaigns
                    WHERE tenant_id=:tenant AND id=:campaign"""),
            {"tenant": tenant_id, "campaign": comm["source_id"]},
        ).scalar_one_or_none()
    if comm.get("reply_body") is not None:
        body = comm["reply_body"]
    return len(body) if body else None


def _campaign_send_outcome(connection, *, tenant_id: str, comm, state: str) -> None:
    """Keep the campaign ledger aligned with the dispatch outcome."""
    if comm.get("source_type") != "campaign":
        return
    connection.execute(
        text("""UPDATE campaign_sends SET state=:state, updated_at=now()
                WHERE tenant_id=:tenant AND communication_id=:communication
                  AND state='recorded'"""),
        {"tenant": tenant_id, "communication": comm["id"], "state": state})


def _maybe_fallback(connection, *, tenant_id: str, comm, summary: dict,
                    now: datetime) -> None:
    """Record a fallback after an adapter definitively rejects a send (TASK-032)."""
    from modules.quiet import try_fallback_on_exhaustion
    created = try_fallback_on_exhaustion(connection, tenant_id=tenant_id,
                                         comm=comm, now=now)
    if created is not None:
        summary["fallback_created"] += 1


def _record_known_delivery_failure(connection, *, tenant_id: str, comm: dict,
                                   summary: dict, now: datetime) -> None:
    """Persist a definitive provider rejection, then consider eligible fallback."""
    connection.execute(
        text("""UPDATE communications SET state='failed',
                       skip_reason='provider definitively rejected the message',
                       version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id"""),
        {"tenant": tenant_id, "id": comm["id"]})
    summary["failed"] += 1
    _maybe_fallback(connection, tenant_id=tenant_id, comm=comm,
                    summary=summary, now=now)


def _route_candidate(connection, *, tenant_id: str, comm, adapter, summary: dict,
                     now: datetime) -> None:
    """Apply gates and send one candidate; adapter errors propagate.

    Shared by the booking-time dispatch and the retry worker so a retried send
    re-checks versions, expiry, suppressions, consent and the SMS sender —
    exactly like a first attempt. Gated outcomes update state and the summary;
    only a successful adapter call returns normally.
    """
    from modules.quiet import (defer_past_quiet, in_quiet_hours,
                               load_messaging_settings, _location_now)
    settings = load_messaging_settings(connection, tenant_id=tenant_id)
    # AC-2 / FR-047: the emergency stop defers everything, visibly. Flipping
    # it off resumes the queue; nothing is lost, nothing sneaks through.
    if settings["emergency_stop"]:
        connection.execute(
            text("""UPDATE communications SET state='recorded',
                           version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": comm["id"]})
        summary["deferred"] += 1
        return
    source = comm.get("source_type", "appointment")
    if source == "campaign":
        campaign_status = connection.execute(
            text("""SELECT status FROM offer_campaigns
                    WHERE tenant_id=:tenant AND id=:id FOR SHARE"""),
            {"tenant": tenant_id, "id": comm["source_id"]}).scalar_one_or_none()
        if campaign_status == "paused":
            connection.execute(
                text("""UPDATE communications SET state='recorded',
                               version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm["id"]})
            summary["deferred"] += 1
            return
        # Older independently queued marketing communications can carry a
        # campaign provenance ID without a campaign row. Enforce lifecycle
        # authorization only when this source resolves to an actual campaign.
        if campaign_status is not None and campaign_status not in (
                "sending", "completed", "partial"):
            connection.execute(
                text("""UPDATE communications SET state='skipped_stale',
                               skip_reason='campaign is not authorized to send',
                               version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm["id"]})
            connection.execute(
                text("""UPDATE campaign_sends SET state='skipped_cancelled',
                               updated_at=now()
                        WHERE tenant_id=:tenant AND communication_id=:id
                          AND state='recorded'"""),
                {"tenant": tenant_id, "id": comm["id"]})
            summary["skipped_stale"] += 1
            return
    # Re-read the current source version — appointments and cycles each
    # version their own rows. Broadcasts carry no version; their gates are
    # consent, suppression, quiet hours and budget, checked below like sends.
    current = None
    if source == "appointment":
        current = connection.execute(
            text("""SELECT version FROM appointments
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": comm["source_id"]},
        ).scalar_one_or_none()
    elif source == "rebooking_cycle":
        current = connection.execute(
            text("""SELECT c.version, c.status,
                           (c.snoozed_until IS NOT NULL AND
                            c.snoozed_until > (:now AT TIME ZONE source.local_timezone)::date)
                               AS snoozed_active,
                           EXISTS (
                               SELECT 1 FROM appointments upcoming
                               JOIN appointment_services line
                                 ON line.tenant_id=upcoming.tenant_id
                                AND line.appointment_id=upcoming.id
                               WHERE upcoming.tenant_id=c.tenant_id
                                 AND upcoming.client_id=c.client_id
                                 AND line.service_id=c.service_id
                                 AND upcoming.starts_at > :now
                                 AND upcoming.status IN ('pending_approval', 'confirmed',
                                                         'checked_in', 'in_service')
                           ) AS has_future_booking
                    FROM rebooking_cycles c
                    JOIN appointments source ON source.tenant_id=c.tenant_id
                                            AND source.id=c.source_appointment_id
                    WHERE c.tenant_id=:tenant AND c.id=:id"""),
            {"tenant": tenant_id, "id": comm["source_id"], "now": now},
        ).mappings().one_or_none()
    elif source == "inbox_task":
        current = connection.execute(
            text("""SELECT version, status FROM follow_up_tasks
                    WHERE tenant_id=:tenant AND id=:id FOR UPDATE"""),
            {"tenant": tenant_id, "id": comm["source_id"]},
        ).mappings().one_or_none()

    if (source == "appointment"
            and (current is None or current != comm["expected_entity_version"])):
        connection.execute(
            text("""UPDATE communications
                    SET state='skipped_stale',
                        skip_reason=:reason, version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": comm["id"],
             "reason": f"appointment version changed (expected {comm['expected_entity_version']}, current {current})"})
        summary["skipped_stale"] += 1
        return

    if source == "rebooking_cycle" and (
            current is None or current["version"] != comm["expected_entity_version"]
            or current["status"] != "due" or current["snoozed_active"]
            or current["has_future_booking"]):
        connection.execute(
            text("""UPDATE communications
                    SET state='skipped_stale',
                        skip_reason=:reason, version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": comm["id"],
             "reason": "rebooking cycle moved on before dispatch"})
        summary["skipped_stale"] += 1
        return

    if source == "inbox_task" and (
            current is None or current["version"] != comm["expected_entity_version"]
            or current["status"] != "open"):
        connection.execute(
            text("""UPDATE communications SET state='skipped_stale',
                    skip_reason='inbox task closed or changed before dispatch',
                    version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": comm["id"]})
        summary["skipped_stale"] += 1
        return

    if comm["expires_at"] is not None and comm["expires_at"] <= now:
        connection.execute(
            text("""UPDATE communications
                    SET state='skipped_expired',
                        skip_reason=:reason, version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": comm["id"],
             "reason": "reminder window passed before dispatch"})
        summary["skipped_expired"] += 1
        return

    # A queued send honors the client's latest preference, not the channel
    # captured when another workflow first wrote the outbox row. Closure
    # broadcasts are the exception: their caller explicitly selected the
    # audience channel and contact after applying the broadcast policy, so a
    # later preference lookup must not rewrite that target. Fallback rows
    # carry their parent explicitly and keep the alternate channel selected.
    if (comm.get("fallback_of") is None and comm.get("client_id") is not None
            and comm["purpose"] != "closure_broadcast"):
        from modules.quiet import resolve_primary_channel
        preference = resolve_primary_channel(
            connection, tenant_id=tenant_id, client_id=comm["client_id"],
            purpose=comm["purpose"])
        if preference["configured"]:
            channel = preference["primary_channel"]
            kind = "email" if channel == "email" else "phone"
            contact = connection.execute(
                text("""SELECT normalized_value FROM client_contacts
                        WHERE tenant_id=:tenant AND client_id=:client AND kind=:kind
                        ORDER BY is_primary DESC, created_at LIMIT 1"""),
                {"tenant": tenant_id, "client": comm["client_id"], "kind": kind},
            ).scalar_one_or_none()
            if contact is None:
                connection.execute(
                    text("""UPDATE communications SET state='failed',
                            skip_reason='preferred channel has no matching client contact',
                            version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": tenant_id, "id": comm["id"]})
                summary["failed"] += 1
                return
            connection.execute(
                text("""UPDATE communications SET channel=:channel, contact_value=:contact,
                        version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm["id"], "channel": channel,
                 "contact": contact})
            comm["channel"] = channel
            comm["contact_value"] = contact

    # AC-1 / FR-047: quiet hours defer the send to the window end — unless the
    # deferral itself passes usefulness, in which case it expires now.
    local_now, _ = _location_now(connection, tenant_id=tenant_id, now=now)
    if in_quiet_hours(settings["quiet_start"], settings["quiet_end"], local_now):
        deferred_to = defer_past_quiet(connection, tenant_id=tenant_id,
                                       communication_id=comm["id"], now=now)
        expires = comm["expires_at"]
        if expires is not None:
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if deferred_to > expires:
                connection.execute(
                    text("""UPDATE communications
                            SET state='skipped_expired',
                                skip_reason=:reason, version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": tenant_id, "id": comm["id"],
                     "reason": "quiet deferral passed the reminder window"})
                summary["skipped_expired"] += 1
                return
        connection.execute(
            text("""UPDATE communications SET state='recorded',
                           version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": comm["id"]})
        summary["deferred"] += 1
        return

    # AC-1 / FR-047: per-recipient daily cap throttles by deferring a day.
    cap = settings["max_per_recipient_per_day"]
    if cap is not None:
        day_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = connection.execute(
            text("""SELECT count(*) FROM communications
                    WHERE tenant_id=:tenant AND client_id=:client
                      AND channel=:channel AND state IN ('dispatched', 'delivered', 'read')
                      AND dispatched_at >= :day_start"""),
            {"tenant": tenant_id, "client": comm["client_id"],
             "channel": comm["channel"], "day_start": day_start.isoformat()},
        ).scalar_one()
        if sent_today >= cap:
            connection.execute(
                text("""UPDATE communications
                        SET scheduled_at=:at, state='recorded',
                            version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm["id"],
                 "at": (local_now + timedelta(days=1)).isoformat()})
            summary["deferred"] += 1
            return

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
        _campaign_send_outcome(connection, tenant_id=tenant_id, comm=comm,
                               state="skipped_suppressed")
        return

    # AC-1 / FR-040: gated purposes (marketing, closure broadcasts) need a
    # current grant; a later revocation always wins over an earlier grant.
    if comm["purpose"] in CONSENT_GATED_PURPOSES:
        marketing_allowed = connection.execute(
            text("""SELECT marketing_allowed FROM tenant_entitlements
                    WHERE tenant_id=:tenant"""),
            {"tenant": tenant_id}).scalar_one_or_none()
        if marketing_allowed is not True or not check_consented(
                connection, tenant_id=tenant_id, client_id=comm["client_id"],
                contact_value=comm["contact_value"], channel=comm["channel"],
                purpose=comm["purpose"], now=now):
            connection.execute(
                text("""UPDATE communications
                        SET state='suppressed',
                            skip_reason=:reason, version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm["id"],
                 "reason": ("marketing entitlement is inactive"
                            if marketing_allowed is not True else
                            "no current marketing consent for this client/channel")})
            summary["suppressed"] += 1
            _campaign_send_outcome(connection, tenant_id=tenant_id, comm=comm,
                                   state="skipped_consent")
            return

    # AC-1 / TASK-029: SMS routes only through the tenant's verified sender,
    # stamped on the send so the client always sees their own salon's
    # identity. Without one the message fails (retryable once a sender is
    # verified) rather than borrowing another salon's number.
    sender_identity = None
    if comm["channel"] == "sms":
        sender = get_verified_sender(connection, tenant_id=tenant_id)
        if sender is None:
            connection.execute(
                text("""UPDATE communications
                        SET state='failed',
                            skip_reason=:reason, version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm["id"],
                 "reason": "no verified SMS sender for this salon"})
            summary["failed"] += 1
            return
        sender_identity = sender["sender_identity"]

    # AC-1/AC-2 / TASK-031: WhatsApp needs an approved sender with acceptable
    # quality, plus an approved template outside the open 24h window. A
    # restricted sender or template fails visibly (staff follow-up) instead of
    # sending; the row itself is the unsent task.
    if comm["channel"] == "whatsapp":
        from modules.communications.whatsapp import check_wa_eligible
        decision = check_wa_eligible(connection, tenant_id=tenant_id,
                                     contact_value=comm["contact_value"],
                                     template_id=comm["template_id"],
                                     purpose=comm["purpose"],
                                     expected_template_version=(
                                         comm.get("template_version")
                                         if source in ("campaign", "campaign_test") else None),
                                     now=now)
        if not decision["ok"]:
            connection.execute(
                text("""UPDATE communications
                        SET state='failed',
                            skip_reason=:reason, version=version+1, updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": comm["id"],
                 "reason": decision["reason"][:240]})
            summary["failed"] += 1
            _campaign_send_outcome(connection, tenant_id=tenant_id, comm=comm,
                                   state="skipped_suppressed")
            return
        sender_identity = decision["sender"]["phone_number"]
    else:
        decision = None

    # AC-1 / TASK-049: hold the estimated cost before sending. The budget row
    # locks first, so concurrent dispatchers serialize per tenant and the
    # ceiling holds exactly. Over-ceiling sends fail visibly (retryable after
    # the ceiling moves) instead of spending unapproved money.
    from modules.budgets import (BudgetModuleError, reconcile_reservation,
                                 reserve_usage, void_reservation)
    try:
        reserve_usage(connection, tenant_id=tenant_id,
                      communication_id=comm["id"], channel=comm["channel"], now=now)
    except BudgetModuleError as exc:
        connection.execute(
            text("""UPDATE communications
                    SET state='failed',
                        skip_reason=:reason, version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": comm["id"],
             "reason": f"budget_exceeded: {exc}"[:240]})
        summary["failed"] += 1
        _campaign_send_outcome(connection, tenant_id=tenant_id, comm=comm,
                               state="skipped_budget")
        return

    # Dispatch through the injected adapter. Errors propagate: the caller
    # (booking dispatch or retry worker) schedules the backoff retry.
    unsubscribe_url = None
    if comm["channel"] == "email":
        unsubscribe_url = issue_email_unsubscribe_url(
            connection, tenant_id=tenant_id, client_id=comm["client_id"],
            contact_value=comm["contact_value"])
    try:
        send_template = comm["purpose"]
        template_metadata = {}
        content_metadata = {}
        if comm["channel"] == "whatsapp" and decision.get("basis") == "approved_template":
            send_template = decision["template"]["name"]
            template_metadata = {
                "template_category": decision["template"]["category"],
                "template_locale": decision["template"]["locale"],
            }
        elif source in ("campaign", "campaign_test"):
            send_template = "campaign_offer"
            if comm.get("template_body_snapshot") is not None:
                content_metadata["message_body"] = comm["template_body_snapshot"]
            if (comm["channel"] == "email"
                    and comm.get("template_subject_snapshot") is not None):
                content_metadata["message_subject"] = comm["template_subject_snapshot"]
        receipt = adapter.send(
            channel=comm["channel"],
            recipient=comm["contact_value"],
            template=send_template,
            sender=sender_identity,
            unsubscribe_url=unsubscribe_url,
            # Stable across attempts/restarts. Real provider adapters must
            # map this key to their idempotency or status-reconciliation API.
            idempotency_key=hashlib.sha256(
                f"salon:{tenant_id}:{comm['id']}".encode("utf-8")).hexdigest(),
            **({"message_body": comm["reply_body"]}
               if comm.get("reply_body") is not None else content_metadata),
            **template_metadata,
        )
    except Exception:
        void_reservation(connection, tenant_id=tenant_id,
                         communication_id=comm["id"])
        raise
    connection.execute(
        text("""UPDATE communications
                SET state='dispatched', dispatched_at=now(),
                    provider_ref=:ref, version=version+1, updated_at=now()
                WHERE tenant_id=:tenant AND id=:id"""),
        {"tenant": tenant_id, "id": comm["id"],
         "ref": receipt.get("id") if isinstance(receipt, dict) else None})
    # TASK-029 / FR-044: every dispatch writes its CAD segment-cost
    # ledger row, so spend is visible before the next campaign.
    record_message_cost(connection, tenant_id=tenant_id,
                        communication_id=comm["id"],
                        channel=comm["channel"],
                        body_chars=_sms_template_body_chars(
                            connection, tenant_id=tenant_id, comm=comm))
    _campaign_send_outcome(connection, tenant_id=tenant_id, comm=comm, state="sent")
    # TASK-049: settle the held estimate to the ledgered actual.
    reconcile_reservation(connection, tenant_id=tenant_id,
                          communication_id=comm["id"])
    # TASK-053 / FR-077: the worker hop joins the request's trace.
    if comm.get("trace_id"):
        from modules.trace import record_trace_event
        record_trace_event(connection, tenant_id=tenant_id, trace_id=comm["trace_id"],
                           span="worker_send",
                           detail={"communication_id": comm["id"],
                                   "channel": comm["channel"], "purpose": comm["purpose"]})
    summary["dispatched"] += 1


def dispatch_claimed_retries(connection, *, tenant_id: str, adapter,
                             now: datetime | None = None, limit: int = 25) -> dict:
    """Worker step: claim due retries and send each through the shared gates.

    One function so a worker process (or a test) performs claim, re-gated send
    and bookkeeping atomically per batch: claimed attempts that send become
    ``succeeded``/``dispatched``; adapter raises mark the attempt and schedule
    the next backoff via :func:`callbacks.queue_retry`; gated outcomes (stale,
    expired, suppressed) resolve inside ``_route_candidate`` and mark the
    attempt ``succeeded`` — the message reached a terminal state, so no further
    attempt must fire.
    """
    from modules.communications.callbacks import claim_due_retries, queue_retry
    if now is None:
        now = datetime.now(timezone.utc)
    summary = {"dispatched": 0, "skipped_stale": 0, "skipped_expired": 0,
               "suppressed": 0, "retry_scheduled": 0, "failed": 0,
               "deferred": 0, "fallback_created": 0, "reconciled": 0}
    claimed = claim_due_retries(connection, tenant_id=tenant_id, now=now, limit=limit)
    for job in claimed:
        comm = connection.execute(
            text("""SELECT id, client_id, contact_value, channel, purpose,
                           template_id, template_version, source_type, source_id, state,
                           expected_entity_version, fallback_of, expires_at, reply_body,
                           template_subject_snapshot, template_body_snapshot,
                           trace_id::text AS trace_id
                    FROM communications
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": job["communication_id"]},
        ).mappings().one_or_none()
        if comm is None:
            continue
        comm = dict(comm)
        if comm["state"] != "sending":
            # A callback may have reconciled the message after this attempt was
            # claimed by another transaction. Never route known terminal state.
            connection.execute(
                text("""UPDATE dispatch_attempts SET state='superseded', updated_at=now()
                        WHERE tenant_id=:tenant AND id=:id"""),
                {"tenant": tenant_id, "id": job["id"]})
            summary["reconciled"] += 1
            continue
        try:
            _route_candidate(connection, tenant_id=tenant_id, comm=comm,
                             adapter=adapter, summary=summary, now=now)
            outcome = "succeeded"
        except KnownDeliveryFailure:
            _record_known_delivery_failure(connection, tenant_id=tenant_id,
                                           comm=comm, summary=summary, now=now)
            outcome = "exhausted"
        except Exception as exc:  # noqa: BLE001 — unknown outcome stays retryable
            outcome = "retry_scheduled"
            queued = queue_retry(connection, tenant_id=tenant_id,
                                 communication_id=comm["id"],
                                 error=str(exc), now=now)
            if queued["status"] == "retry_scheduled":
                summary["retry_scheduled"] += 1
            else:
                summary["failed"] += 1
                outcome = "exhausted"
        connection.execute(
            text("""UPDATE dispatch_attempts SET state=:state, updated_at=now()
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": tenant_id, "id": job["id"], "state": outcome})
    return summary
