"""Company finance adjustments and operations controls (FR-073/FR-074; TASK-052).

Money moves only through requests with four eyes: the approver must differ
from the requester, every step is company-audited, and execution records a
synthetic provider reference (sandbox; DQ-006). Finance roles see money and
registries — never client detail or service notes (no such grant exists, and
a test proves the denial).

- FR-073: invoice/payment history (provider trail), tax config (NULL until
  counsel sets it — the product assumes no rates), refund/credit/discount
  workflow, reconciliation anomalies, accounting export.
- FR-074: provider incidents, queue/DLQ/webhook status, per-tenant feature
  flags (enforced, not decorative), audited suspension/reactivation, abuse
  rate limits on provider webhooks, public status notices.
"""

from __future__ import annotations

import json
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from modules.company import _company_audit, _require_company, company_scope

__all__ = [
    "FinanceModuleError",
    "flag_enabled",
    "register_finance_routes",
]

# Abuse guard for provider webhooks: 60 calls per tenant per minute. Process
# memory by design (dev/small deploys); a multi-worker production needs the
# same window in Redis — recorded as a residual, not silent behavior.
RATE_LIMIT_MAX = 60
RATE_LIMIT_WINDOW = timedelta(minutes=1)
_rate_lock = threading.Lock()
_rate_hits: dict[tuple[str, str], list[datetime]] = {}


class FinanceModuleError(Exception):
    """Base exception for finance operations."""


def check_provider_rate(tenant_id: str, route: str,
                        now: datetime | None = None) -> None:
    """Sliding-window abuse guard; raises 429 past the allowance."""
    moment = now or datetime.now(timezone.utc)
    key = (tenant_id, route)
    with _rate_lock:
        hits = _rate_hits.get(key, [])
        hits = [hit for hit in hits if moment - hit < RATE_LIMIT_WINDOW]
        if len(hits) >= RATE_LIMIT_MAX:
            _rate_hits[key] = hits
            raise HTTPException(status_code=429, detail={
                "code": "provider_rate_limited",
                "message": "too many provider calls; retry shortly"})
        hits.append(moment)
        _rate_hits[key] = hits


def flag_enabled(connection, *, tenant_id: str, flag: str,
                 default: bool = True) -> bool:
    """Kill switches with per-tenant override: tenant row wins, else the
    global row, else the default. Flags nobody reads are theater — every flag
    added here must have a reader; rebooking_prompts_enabled gates the worker.
    """
    row = connection.execute(
        text("""SELECT enabled FROM company_feature_flags
                WHERE flag=:flag AND (tenant_id=:tenant OR tenant_id IS NULL)
                ORDER BY (tenant_id IS NULL) LIMIT 1"""),
        {"tenant": tenant_id, "flag": flag}).scalar_one_or_none()
    return default if row is None else bool(row)


class AdjustmentIn(BaseModel):
    tenant_id: uuid.UUID
    kind: str = Field(pattern="^(refund|credit|discount)$")
    amount_minor: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=500)


class TaxConfigIn(BaseModel):
    gst_hst_rate: float | None = Field(default=None, ge=0, le=1)
    pst_rate: float | None = Field(default=None, ge=0, le=1)
    qst_rate: float | None = Field(default=None, ge=0, le=1)
    notes: str | None = Field(default=None, max_length=2000)


class IncidentIn(BaseModel):
    provider: str = Field(min_length=1, max_length=80)
    severity: str = Field(pattern="^(low|medium|high|critical)$")
    title: str = Field(min_length=1, max_length=160)
    detail: str | None = Field(default=None, max_length=4000)


class IncidentStatusIn(BaseModel):
    status: str = Field(pattern="^(mitigated|resolved)$")


class FlagIn(BaseModel):
    tenant_id: uuid.UUID | None = None
    flag: str = Field(min_length=1, max_length=64, pattern="^[a-z_]+$")
    enabled: bool


class SuspendIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class NoticeIn(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=2000)
    severity: str = Field(pattern="^(info|degraded|outage)$")


