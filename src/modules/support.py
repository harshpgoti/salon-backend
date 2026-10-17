"""Support cases and tenant-approved access (FR-071/FR-072; TASK-051).

Cases are company-internal rows linked to a tenant by id — never by client
data. The diagnostic view a grant unlocks is masked by construction
(counts and identifiers from a SECURITY DEFINER function; the query selects
nothing else). Internal notes are redacted on write (emails, phones and long
digit runs become blocks), so a pasted transcript cannot smuggle PII into
the case file.

- AC-1: a case correlates the safe booking/job/trace identifiers; the grant
  behind it defaults to masked diagnostic scope and thirty minutes.
- AC-2: grants move requested -> approved (tenant owner) -> expired/revoked;
  scope, expiry and revocation are enforced on every diagnostic read, and
  every read is logged where the tenant can see it. No write access exists
  anywhere in this module.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from modules.company import _company_audit, _require_company, company_scope

__all__ = [
    "GRANT_MINUTES",
    "SupportModuleError",
    "redact_note",
    "register_support_routes",
]

# FR-072 proposed expiry: thirty minutes from tenant approval.
GRANT_MINUTES = 30

CASE_STATUSES = ("open", "triaged", "assigned", "resolved", "closed")
GRANT_SCOPES = ("diagnostic", "billing")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?[0-9][0-9 .()-]{6,}[0-9]")
_LONG_DIGITS_RE = re.compile(r"[0-9]{6,}")


class SupportModuleError(Exception):
    """Base exception for support operations."""


def redact_note(note: str) -> str:
    """Mask contact identifiers in free text before it reaches the case file."""
    redacted = _EMAIL_RE.sub("[redacted-email]", note)
    redacted = _PHONE_RE.sub("[redacted-phone]", redacted)
    return _LONG_DIGITS_RE.sub("[redacted-id]", redacted)


def _as_json(value):
    if value is None:
        return None
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, dict):
        return value
    return list(value)


def _grant_valid(connection, *, grant_id: str, tenant_id: str, identity) -> dict | None:
    """Authorize the requesting employee's diagnostic read of a working case.

    Callers first lock the active employee via company_scope(lock=True).
    Share locks keep grant revocation and case closure ordered after this read.
    Invalid/expired grants are not locked; a denial does not alter other grants.
    """
    row = connection.execute(
        text("""SELECT g.id::text AS id, g.case_id::text AS case_id,
                       g.scope, g.status, g.expires_at
                FROM support_access_grants g
                JOIN support_cases c ON c.id=g.case_id AND c.tenant_id=g.tenant_id
                WHERE g.id=:id AND g.tenant_id=:tenant
                  AND g.status='approved' AND g.scope='diagnostic'
                  AND g.requested_by_issuer=:issuer AND g.requested_by_subject=:subject
                  AND g.expires_at IS NOT NULL AND g.expires_at > clock_timestamp()
                  AND c.status IN ('open', 'triaged', 'assigned')
                FOR SHARE OF g, c"""),
        {"id": grant_id, "tenant": tenant_id,
         "issuer": identity.issuer, "subject": identity.subject}).mappings().one_or_none()
    if row is None or row["expires_at"] <= datetime.now(timezone.utc):
        return None
    return dict(row)


def _mark_expired(connection, *, grant_id: str) -> None:
    """Flip a past-due approved grant to expired (idempotent)."""
    connection.execute(
        text("""UPDATE support_access_grants SET status='expired',
                       version=version+1, updated_at=now()
                WHERE id=:id AND status='approved' AND expires_at <= clock_timestamp()"""),
        {"id": grant_id})


class CaseIn(BaseModel):
    tenant_id: uuid.UUID
    title: str = Field(min_length=1, max_length=160)
    severity: str = Field(default="medium", pattern="^(low|medium|high|urgent)$")
    request_id: uuid.UUID | None = None
    appointment_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    communication_id: uuid.UUID | None = None
    trace_id: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=4000)


class CaseStatusIn(BaseModel):
    status: str = Field(pattern="^(triaged|assigned|resolved|closed)$")
    assignee_staff_id: uuid.UUID | None = None
    notes: str | None = Field(default=None, max_length=4000)


class GrantIn(BaseModel):
    scope: str = Field(default="diagnostic", pattern="^(diagnostic|billing)$")
    justification: str = Field(min_length=1, max_length=500)


def register_support_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    require_company_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    @app.post("/v1/company/cases", tags=["company"], status_code=201)
    def open_case(payload: CaseIn,
                  identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.support.manage")
                name = connection.execute(
                    text("SELECT company_tenant_name(:t)"),
                    {"t": str(payload.tenant_id)}).scalar_one_or_none()
                if name is None:
                    raise HTTPException(status_code=404, detail={"code": "tenant_not_found"})
                if payload.appointment_id or payload.communication_id or payload.job_id:
                    connection.execute(
                        text("SELECT set_config('app.tenant_id', :t, true)"),
                        {"t": str(payload.tenant_id)})
                    for table, value in (
                        ("appointments", payload.appointment_id),
                        ("communications", payload.communication_id),
                        ("tenant_jobs", payload.job_id),
                    ):
                        if value is not None:
                            found = connection.execute(
                                text(f"SELECT 1 FROM {table} WHERE tenant_id=:tenant AND id=:id"),
                                {"tenant": str(payload.tenant_id), "id": str(value)},
                            ).scalar_one_or_none()
                            if found is None:
                                raise HTTPException(status_code=422, detail={
                                    "code": "support_reference_unknown",
                                    "message": "correlation reference is not available for this salon"})
                row = connection.execute(
                    text("""INSERT INTO support_cases
                            (tenant_id, title, severity, request_id, appointment_id,
                             job_id, communication_id, trace_id, internal_notes,
                             opened_by_issuer, opened_by_subject)
                            VALUES (:tenant, :title, :severity, :request_id, :appt,
                                    :job_id, :comm, :trace, :notes, :issuer, :subject)
                            RETURNING id::text AS id, status"""),
                    {"tenant": str(payload.tenant_id), "title": payload.title.strip(),
                     "request_id": str(payload.request_id) if payload.request_id else None,
                     "severity": payload.severity, "appt": str(payload.appointment_id)
                     if payload.appointment_id else None,
                     "job_id": str(payload.job_id) if payload.job_id else None,
                     "comm": str(payload.communication_id) if payload.communication_id else None,
                     "trace": payload.trace_id,
                     "notes": redact_note(payload.notes or ""),
                     "issuer": identity.issuer, "subject": identity.subject},
                ).mappings().one()
                _company_audit(connection, identity=identity, action="company.case_opened",
                               resource_type="support_case", resource_id=row["id"],
                               reason=payload.title.strip())
            return dict(row)
        finally:
            engine.dispose()

    @app.get("/v1/company/cases", tags=["company"])
    def list_cases(status: str = "open",
                   identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.support.manage")
                if status not in CASE_STATUSES + ("all",):
                    raise HTTPException(status_code=422, detail={"code": "invalid_status"})
                items = _as_json(connection.execute(
                    text("SELECT company_case_list()")).scalar_one()) or []
                if status != "all":
                    items = [i for i in items if i["status"] == status]
                if items:
                    case_ids = [uuid.UUID(i["id"]) for i in items]
                    grant_rows = connection.execute(
                        text("""SELECT g.id::text AS id, g.case_id::text AS case_id,
                                       g.scope,
                                       CASE WHEN g.status='approved'
                                                  AND g.expires_at <= clock_timestamp()
                                            THEN 'expired' ELSE g.status END AS status,
                                       g.justification, g.requested_by_subject,
                                       g.created_at, g.approved_at, g.expires_at,
                                       (SELECT count(*) FROM support_grant_uses u
                                         WHERE u.grant_id=g.id) AS uses
                                FROM support_access_grants g
                                WHERE g.case_id = ANY(CAST(:ids AS uuid[]))
                                ORDER BY g.created_at DESC"""),
                        {"ids": case_ids}).mappings().all()
                    by_case: dict[str, list[dict]] = {}
                    for grant in grant_rows:
                        grant = dict(grant)
                        grant["created_at"] = grant["created_at"].isoformat()
                        grant["approved_at"] = (grant["approved_at"].isoformat()
                                                if grant["approved_at"] else None)
                        grant["expires_at"] = (grant["expires_at"].isoformat()
                                               if grant["expires_at"] else None)
                        by_case.setdefault(grant["case_id"], []).append(grant)
                    for item in items:
                        item["grants"] = by_case.get(item["id"], [])
            for item in items:
                item.pop("internal_notes", None)
            return {"items": items}
        finally:
            engine.dispose()

    @app.post("/v1/company/cases/{case_id}/status", tags=["company"])
    def move_case(case_id: uuid.UUID, payload: CaseStatusIn,
                  identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.support.manage")
                if payload.assignee_staff_id is not None:
                    staff = connection.execute(
                        text("""SELECT 1 FROM company_staff
                                WHERE id=:id AND is_active"""),
                        {"id": str(payload.assignee_staff_id)}).scalar_one_or_none()
                    if staff is None:
                        raise HTTPException(status_code=422, detail={"code": "assignee_unknown"})
                row = connection.execute(
                    text("""UPDATE support_cases
                            SET status=:status,
                                assignee_staff_id=COALESCE(:assignee, assignee_staff_id),
                                internal_notes=CASE WHEN :notes <> ''
                                                     THEN internal_notes || chr(10) || :notes
                                                     ELSE internal_notes END,
                                version=version+1, updated_at=now()
                            WHERE id=:id AND status <> 'closed'
                            RETURNING id::text AS id, status"""),
                    {"id": str(case_id), "status": payload.status,
                     "assignee": str(payload.assignee_staff_id)
                     if payload.assignee_staff_id else None,
                     "notes": redact_note(payload.notes or "")}).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404,
                                        detail={"code": "case_not_found_or_closed"})
                _company_audit(connection, identity=identity, action="company.case_moved",
                               resource_type="support_case", resource_id=row["id"],
                               reason=payload.status)
            return dict(row)
        finally:
            engine.dispose()

    @app.post("/v1/company/cases/{case_id}/grants", tags=["company"], status_code=201)
    def request_grant(case_id: uuid.UUID, payload: GrantIn,
                      identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity, lock=True)
                _require_company(role, "company.support.manage")
                case = connection.execute(
                    text("""SELECT id::text AS id, tenant_id::text AS tenant_id, status
                            FROM support_cases WHERE id=:id"""),
                    {"id": str(case_id)}).mappings().one_or_none()
                if case is None or case["status"] in ("resolved", "closed"):
                    raise HTTPException(status_code=422, detail={
                        "code": "grant_case_ineligible",
                        "message": "grants attach to working cases only"})
                row = connection.execute(
                    text("""INSERT INTO support_access_grants
                            (case_id, tenant_id, scope, requested_by_issuer,
                             requested_by_subject, justification)
                            VALUES (:case, :tenant, :scope, :issuer, :subject, :just)
                            RETURNING id::text AS id, status"""),
                    {"case": str(case_id), "tenant": case["tenant_id"],
                     "scope": payload.scope, "issuer": identity.issuer,
                     "subject": identity.subject,
                     "just": payload.justification.strip()}).mappings().one()
                _company_audit(connection, identity=identity, action="company.grant_requested",
                               resource_type="support_access_grant", resource_id=row["id"],
                               reason=f"case {case_id} scope {payload.scope}")
            return {**dict(row), "tenant_id": case["tenant_id"]}
        finally:
            engine.dispose()

    @app.get("/v1/company/grants/{grant_id}", tags=["company"])
    def read_grant(grant_id: uuid.UUID,
                   identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.support.manage")
                row = connection.execute(
                    text("""SELECT g.id::text AS id, g.tenant_id::text AS tenant_id,
                                   g.case_id::text AS case_id, g.scope, g.status,
                                   g.justification, g.approved_at, g.expires_at,
                                   (SELECT count(*) FROM support_grant_uses u
                                     WHERE u.grant_id=g.id) AS uses
                            FROM support_access_grants g WHERE g.id=:id"""),
                    {"id": str(grant_id)}).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={"code": "grant_not_found"})
                out = dict(row)
                out["approved_at"] = out["approved_at"].isoformat() if out["approved_at"] else None
                out["expires_at"] = out["expires_at"].isoformat() if out["expires_at"] else None
            return out
        finally:
            engine.dispose()

    @app.post("/v1/company/grants/{grant_id}/revoke", tags=["company"])
    def revoke_grant(grant_id: uuid.UUID,
                     identity=Depends(require_company_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity)
                _require_company(role, "company.support.manage")
                row = connection.execute(
                    text("""UPDATE support_access_grants SET status='revoked',
                                   version=version+1, updated_at=now()
                            WHERE id=:id AND status='approved'
                            RETURNING id::text AS id, status"""),
                    {"id": str(grant_id)}).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404,
                                        detail={"code": "grant_not_active"})
                _company_audit(connection, identity=identity, action="company.grant_revoked",
                               resource_type="support_access_grant", resource_id=row["id"],
                               reason="revoked by company")
            return dict(row)
        finally:
            engine.dispose()

    @app.get("/v1/company/tenants/{tenant_id}/diagnostics", tags=["company"])
    def read_diagnostics(tenant_id: uuid.UUID, grant_id: uuid.UUID,
                         identity=Depends(require_company_identity)) -> dict:
        """Masked diagnostic snapshot under an active grant; every read logged."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role = company_scope(connection, identity, lock=True)
                _require_company(role, "company.support.manage")
                grant = _grant_valid(connection, grant_id=str(grant_id),
                                     tenant_id=str(tenant_id), identity=identity)
                if grant is not None:
                    snapshot = _as_json(connection.execute(
                        text("SELECT company_tenant_diagnostics(:t)"),
                        {"t": str(tenant_id)}).scalar_one_or_none())
                    if snapshot is None:
                        raise HTTPException(status_code=404, detail={"code": "tenant_not_found"})
                    connection.execute(
                        text("""INSERT INTO support_grant_uses
                                (grant_id, tenant_id, actor_issuer, actor_subject, action)
                                VALUES (:grant, :tenant, :issuer, :subject, 'diagnostics_read')"""),
                        {"grant": str(grant_id), "tenant": str(tenant_id),
                         "issuer": identity.issuer, "subject": identity.subject})
            if grant is None:
                # Release any locks before persisting expiry. A grant may have
                # expired while waiting for its row lock; upgrading a held share
                # lock in another connection would deadlock against this read.
                with engine.begin() as outward:
                    _mark_expired(outward, grant_id=str(grant_id))
                raise HTTPException(status_code=403, detail={
                    "code": "grant_not_authorized",
                    "message": "no active grant covers this read"})
            return {"grant_id": str(grant_id), "snapshot": snapshot}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/support-access", tags=["company"])
    def list_tenant_grants(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        """What the tenant sees: grant requests, states and every logged use."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "team.manage")
                grants = connection.execute(
                    text("""SELECT g.id::text AS id, g.case_id::text AS case_id,
                                   g.scope, g.status, g.justification,
                                   g.approved_at, g.expires_at, g.created_at,
                                   (SELECT count(*) FROM support_grant_uses u
                                     WHERE u.grant_id=g.id) AS uses
                            FROM support_access_grants g
                            WHERE g.tenant_id=:tenant ORDER BY g.created_at DESC"""),
                    {"tenant": str(tenant_id)}).mappings().all()
                uses = connection.execute(
                    text("""SELECT u.grant_id::text AS grant_id, u.actor_subject,
                                   u.action, u.created_at
                            FROM support_grant_uses u WHERE u.tenant_id=:tenant
                            ORDER BY u.created_at DESC LIMIT 100"""),
                    {"tenant": str(tenant_id)}).mappings().all()
            return {"grants": [{**dict(g),
                                "approved_at": g["approved_at"].isoformat() if g["approved_at"] else None,
                                "expires_at": g["expires_at"].isoformat() if g["expires_at"] else None,
                                "created_at": g["created_at"].isoformat()} for g in grants],
                    "uses": [{**dict(u), "created_at": u["created_at"].isoformat()} for u in uses]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/support-access/{grant_id}/approve", tags=["company"])
    def approve_grant(tenant_id: uuid.UUID, grant_id: uuid.UUID,
                      request: Request, identity=Depends(require_identity)) -> dict:
        """Tenant approval: thirty minutes from this call, then it expires."""
        from datetime import timedelta as _td
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, user_id, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "team.manage")
                row = connection.execute(
                    text("""UPDATE support_access_grants
                            SET status='approved', approved_by_issuer=:issuer,
                                approved_by_subject=:subject,
                                approved_at=now(),
                                expires_at=now() + (:minutes || ' minutes')::interval,
                                version=version+1, updated_at=now()
                            WHERE id=:id AND tenant_id=:tenant AND status='requested'
                            RETURNING id::text AS id, status, expires_at"""),
                    {"id": str(grant_id), "tenant": str(tenant_id),
                     "issuer": identity.issuer, "subject": identity.subject,
                     "minutes": str(GRANT_MINUTES)}).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={
                        "code": "grant_not_pending",
                        "message": "only a requested grant can be approved"})
                insert_audit(connection, tenant_id, identity, action="support.grant_approved",
                             resource_type="support_access_grant", resource_id=row["id"],
                             reason="tenant approved thirty-minute diagnostic access",
                             request_id=request.state.request_id,
                             changed_fields=["status"])
            return {"id": row["id"], "status": row["status"],
                    "expires_at": row["expires_at"].isoformat()}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/support-access/{grant_id}/reject", tags=["company"])
    def reject_grant(tenant_id: uuid.UUID, grant_id: uuid.UUID,
                     request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "team.manage")
                row = connection.execute(
                    text("""UPDATE support_access_grants SET status='rejected',
                                   version=version+1, updated_at=now()
                            WHERE id=:id AND tenant_id=:tenant AND status='requested'
                            RETURNING id::text AS id, status"""),
                    {"id": str(grant_id), "tenant": str(tenant_id)}).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={
                        "code": "grant_not_pending",
                        "message": "only a requested grant can be rejected"})
                insert_audit(connection, tenant_id, identity, action="support.grant_rejected",
                             resource_type="support_access_grant", resource_id=row["id"],
                             reason="tenant declined access",
                             request_id=request.state.request_id,
                             changed_fields=["status"])
            return dict(row)
        finally:
            engine.dispose()
