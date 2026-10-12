"""Operational alert evaluation (FR-078; TASK-060).

ALERT_RULES are evaluated against ``metrics_snapshot`` (trace.py) plus the
queue tables it already counts. A breach opens one 'active' row in
``ops_alerts``; recovery resolves it. Dedupe is structural (partial unique
index: one active alert per rule per tenant).

Pure functions + SQL only: no route registration here. Thresholds are
starting hypotheses for owner review (TASK-060-oncall.md §3), and owners
are placeholders until DQ-006 names humans.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import text

from modules.trace import metrics_snapshot

__all__ = [
    "ALERT_RULES",
    "OpsAlertsError",
    "evaluate_alerts",
    "list_active_alerts",
    "resolve_alert",
]


class OpsAlertsError(Exception):
    """Base exception for ops alert operations."""


# metric: key in metrics_snapshot, except "provider_error_rate" which is
# derived as callback_failures_24h / communications_recorded_24h.
# window: observation basis (informational; snapshot windows are fixed in
# trace.py). owner_placeholder: DQ-006 role slot, not a person.
ALERT_RULES = [
    {"rule": "failed_communications", "metric": "failed_communications",
     "threshold": 5, "window": "15m", "severity": "major",
     "owner_placeholder": "[ONCALL-PRIMARY]"},
    {"rule": "dead_letters_open", "metric": "open_dead_letters",
     "threshold": 3, "window": "15m", "severity": "major",
     "owner_placeholder": "[ONCALL-PRIMARY]"},
    {"rule": "retry_queue_depth", "metric": "open_retry_queue",
     "threshold": 10, "window": "15m", "severity": "minor",
     "owner_placeholder": "[ONCALL-BACKUP]"},
    {"rule": "provider_error_rate", "metric": "provider_error_rate",
     "threshold": 0.2, "window": "24h", "severity": "major",
     "owner_placeholder": "[ONCALL-PRIMARY]"},
    {"rule": "spend_spike_24h", "metric": "spend_24h_minor",
     "threshold": 10000, "window": "24h", "severity": "critical",
     "owner_placeholder": "[FINANCE-OWNER]"},
]

_ZERO_TENANT = "00000000-0000-0000-0000-000000000000"


def _rule_for(name: str) -> dict:
    for rule in ALERT_RULES:
        if rule["rule"] == name:
            return rule
    raise OpsAlertsError(f"unknown_rule: {name!r}")


def _metric_value(snapshot: dict, rule: dict) -> float:
    if rule["metric"] == "provider_error_rate":
        recorded = snapshot.get("communications_recorded_24h") or 0
        if not recorded:
            return 0.0
        return (snapshot.get("callback_failures_24h") or 0) / recorded
    return float(snapshot.get(rule["metric"]) or 0)


def _row_to_dict(row) -> dict:
    return {"id": str(row["id"]), "tenant_id": str(row["tenant_id"]) if row["tenant_id"] else None,
            "rule": row["rule"], "severity": row["severity"], "state": row["state"],
            "metric_value": float(row["metric_value"]) if row["metric_value"] is not None else None,
            "fired_at": row["fired_at"].isoformat() if row["fired_at"] else None,
            "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] else None}


def _active_row(connection, *, rule: str, tenant_id: str | None):
    return connection.execute(
        text("""SELECT * FROM ops_alerts
                WHERE rule=:rule AND state='active'
                  AND COALESCE(tenant_id, :zero) = COALESCE(:tenant, :zero)
                LIMIT 1"""),
        {"rule": rule, "tenant": tenant_id, "zero": _ZERO_TENANT}).mappings().first()


def evaluate_alerts(connection, *, tenant_id: str | None = None) -> list[dict]:
    """Evaluate every rule; open alerts on breach, resolve on recovery.

    Returns the alerts active after evaluation (newly fired + still
    breaching). Re-evaluation while breached returns the existing row —
    never a duplicate. Recovery (metric below threshold) resolves the
    active row with resolved_at=now().
    """
    if tenant_id is not None:
        # Transaction-local scope so the snapshot counts this tenant's
        # queue tables (same set_config discipline as the trace routes).
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                           {"t": tenant_id})
    snapshot = metrics_snapshot(connection, tenant_id=tenant_id)
    active: list[dict] = []
    for rule in ALERT_RULES:
        value = _metric_value(snapshot, rule)
        existing = _active_row(connection, rule=rule["rule"], tenant_id=tenant_id)
        if value >= rule["threshold"]:
            if existing is None:
                row = connection.execute(
                    text("""INSERT INTO ops_alerts (tenant_id, rule, severity, metric_value)
                            VALUES (:tenant, :rule, :severity, :value)
                            ON CONFLICT DO NOTHING
                            RETURNING *"""),
                    {"tenant": tenant_id, "rule": rule["rule"],
                     "severity": rule["severity"], "value": value}).mappings().first()
                # Lost a race with a concurrent evaluator: read the winner.
                if row is None:
                    row = _active_row(connection, rule=rule["rule"], tenant_id=tenant_id)
                active.append(_row_to_dict(row))
            else:
                active.append(_row_to_dict(existing))
        elif existing is not None:
            connection.execute(
                text("""UPDATE ops_alerts SET state='resolved', resolved_at=now()
                        WHERE id=:id AND state='active'"""),
                {"id": existing["id"]})
    return active


def resolve_alert(connection, *, rule: str, tenant_id: str | None = None) -> dict | None:
    """Close the active alert for a rule; None when none is active."""
    _rule_for(rule)  # rejects unknown rules before touching state
    existing = _active_row(connection, rule=rule, tenant_id=tenant_id)
    if existing is None:
        return None
    row = connection.execute(
        text("""UPDATE ops_alerts SET state='resolved', resolved_at=now()
                WHERE id=:id AND state='active' RETURNING *"""),
        {"id": existing["id"]}).mappings().first()
    return _row_to_dict(row) if row is not None else None


def list_active_alerts(connection, *, tenant_id: str | None = None) -> list[dict]:
    """Alerts currently active (tenant-scoped, or global when None)."""
    rows = connection.execute(
        text("""SELECT * FROM ops_alerts
                WHERE state='active'
                  AND (CAST(:tenant AS uuid) IS NULL AND tenant_id IS NULL
                       OR tenant_id = CAST(:tenant AS uuid))
                ORDER BY fired_at"""),
        {"tenant": tenant_id}).mappings().all()
    return [_row_to_dict(r) for r in rows]