def register_finance_routes(
    app: FastAPI,
    *,
    require_company_identity: Callable,
) -> None:
    @app.get("/v1/company/finance/ledger", tags=["company"])
    def read_ledger(since_days: int = 30,
                    identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.finance.read")
                if not 1 <= since_days <= 365:
                    raise HTTPException(status_code=422, detail={"code": "invalid_window"})
                rows = connection.execute(
                    text("SELECT company_finance_ledger(now() - (:days || ' days')::interval)"),
                    {"days": str(since_days)}).scalar_one()
            return {"items": rows or []}
        finally:
            engine.dispose()

    @app.get("/v1/company/finance/reconciliation", tags=["company"])
    def read_reconciliation(identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.finance.read")
                rows = connection.execute(
                    text("SELECT company_reconciliation()")).scalar_one()
            return {"anomalies": rows or []}
        finally:
            engine.dispose()

    @app.get("/v1/company/finance/export", tags=["company"])
    def export_accounting(since_days: int = 90,
                          identity=Depends(require_company_identity)) -> dict:
        """Accounting export: adjustments plus money events with definitions."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.finance.read")
                if not 1 <= since_days <= 365:
                    raise HTTPException(status_code=422, detail={"code": "invalid_window"})
                ledger = connection.execute(
                    text("SELECT company_finance_ledger(now() - (:days || ' days')::interval)"),
                    {"days": str(since_days)}).scalar_one()
            return {"generated_at": datetime.now(timezone.utc).isoformat(),
                    "currency": "CAD",
                    "definitions": "Provider money events plus approved finance adjustments; "
                                   "refunds/credits/discounts reduce net; no card data anywhere.",
                    "items": ledger or []}
        finally:
            engine.dispose()

    @app.get("/v1/company/finance/tax-config", tags=["company"])
    def read_tax_config(identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.finance.read")
                row = connection.execute(
                    text("""SELECT country, gst_hst_rate, pst_rate, qst_rate, notes
                            FROM company_tax_config WHERE id=1""")).mappings().one()
            return dict(row)
        finally:
            engine.dispose()

    @app.put("/v1/company/finance/tax-config", tags=["company"])
    def write_tax_config(payload: TaxConfigIn,
                         identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.finance.manage")
                connection.execute(
                    text("""UPDATE company_tax_config SET gst_hst_rate=:gst, pst_rate=:pst,
                                   qst_rate=:qst, notes=:notes, updated_at=now() WHERE id=1"""),
                    {"gst": payload.gst_hst_rate, "pst": payload.pst_rate,
                     "qst": payload.qst_rate, "notes": payload.notes or ""})
                _company_audit(connection, identity=identity, action="company.tax_configured",
                               resource_type="company_tax_config", resource_id="1",
                               reason="tax behaviour configured")
            return {"updated": True}
        finally:
            engine.dispose()

    @app.post("/v1/company/finance/adjustments", tags=["company"], status_code=201)
    def request_adjustment(payload: AdjustmentIn,
                           identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.finance.manage")
                name = connection.execute(
                    text("SELECT company_tenant_name(:t)"),
                    {"t": str(payload.tenant_id)}).scalar_one_or_none()
                if name is None:
                    raise HTTPException(status_code=404, detail={"code": "tenant_not_found"})
                row = connection.execute(
                    text("""INSERT INTO finance_adjustments
                            (tenant_id, kind, amount_minor, reason,
                             requested_by_issuer, requested_by_subject)
                            VALUES (:tenant, :kind, :amount, :reason, :issuer, :subject)
                            RETURNING id::text AS id, status"""),
                    {"tenant": str(payload.tenant_id), "kind": payload.kind,
                     "amount": payload.amount_minor, "reason": payload.reason.strip(),
                     "issuer": identity.issuer, "subject": identity.subject},
                ).mappings().one()
                _company_audit(connection, identity=identity, action="company.adjustment_requested",
                               resource_type="finance_adjustment", resource_id=row["id"],
                               reason=f"{payload.kind} {payload.amount_minor}")
            return dict(row)
        finally:
            engine.dispose()

    @app.post("/v1/company/finance/adjustments/{adjustment_id}/decide", tags=["company"])
    def decide_adjustment(adjustment_id: uuid.UUID, approve: bool,
                          identity=Depends(require_company_identity)) -> dict:
        """Four-eyes decision: approver must differ from requester."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.finance.manage")
                current = connection.execute(
                    text("""SELECT requested_by_issuer, requested_by_subject, status
                            FROM finance_adjustments WHERE id=:id"""),
                    {"id": str(adjustment_id)}).mappings().one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "adjustment_not_found"})
                if current["status"] != "requested":
                    raise HTTPException(status_code=409, detail={
                        "code": "adjustment_not_pending"})
                if (current["requested_by_issuer"] == identity.issuer
                        and current["requested_by_subject"] == identity.subject):
                    raise HTTPException(status_code=422, detail={
                        "code": "four_eyes",
                        "message": "approver must differ from requester"})
                status = "approved" if approve else "rejected"
                row = connection.execute(
                    text("""UPDATE finance_adjustments
                            SET status=:status, approved_by_issuer=:issuer,
                                approved_by_subject=:subject,
                                provider_ref=CASE WHEN :status='approved'
                                                  THEN 'adj_test_' || left(CAST(:id AS text), 8)
                                                  END,
                                version=version+1, updated_at=now()
                            WHERE id=:id AND status='requested'
                            RETURNING id::text AS id, status, provider_ref"""),
                    {"id": str(adjustment_id), "status": status,
                     "issuer": identity.issuer, "subject": identity.subject},
                ).mappings().one()
                _company_audit(connection, identity=identity, action="company.adjustment_decided",
                               resource_type="finance_adjustment", resource_id=row["id"],
                               reason=status)
                if approve:
                    connection.execute(
                        text("""UPDATE finance_adjustments SET status='executed',
                                       version=version+1, updated_at=now()
                                WHERE id=:id AND status='approved'"""),
                        {"id": str(adjustment_id)})
                    row = {**dict(row), "status": "executed"}
            return dict(row)
        finally:
            engine.dispose()

    @app.get("/v1/company/ops/queues", tags=["company"])
    def read_queues(identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.support.manage")
                status = connection.execute(
                    text("SELECT company_ops_queues()")).scalar_one()
            return dict(status) if isinstance(status, dict) else status
        finally:
            engine.dispose()

    @app.post("/v1/company/incidents", tags=["company"], status_code=201)
    def open_incident(payload: IncidentIn,
                      identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.support.manage")
                row = connection.execute(
                    text("""INSERT INTO provider_incidents
                            (provider, severity, title, detail)
                            VALUES (:provider, :severity, :title, :detail)
                            RETURNING id::text AS id, status"""),
                    {"provider": payload.provider.strip(), "severity": payload.severity,
                     "title": payload.title.strip(),
                     "detail": payload.detail or ""}).mappings().one()
                _company_audit(connection, identity=identity, action="company.incident_opened",
                               resource_type="provider_incident", resource_id=row["id"],
                               reason=payload.title.strip())
            return dict(row)
        finally:
            engine.dispose()

    @app.post("/v1/company/incidents/{incident_id}/status", tags=["company"])
    def move_incident(incident_id: uuid.UUID, payload: IncidentStatusIn,
                      identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.support.manage")
                row = connection.execute(
                    text("""UPDATE provider_incidents SET status=:status,
                                   resolved_at=CASE WHEN :status='resolved' THEN now() END,
                                   updated_at=now()
                            WHERE id=:id AND status <> 'resolved'
                            RETURNING id::text AS id, status"""),
                    {"id": str(incident_id), "status": payload.status},
                ).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={
                        "code": "incident_not_found_or_resolved"})
                _company_audit(connection, identity=identity, action="company.incident_moved",
                               resource_type="provider_incident", resource_id=row["id"],
                               reason=payload.status)
            return dict(row)
        finally:
            engine.dispose()

    @app.put("/v1/company/flags", tags=["company"])
    def set_flag(payload: FlagIn, identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.support.manage")
                if payload.tenant_id is not None:
                    name = connection.execute(
                        text("SELECT company_tenant_name(:t)"),
                        {"t": str(payload.tenant_id)}).scalar_one_or_none()
                    if name is None:
                        raise HTTPException(status_code=404, detail={"code": "tenant_not_found"})
                connection.execute(
                    text("""INSERT INTO company_feature_flags (tenant_id, flag, enabled)
                            VALUES (:tenant, :flag, :enabled)
                            ON CONFLICT (tenant_id, flag) DO UPDATE SET
                                enabled=EXCLUDED.enabled, updated_at=now()"""),
                    {"tenant": str(payload.tenant_id) if payload.tenant_id else None,
                     "flag": payload.flag, "enabled": payload.enabled})
                _company_audit(connection, identity=identity, action="company.flag_set",
                               resource_type="company_feature_flag",
                               resource_id=f"{payload.tenant_id}:{payload.flag}",
                               reason=f"enabled={payload.enabled}")
            return {"tenant_id": str(payload.tenant_id) if payload.tenant_id else None,
                    "flag": payload.flag, "enabled": payload.enabled}
        finally:
            engine.dispose()

    @app.post("/v1/company/tenants/{tenant_id}/suspend", tags=["company"])
    def suspend_tenant(tenant_id: uuid.UUID, payload: SuspendIn,
                       identity=Depends(require_company_identity)) -> dict:
        """Security suspension, independent of billing (FR-068/FR-074)."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.support.manage")
                # Scope follows the role check (same pattern as provider
                # webhooks): the company role authorizes, the scope selects rows.
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"),
                    {"t": str(tenant_id)})
                current = connection.execute(
                    text("""SELECT status FROM tenant_subscriptions WHERE tenant_id=:t"""),
                    {"t": str(tenant_id)}).scalar_one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "subscription_not_found"})
                if current == "suspended":
                    raise HTTPException(status_code=409, detail={"code": "already_suspended"})
                connection.execute(
                    text("""UPDATE tenant_subscriptions
                            SET status_before_suspension=:prior, status='suspended',
                                version=version+1, updated_at=now()
                            WHERE tenant_id=:t"""),
                    {"t": str(tenant_id), "prior": current})
                from modules.company import rebuild_entitlements
                rebuild_entitlements(connection, tenant_id=str(tenant_id))
                _company_audit(connection, identity=identity, action="company.tenant_suspended",
                               resource_type="tenant_subscription", resource_id=str(tenant_id),
                               reason=payload.reason.strip())
            return {"tenant_id": str(tenant_id), "status": "suspended",
                    "was": current}
        finally:
            engine.dispose()

    @app.post("/v1/company/tenants/{tenant_id}/reactivate", tags=["company"])
    def reactivate_tenant(tenant_id: uuid.UUID,
                          identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.support.manage")
                connection.execute(
                    text("SELECT set_config('app.tenant_id', :t, true)"),
                    {"t": str(tenant_id)})
                row = connection.execute(
                    text("""SELECT status, status_before_suspension FROM tenant_subscriptions
                            WHERE tenant_id=:t"""),
                    {"t": str(tenant_id)}).mappings().one_or_none()
                if row is None or row["status"] != "suspended":
                    raise HTTPException(status_code=404, detail={
                        "code": "not_suspended",
                        "message": "only a suspended account reactivates"})
                restored = row["status_before_suspension"] or "active"
                connection.execute(
                    text("""UPDATE tenant_subscriptions
                            SET status=:status, status_before_suspension=NULL,
                                version=version+1, updated_at=now()
                            WHERE tenant_id=:t"""),
                    {"t": str(tenant_id), "status": restored})
                from modules.company import rebuild_entitlements
                rebuild_entitlements(connection, tenant_id=str(tenant_id))
                _company_audit(connection, identity=identity,
                               action="company.tenant_reactivated",
                               resource_type="tenant_subscription", resource_id=str(tenant_id),
                               reason=f"restored to {restored}")
            return {"tenant_id": str(tenant_id), "status": restored}
        finally:
            engine.dispose()

    @app.post("/v1/company/notices", tags=["company"], status_code=201)
    def publish_notice(payload: NoticeIn,
                       identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.support.manage")
                row = connection.execute(
                    text("""INSERT INTO status_notices (title, body, severity)
                            VALUES (:title, :body, :severity)
                            RETURNING id::text AS id"""),
                    {"title": payload.title.strip(), "body": payload.body.strip(),
                     "severity": payload.severity}).mappings().one()
                _company_audit(connection, identity=identity, action="company.notice_published",
                               resource_type="status_notice", resource_id=row["id"],
                               reason=payload.title.strip())
            return dict(row)
        finally:
            engine.dispose()

    @app.get("/v1/public/status", tags=["company"])
    def read_public_status() -> dict:
        """Unauthenticated status page feed: active notices only, no internals."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                rows = connection.execute(
                    text("""SELECT id::text AS id, title, body, severity, created_at
                            FROM status_notices WHERE is_active ORDER BY created_at DESC"""),
                ).mappings().all()
            return {"notices": [{**dict(r), "created_at": r["created_at"].isoformat()}
                                for r in rows]}
        finally:
            engine.dispose()
