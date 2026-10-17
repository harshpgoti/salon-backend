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
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

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


class ApprovalIn(BaseModel):
    action: str = Field(pattern="^(feature_flag|tenant_suspend|tenant_reactivate|accounting_export|invoice_action|dispute_action)$")
    tenant_id: uuid.UUID | None = None
    flag: str | None = Field(default=None, min_length=1, max_length=64, pattern="^[a-z_]+$")
    provider_ref: str | None = Field(default=None, min_length=1, max_length=200)
    operation: str | None = Field(default=None, pattern="^(void|credit|accept|contest)$")
    enabled: bool | None = None
    since_days: int | None = Field(default=None, ge=1, le=365)
    reason: str = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=128)


class ApprovalDecisionIn(BaseModel):
    approve: bool
    idempotency_key: str = Field(min_length=8, max_length=128)


def _require_fresh_company_auth(identity) -> None:
    """Company sessions must have authenticated with MFA in the last 5 minutes."""
    auth_time = getattr(identity, "auth_time", None)
    if auth_time is None:
        raise HTTPException(status_code=401, detail={"code": "recent_company_mfa_required"})
    epoch = auth_time.timestamp() if isinstance(auth_time, datetime) else float(auth_time)
    if epoch > time.time() + 30 or time.time() - epoch > 300:
        raise HTTPException(status_code=401, detail={"code": "recent_company_mfa_required"})


