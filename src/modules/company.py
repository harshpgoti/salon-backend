"""Plans, trial and hosted subscription checkout (FR-065/FR-066; TASK-046).

The company is the merchant, the salon the subscriber. Prices and limits live
in `billing_plans` rows (configurable hypotheses, DQ-005) — nothing
commercial is hard-coded here.

- AC-1: plan/limits/version listing plus hosted checkout, portal and invoices
  through the synthetic Stripe test adapter (sandbox; DQ-006). No card data
  is stored anywhere — only opaque provider references.
- AC-2: the browser success page grants nothing. Checkout sessions stay
  pending until a signed provider webhook completes them (TASK-047); polling
  a session only reports its state. New tenants start on a trial subscription
  created inside the onboarding transaction.

Billing surfaces are owner-gated (the salon billing role); plan reads need
any membership so the checkout page can render.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

__all__ = [
    "TRIAL_DAYS",
    "SyntheticStripeAdapter",
    "register_company_routes",
    "start_trial",
]

# Trial length is a commercial hypothesis, not a constant of nature.
TRIAL_DAYS = 14


class CompanyModuleError(Exception):
    """Base exception for company billing operations."""


class SyntheticStripeAdapter:
    """Test-only hosted-billing stand-in (sandbox; DQ-006).

    Mints provider references and hosted URLs deterministically from ids. It
    never touches a network, never sees card data, and never grants access —
    activation happens only through signed webhooks (TASK-047).
    """

    provider = "stripe_test"

    def create_checkout_session(self, *, tenant_id: str, plan_id: str,
                                plan_code: str, amount_minor: int) -> dict:
        session_ref = f"cs_test_{uuid.uuid4().hex[:24]}"
        return {"provider": self.provider, "session_ref": session_ref,
                "url": f"https://checkout.stripe.test/pay/{session_ref}",
                "tenant_id": tenant_id, "plan_id": plan_id,
                "plan_code": plan_code, "amount_minor": amount_minor}

    def create_portal_session(self, *, customer_ref: str) -> dict:
        return {"provider": self.provider,
                "url": f"https://billing.stripe.test/portal/{customer_ref}"}

    def list_invoices(self, *, customer_ref: str, plan_name: str,
                      amount_minor: int, period_end: str | None) -> list[dict]:
        # Synthetic invoice history: the current open invoice only. Amounts
        # echo the plan row; no card or payment-method data exists here.
        return [{"id": f"in_test_{customer_ref[-8:]}", "plan": plan_name,
                 "amount_minor": amount_minor, "currency": "CAD",
                 "status": "open", "period_end": period_end}]


def start_trial(connection, *, tenant_id: str,
                now: datetime | None = None) -> dict:
    """Open the Essential trial for a new tenant (onboarding transaction)."""
    moment = now or datetime.now(timezone.utc)
    plan_id = connection.execute(
        text("""SELECT id FROM billing_plans
                WHERE code='essential' AND is_active ORDER BY version DESC LIMIT 1"""),
    ).scalar_one_or_none()
    if plan_id is None:
        raise CompanyModuleError("no active Essential plan to trial")
    connection.execute(
        text("""INSERT INTO tenant_subscriptions
                (tenant_id, plan_id, status, trial_ends_at)
                VALUES (:tenant, :plan, 'trial', :ends)
                ON CONFLICT (tenant_id) DO NOTHING"""),
        {"tenant": tenant_id, "plan": str(plan_id),
         "ends": (moment + timedelta(days=TRIAL_DAYS)).isoformat()})
    return {"tenant_id": tenant_id, "plan_id": str(plan_id), "status": "trial"}


def _subscription(connection, tenant_id: str) -> dict | None:
    row = connection.execute(
        text("""SELECT s.status, s.stripe_customer_ref, s.stripe_subscription_ref,
                       s.trial_ends_at, s.current_period_end, s.cancel_at_period_end,
                       p.id::text AS plan_id, p.code AS plan_code, p.name AS plan_name,
                       p.price_minor, p.currency, p.limits, p.version AS plan_version
                FROM tenant_subscriptions s
                JOIN billing_plans p ON p.id=s.plan_id
                WHERE s.tenant_id=:tenant"""),
        {"tenant": tenant_id}).mappings().one_or_none()
    if row is None:
        return None
    out = dict(row)
    for key in ("trial_ends_at", "current_period_end"):
        out[key] = out[key].isoformat() if out[key] else None
    return out


class CheckoutIn(BaseModel):
    plan_code: str = Field(pattern="^(essential|growth|multi_location)$")


def register_company_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    adapter = SyntheticStripeAdapter()

    @app.get("/v1/tenants/{tenant_id}/billing/plans", tags=["company"])
    def list_plans(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                membership_scope(connection, tenant_id, identity)
                rows = connection.execute(
                    text("""SELECT id::text AS id, code, version, name, price_minor,
                                   currency, limits FROM billing_plans
                            WHERE is_active ORDER BY price_minor""")).mappings().all()
            return {"items": [dict(r) for r in rows]}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/billing/subscription", tags=["company"])
    def read_subscription(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                # The salon billing role is the owner (team.manage holders).
                require_permission(role, "team.manage")
                sub = _subscription(connection, str(tenant_id))
                if sub is None:
                    raise HTTPException(status_code=404, detail={"code": "subscription_not_found"})
            return sub
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/billing/checkout", tags=["company"], status_code=201)
    def start_checkout(tenant_id: uuid.UUID, payload: CheckoutIn,
                       request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                # The salon billing role is the owner (team.manage holders).
                require_permission(role, "team.manage")
                plan = connection.execute(
                    text("""SELECT id::text AS id, code, name, price_minor FROM billing_plans
                            WHERE code=:code AND is_active ORDER BY version DESC LIMIT 1"""),
                    {"code": payload.plan_code}).mappings().one_or_none()
                if plan is None:
                    raise HTTPException(status_code=422, detail={"code": "unknown_plan"})
                session = adapter.create_checkout_session(
                    tenant_id=str(tenant_id), plan_id=plan["id"],
                    plan_code=plan["code"], amount_minor=plan["price_minor"])
                row = connection.execute(
                    text("""INSERT INTO billing_checkout_sessions
                            (tenant_id, id, plan_id, provider_session_ref, checkout_url)
                            VALUES (:tenant, :id, :plan, :ref, :url)
                            RETURNING id::text AS id, status, checkout_url"""),
                    {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                     "plan": plan["id"], "ref": session["session_ref"],
                     "url": session["url"]}).mappings().one()
                insert_audit(connection, tenant_id, identity, action="billing.checkout_started",
                             resource_type="billing_checkout_session", resource_id=row["id"],
                             reason=f"plan {plan['code']}",
                             request_id=request.state.request_id,
                             changed_fields=["plan_id", "status"])
                # The subscription row is deliberately untouched: returning
                # from the hosted page grants nothing (AC-2; TASK-047 owns it).
            return {"session_id": row["id"], "status": row["status"],
                    "checkout_url": row["checkout_url"]}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/billing/checkout/{session_id}", tags=["company"])
    def read_checkout(tenant_id: uuid.UUID, session_id: uuid.UUID,
                      identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                # The salon billing role is the owner (team.manage holders).
                require_permission(role, "team.manage")
                row = connection.execute(
                    text("""SELECT id::text AS id, status, checkout_url FROM billing_checkout_sessions
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(session_id)}).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={"code": "checkout_not_found"})
            # Read-only by construction: reporting pending grants nothing.
            return dict(row)
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/billing/portal", tags=["company"])
    def open_portal(tenant_id: uuid.UUID, request: Request,
                    identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                # The salon billing role is the owner (team.manage holders).
                require_permission(role, "team.manage")
                sub = _subscription(connection, str(tenant_id))
                if sub is None:
                    raise HTTPException(status_code=404, detail={"code": "subscription_not_found"})
                customer = sub["stripe_customer_ref"] or f"cus_test_{str(tenant_id)[:8]}"
                insert_audit(connection, tenant_id, identity, action="billing.portal_opened",
                             resource_type="tenant_subscription", resource_id=str(tenant_id),
                             reason="hosted portal session",
                             request_id=request.state.request_id,
                             changed_fields=[])
            return adapter.create_portal_session(customer_ref=customer)
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/billing/invoices", tags=["company"])
    def list_invoices(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                # The salon billing role is the owner (team.manage holders).
                require_permission(role, "team.manage")
                sub = _subscription(connection, str(tenant_id))
                if sub is None:
                    raise HTTPException(status_code=404, detail={"code": "subscription_not_found"})
                customer = sub["stripe_customer_ref"] or f"cus_test_{str(tenant_id)[:8]}"
            # Synthetic history from the plan row: amounts echo the catalogue,
            # and no payment-method data exists anywhere in this response.
            return {"items": adapter.list_invoices(
                customer_ref=customer, plan_name=sub["plan_name"],
                amount_minor=sub["price_minor"], period_end=sub["current_period_end"])}
        finally:
            engine.dispose()
