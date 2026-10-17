"""Polling worker for the tenant-scoped communication outbox.

The local/default adapter only records send intent. No provider SDK or credential is
loaded by this process; production provider activation remains a separate release gate.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from modules.communications.outbox import dispatch_due_communications
from salon_platform.tenant import set_tenant_context

POLL_INTERVAL_SECONDS = 5
BATCH_SIZE = 50
SUMMARY_KEYS = ("dispatched", "skipped_stale", "skipped_expired", "suppressed",
                "retry_scheduled", "failed", "deferred", "fallback_created",
                "reconciled", "prompted", "follow_ups", "snoozed_skip",
                "future_booking_skip", "consent_skip", "send_time_skip",
                "booking_link_skip", "campaigns_started", "campaign_queued",
                "campaigns_completed", "campaigns_partial")
logger = logging.getLogger("salon.communication_worker")


class RecordingDeliveryAdapter:
    """Synthetic adapter that records a stable provider reference without sending."""

    def __init__(self) -> None:
        self._sequence = 0
        self._receipts: dict[str, dict[str, str]] = {}

    def send(self, *, channel, recipient, template, sender=None,
             unsubscribe_url=None, idempotency_key=None, template_category=None,
             template_locale=None, message_body=None, message_subject=None) -> dict[str, str]:
        # Deliberately discard delivery content and capability URLs; production
        # adapters are responsible for adding RFC-compatible unsubscribe headers.
        if idempotency_key and idempotency_key in self._receipts:
            return self._receipts[idempotency_key]
        self._sequence += 1
        receipt = {"status": "recorded", "id": f"worker-recorded-{self._sequence}"}
        if idempotency_key:
            self._receipts[idempotency_key] = receipt
        return receipt


def dispatch_tenant_once(engine: Engine, tenant_id: str, *, adapter=None,
                         now: datetime | None = None, limit: int = BATCH_SIZE) -> dict[str, int]:
    """Dispatch one tenant's due work in an RLS-scoped transaction."""
    adapter = adapter or RecordingDeliveryAdapter()
    now = now or datetime.now(timezone.utc)
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        from modules.campaigns import run_campaigns
        from modules.retention import run_rebooking_prompts
        campaigns = run_campaigns(connection, tenant_id=tenant_id, now=now, limit=limit)
        prompts = run_rebooking_prompts(connection, tenant_id=tenant_id, now=now)
        dispatched = dispatch_due_communications(connection, tenant_id=tenant_id,
                                                 adapter=adapter, now=now, limit=limit)
        return {**dispatched,
                "campaigns_started": campaigns.get("campaigns_started", 0),
                "campaign_queued": campaigns.get("sent", 0),
                "campaigns_completed": campaigns.get("completed", 0),
                "campaigns_partial": campaigns.get("partial", 0),
                **{key: prompts.get(key, 0) for key in SUMMARY_KEYS
                   if key in prompts}}


def dispatch_once(engine: Engine, *, adapter=None, now: datetime | None = None,
                  tenant_limit: int = BATCH_SIZE, batch_size: int = BATCH_SIZE) -> dict[str, int]:
    """Discover due tenant IDs, then process each one's content under forced RLS."""
    adapter = adapter or RecordingDeliveryAdapter()
    now = now or datetime.now(timezone.utc)
    with engine.begin() as connection:
        tenant_ids = connection.execute(text("""SELECT tenant_id::text FROM (
                SELECT tenant_id FROM due_communication_tenants(:now, :limit)
                UNION
                SELECT tenant_id FROM due_rebooking_tenants(:now, :limit)
                UNION
                SELECT tenant_id FROM due_campaign_tenants(:now, :limit)
            ) AS due ORDER BY tenant_id"""),
            {"now": now, "limit": tenant_limit}).scalars().all()
    summary = {key: 0 for key in SUMMARY_KEYS}
    for tenant_id in tenant_ids:
        result = dispatch_tenant_once(engine, tenant_id, adapter=adapter,
                                      now=now, limit=batch_size)
        for key in SUMMARY_KEYS:
            summary[key] += result.get(key, 0)
    return summary


def run_forever(engine: Engine, *, poll_interval: int = POLL_INTERVAL_SECONDS,
                stop: Callable[[], bool] | None = None) -> None:
    """Poll until stopped, logging only aggregate counts and no message data."""
    if not 1 <= poll_interval <= 300:
        raise ValueError("poll interval must be between 1 and 300 seconds")
    while stop is None or not stop():
        summary = dispatch_once(engine)
        if any(summary.values()):
            logger.info("communication_dispatch counts=%s", summary)
        time.sleep(poll_interval)


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not database_url:
        logger.error("DATABASE_URL is required")
        return 2
    engine = create_engine(database_url, pool_pre_ping=True,
                           connect_args={"connect_timeout": 2})
    try:
        run_forever(engine)
    except KeyboardInterrupt:
        logger.info("communication_worker_stopped")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