def _reject_unapproved_write(app: FastAPI, identity, permission: str) -> None:
    engine = app.state.engine_factory()
    try:
        with engine.begin() as connection:
            role = company_scope(connection, identity)
            _require_company(role, permission)
    finally:
        engine.dispose()
    raise HTTPException(status_code=409, detail={"code": "approval_required"})


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
    def export_accounting(approval_id: uuid.UUID,
                          identity=Depends(require_company_identity)) -> dict:
        """Return a previously approved and generated accounting export."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.finance.read")
                _require_fresh_company_auth(identity)
                row = connection.execute(text("""SELECT result FROM company_action_approvals
                    WHERE id=:id AND action='accounting_export' AND status='completed'"""),
                    {"id": str(approval_id)}).scalar_one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={"code": "approved_export_not_found"})
            return row
        finally:
            engine.dispose()

    @app.get("/v1/company/approvals", tags=["company"])
    def list_approvals(identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                if role not in ("admin", "finance", "support"):
                    raise HTTPException(status_code=403, detail={"code": "company_role_forbidden"})
                rows = connection.execute(text("""SELECT id::text AS id, action, tenant_id::text AS tenant_id,
                    payload, reason, status, requested_by_subject, decided_by_subject, created_at
                    FROM company_action_approvals WHERE
                    (status='requested' AND (:role='admin' OR (action IN ('accounting_export','invoice_action','dispute_action') AND :role='finance')
                     OR (action IN ('feature_flag','tenant_suspend','tenant_reactivate') AND :role='support')))
                    OR (status='completed' AND ((action IN ('accounting_export','invoice_action','dispute_action') AND :role IN ('admin','finance'))
                     OR (action IN ('feature_flag','tenant_suspend','tenant_reactivate') AND :role IN ('admin','support'))))
                    ORDER BY created_at DESC LIMIT 100"""), {"role": role}).mappings().all()
            return {"items": [{**dict(row), "created_at": row["created_at"].isoformat()} for row in rows]}
        finally:
            engine.dispose()

    @app.post("/v1/company/approvals", tags=["company"], status_code=201)
    def request_approval(payload: ApprovalIn,
                         identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_fresh_company_auth(identity)
                if payload.action == "accounting_export":
                    _require_company(role, "company.finance.manage")
                    if payload.since_days is None or payload.tenant_id or payload.flag is not None or payload.enabled is not None or payload.provider_ref is not None or payload.operation is not None:
                        raise HTTPException(status_code=422, detail={"code": "invalid_export_request"})
                    action_payload = {"since_days": payload.since_days}
                    tenant_id = None
                elif payload.action == "feature_flag":
                    _require_company(role, "company.support.manage")
                    if payload.tenant_id is None or payload.flag is None or payload.enabled is None or payload.since_days is not None or payload.provider_ref is not None or payload.operation is not None:
                        raise HTTPException(status_code=422, detail={"code": "invalid_flag_request"})
                    connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(payload.tenant_id)})
                    if connection.execute(text("SELECT 1 FROM tenants WHERE id=:t"), {"t": str(payload.tenant_id)}).scalar_one_or_none() is None:
                        raise HTTPException(status_code=404, detail={"code": "tenant_not_found"})
                    action_payload = {"flag": payload.flag, "enabled": payload.enabled}
                    tenant_id = str(payload.tenant_id)
                elif payload.action in ("invoice_action", "dispute_action"):
                    _require_company(role, "company.finance.manage")
                    if (payload.tenant_id is None or payload.provider_ref is None or payload.operation is None
                            or payload.flag is not None or payload.enabled is not None or payload.since_days is not None):
                        raise HTTPException(status_code=422, detail={"code": "invalid_provider_action"})
                    expected = "invoice." if payload.action == "invoice_action" else "dispute."
                    connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": str(payload.tenant_id)})
                    event = connection.execute(text("""SELECT event_type FROM billing_events
                        WHERE tenant_id=:t AND provider_event_id=:ref"""),
                        {"t": str(payload.tenant_id), "ref": payload.provider_ref}).scalar_one_or_none()
                    if event is None or not event.startswith(expected):
                        raise HTTPException(status_code=404, detail={"code": "provider_event_not_found"})
                    if payload.action == "invoice_action" and payload.operation not in ("void", "credit"):
                        raise HTTPException(status_code=422, detail={"code": "invalid_invoice_operation"})
                    if payload.action == "dispute_action" and payload.operation not in ("accept", "contest"):
                        raise HTTPException(status_code=422, detail={"code": "invalid_dispute_operation"})
                    action_payload = {"provider_ref": payload.provider_ref, "operation": payload.operation}
                    tenant_id = str(payload.tenant_id)
                else:
                    _require_company(role, "company.support.manage")
                    if payload.tenant_id is None or any((payload.flag is not None, payload.enabled is not None, payload.since_days is not None, payload.provider_ref is not None, payload.operation is not None)):
                        raise HTTPException(status_code=422, detail={"code": "invalid_tenant_action"})
                    action_payload = {}
                    tenant_id = str(payload.tenant_id)
                reason = payload.reason.strip()
                if not reason:
                    raise HTTPException(status_code=422, detail={"code": "reason_required"})
                prior = connection.execute(text("""SELECT id::text AS id, action, tenant_id::text AS tenant_id,
                    payload, reason, status FROM company_action_approvals
                    WHERE requested_by_issuer=:issuer AND requested_by_subject=:subject AND idempotency_key=:key"""),
                    {"issuer": identity.issuer, "subject": identity.subject, "key": payload.idempotency_key}).mappings().one_or_none()
                if prior is not None:
                    if (prior["action"], prior["tenant_id"], prior["payload"], prior["reason"]) != (payload.action, tenant_id, action_payload, reason):
                        raise HTTPException(status_code=409, detail={"code": "idempotency_key_reused"})
                    return {"id": prior["id"], "action": prior["action"], "status": prior["status"], "replayed": True}
                row = connection.execute(text("""INSERT INTO company_action_approvals
                    (action,tenant_id,payload,reason,idempotency_key,requested_by_issuer,requested_by_subject)
                    VALUES (:action,:tenant,CAST(:payload AS jsonb),:reason,:key,:issuer,:subject)
                    ON CONFLICT (requested_by_issuer,requested_by_subject,idempotency_key) DO NOTHING
                    RETURNING id::text AS id, action, status"""),
                    {"action": payload.action, "tenant": tenant_id, "payload": json.dumps(action_payload),
                     "reason": reason, "key": payload.idempotency_key, "issuer": identity.issuer,
                     "subject": identity.subject}).mappings().one_or_none()
                if row is None:
                    prior = connection.execute(text("""SELECT id::text AS id,action,tenant_id::text AS tenant_id,
                        payload,reason,status FROM company_action_approvals WHERE requested_by_issuer=:issuer
                        AND requested_by_subject=:subject AND idempotency_key=:key"""),
                        {"issuer": identity.issuer, "subject": identity.subject, "key": payload.idempotency_key}).mappings().one()
                    if (prior["action"], prior["tenant_id"], prior["payload"], prior["reason"]) != (payload.action, tenant_id, action_payload, reason):
                        raise HTTPException(status_code=409, detail={"code": "idempotency_key_reused"})
                    return {"id": prior["id"], "action": prior["action"], "status": prior["status"], "replayed": True}
                _company_audit(connection, identity=identity, action="company.action_requested",
                    resource_type="company_action_approval", resource_id=row["id"], reason=f"{payload.action}: {reason}")
            return {**dict(row), "replayed": False}
        finally:
            engine.dispose()

    @app.post("/v1/company/approvals/{approval_id}/decide", tags=["company"])
    def decide_approval(approval_id: uuid.UUID, payload: ApprovalDecisionIn,
                        identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity, lock=True)
                _require_fresh_company_auth(identity)
                row = connection.execute(text("""SELECT * FROM company_action_approvals
                    WHERE id=:id FOR UPDATE"""), {"id": str(approval_id)}).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={"code": "approval_not_found"})
                if row["action"] in ("accounting_export", "invoice_action", "dispute_action") or row["action"].startswith("finance_"):
                    _require_company(role, "company.finance.manage")
                else:
                    _require_company(role, "company.support.manage")
                if (row["requested_by_issuer"], row["requested_by_subject"]) == (identity.issuer, identity.subject):
                    raise HTTPException(status_code=422, detail={"code": "four_eyes"})
                if row["status"] in ("completed", "rejected") and row["decision_key"] == payload.idempotency_key:
                    if (row["status"] == "completed") != payload.approve:
                        raise HTTPException(status_code=409, detail={"code": "idempotency_key_reused"})
                    return {"id": str(approval_id), "status": row["status"], "result": row["result"], "replayed": True}
                if row["status"] != "requested":
                    raise HTTPException(status_code=409, detail={"code": "approval_not_pending"})
                status = "rejected" if not payload.approve else "completed"
                result = None
                if payload.approve:
                    action_payload = row["payload"]
                    if row["action"] == "accounting_export":
                        days = int(action_payload["since_days"])
                        items = connection.execute(text("SELECT company_finance_ledger(now() - (:days || ' days')::interval)"), {"days": str(days)}).scalar_one() or []
                        result = {"generated_at": datetime.now(timezone.utc).isoformat(), "currency": "CAD",
                            "definitions": "Approved provider money events and finance adjustments; no card or client data.", "items": items}
                    elif row["action"] in ("invoice_action", "dispute_action"):
                        result = {"provider_ref": action_payload["provider_ref"],
                            "operation": action_payload["operation"],
                            "execution": "approval_recorded_only",
                            "message": "No live provider action was performed in this synthetic environment."}
                        _company_audit(connection, identity=identity, action="company.provider_finance_action_approved",
                            resource_type=row["action"], resource_id=action_payload["provider_ref"], reason=row["reason"])
                    elif row["action"] == "feature_flag":
                        connection.execute(text("""INSERT INTO company_feature_flags(tenant_id,flag,enabled)
                            VALUES (:t,:flag,:enabled) ON CONFLICT (tenant_id,flag) DO UPDATE SET
                            enabled=EXCLUDED.enabled,updated_at=now()"""),
                            {"t": str(row["tenant_id"]), "flag": action_payload["flag"], "enabled": action_payload["enabled"]})
                        result = {"tenant_id": str(row["tenant_id"]), **action_payload}
                        _company_audit(connection, identity=identity, action="company.flag_set",
                            resource_type="company_feature_flag",
                            resource_id=f"{row['tenant_id']}:{action_payload['flag']}",
                            reason=f"approved change; enabled={action_payload['enabled']}")
                    elif row["action"] in ("tenant_suspend", "tenant_reactivate"):
                        tid = str(row["tenant_id"])
                        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
                        sub = connection.execute(text("SELECT status,status_before_suspension FROM tenant_subscriptions WHERE tenant_id=:t FOR UPDATE"), {"t": tid}).mappings().one_or_none()
                        if sub is None: raise HTTPException(status_code=404, detail={"code": "subscription_not_found"})
                        if row["action"] == "tenant_suspend":
                            if sub["status"] == "suspended": raise HTTPException(status_code=409, detail={"code": "already_suspended"})
                            connection.execute(text("UPDATE tenant_subscriptions SET status_before_suspension=:prior,status='suspended',version=version+1,updated_at=now() WHERE tenant_id=:t"), {"prior": sub["status"], "t": tid})
                            result = {"tenant_id": tid, "status": "suspended", "was": sub["status"]}
                            _company_audit(connection, identity=identity, action="company.tenant_suspended",
                                resource_type="tenant_subscription", resource_id=tid, reason=row["reason"])
                        else:
                            if sub["status"] != "suspended": raise HTTPException(status_code=409, detail={"code": "not_suspended"})
                            restored = sub["status_before_suspension"] or "active"
                            connection.execute(text("UPDATE tenant_subscriptions SET status=:status,status_before_suspension=NULL,version=version+1,updated_at=now() WHERE tenant_id=:t"), {"status": restored, "t": tid})
                            result = {"tenant_id": tid, "status": restored}
                            _company_audit(connection, identity=identity, action="company.tenant_reactivated",
                                resource_type="tenant_subscription", resource_id=tid, reason=row["reason"])
                        from modules.company import rebuild_entitlements
                        rebuild_entitlements(connection, tenant_id=tid)
                connection.execute(text("""UPDATE company_action_approvals SET status=:status,
                    decided_by_issuer=:issuer,decided_by_subject=:subject,decision_key=:key,
                    result=CAST(:result AS jsonb),updated_at=now() WHERE id=:id"""),
                    {"status": status, "issuer": identity.issuer, "subject": identity.subject,
                     "key": payload.idempotency_key, "result": json.dumps(result) if result is not None else None,
                     "id": str(approval_id)})
                _company_audit(connection, identity=identity, action="company.action_decided",
                    resource_type="company_action_approval", resource_id=str(approval_id),
                    reason=f"{row['action']}: {status}")
            return {"id": str(approval_id), "status": status, "result": result, "replayed": False}
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
                           idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
                           identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.finance.manage")
                _require_fresh_company_auth(identity)
                name = connection.execute(
                    text("SELECT company_tenant_name(:t)"),
                    {"t": str(payload.tenant_id)}).scalar_one_or_none()
                if name is None:
                    raise HTTPException(status_code=404, detail={"code": "tenant_not_found"})
                reason = payload.reason.strip()
                if not reason:
                    raise HTTPException(status_code=422, detail={"code": "reason_required"})
                prior = connection.execute(text("""SELECT id::text AS id,tenant_id::text AS tenant_id,kind,amount_minor,reason,status
                    FROM finance_adjustments WHERE requested_by_issuer=:issuer AND requested_by_subject=:subject
                    AND request_key=:key"""), {"issuer": identity.issuer, "subject": identity.subject,
                    "key": idempotency_key}).mappings().one_or_none()
                if prior is not None:
                    if (prior["tenant_id"], prior["kind"], prior["amount_minor"], prior["reason"]) != (str(payload.tenant_id), payload.kind, payload.amount_minor, reason):
                        raise HTTPException(status_code=409, detail={"code": "idempotency_key_reused"})
                    return {"id": prior["id"], "status": prior["status"], "replayed": True}
                row = connection.execute(
                    text("""INSERT INTO finance_adjustments
                            (tenant_id, kind, amount_minor, reason,
                             requested_by_issuer, requested_by_subject,request_key)
                            VALUES (:tenant, :kind, :amount, :reason, :issuer, :subject,:key)
                            ON CONFLICT (requested_by_issuer,requested_by_subject,request_key)
                            WHERE request_key IS NOT NULL DO NOTHING
                            RETURNING id::text AS id, status"""),
                    {"tenant": str(payload.tenant_id), "kind": payload.kind,
                     "amount": payload.amount_minor, "reason": reason,
                     "issuer": identity.issuer, "subject": identity.subject,"key": idempotency_key},
                ).mappings().one_or_none()
                if row is None:
                    prior = connection.execute(text("""SELECT id::text AS id,tenant_id::text AS tenant_id,kind,amount_minor,reason,status
                        FROM finance_adjustments WHERE requested_by_issuer=:issuer
                        AND requested_by_subject=:subject AND request_key=:key"""),
                        {"issuer": identity.issuer, "subject": identity.subject, "key": idempotency_key}).mappings().one()
                    if (prior["tenant_id"], prior["kind"], prior["amount_minor"], prior["reason"]) != (str(payload.tenant_id), payload.kind, payload.amount_minor, reason):
                        raise HTTPException(status_code=409, detail={"code": "idempotency_key_reused"})
                    return {"id": prior["id"], "status": prior["status"], "replayed": True}
                _company_audit(connection, identity=identity, action="company.adjustment_requested",
                               resource_type="finance_adjustment", resource_id=row["id"],
                               reason=f"{payload.kind} {payload.amount_minor}")
            return {**dict(row), "replayed": False}
        finally:
            engine.dispose()

    @app.get("/v1/company/finance/adjustments", tags=["company"])
    def list_adjustments(identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.finance.read")
                rows = connection.execute(text("""SELECT id::text AS id,tenant_id::text AS tenant_id,
                    kind,amount_minor,reason,status,requested_by_subject,approved_by_subject,created_at
                    FROM finance_adjustments WHERE status='requested' ORDER BY created_at""")).mappings().all()
            return {"items": [{**dict(row), "created_at": row["created_at"].isoformat()} for row in rows]}
        finally:
            engine.dispose()

    @app.post("/v1/company/finance/adjustments/{adjustment_id}/decide", tags=["company"])
    def decide_adjustment(adjustment_id: uuid.UUID, approve: bool,
                          idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
                          identity=Depends(require_company_identity)) -> dict:
        """Four-eyes decision: approver must differ from requester."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.finance.manage")
                _require_fresh_company_auth(identity)
                current = connection.execute(
                    text("""SELECT requested_by_issuer, requested_by_subject, status,decision_key,provider_ref
                            FROM finance_adjustments WHERE id=:id FOR UPDATE"""),
                    {"id": str(adjustment_id)}).mappings().one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "adjustment_not_found"})
                if current["status"] == ("executed" if approve else "rejected") and current["decision_key"] == idempotency_key:
                    return {"id": str(adjustment_id), "status": current["status"], "provider_ref": current["provider_ref"], "replayed": True}
                if current["decision_key"] == idempotency_key:
                    raise HTTPException(status_code=409, detail={"code": "idempotency_key_reused"})
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
                                decision_key=:decision_key,
                                provider_ref=CASE WHEN :status='approved'
                                                  THEN 'adj_test_' || left(CAST(:id AS text), 8)
                                                  END,
                                version=version+1, updated_at=now()
                            WHERE id=:id AND status='requested'
                            RETURNING id::text AS id, status, provider_ref"""),
                    {"id": str(adjustment_id), "status": status,
                     "issuer": identity.issuer, "subject": identity.subject,
                     "decision_key": idempotency_key},
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
            return {**dict(row), "replayed": False}
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
    def set_flag(payload: FlagIn, identity=Depends(require_company_identity)) -> None:
        _reject_unapproved_write(app, identity, "company.support.manage")

    @app.post("/v1/company/tenants/{tenant_id}/suspend", tags=["company"])
    def suspend_tenant(tenant_id: uuid.UUID, payload: SuspendIn,
                       identity=Depends(require_company_identity)) -> None:
        """Security suspension, independent of billing (FR-068/FR-074)."""
        _reject_unapproved_write(app, identity, "company.support.manage")

    @app.post("/v1/company/tenants/{tenant_id}/reactivate", tags=["company"])
    def reactivate_tenant(tenant_id: uuid.UUID,
                          identity=Depends(require_company_identity)) -> None:
        _reject_unapproved_write(app, identity, "company.support.manage")

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
