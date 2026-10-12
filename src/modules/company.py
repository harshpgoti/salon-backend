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

import json
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

__all__ = [
    "TRIAL_DAYS",
    "SyntheticStripeAdapter",
    "rebuild_entitlements",
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


WEBHOOK_EVENTS = (
    "checkout.session.completed", "invoice.paid", "invoice.payment_failed",
    "customer.subscription.updated", "charge.refunded", "dispute.created",
    "payment.requires_action",
)

# Events that move money/state vs. events that only record evidence.
STATEFUL_EVENTS = frozenset({
    "checkout.session.completed", "invoice.paid", "invoice.payment_failed",
    "customer.subscription.updated",
})

# Provider subscription states mapped onto our lifecycle (FR-067). Unknown
# values never move state — they are recorded and reported.
PROVIDER_STATUS_MAP = {
    "trialing": "trial", "active": "active", "past_due": "past_due",
    "canceled": "cancelled", "incomplete": "past_due",
}


def rebuild_entitlements(connection, *, tenant_id: str) -> dict:
    """Rebuild the entitlement projection from the current subscription row.

    Idempotent: same subscription always yields the same projection, so
    out-of-order redelivery converges instead of drifting. Marketing is
    allowed on trial/active (and pre-end cancelled); every other status
    pauses it — TASK-048 refines grace/restricted behavior on top.
    """
    sub = connection.execute(
        text("""SELECT s.status, p.code, p.limits FROM tenant_subscriptions s
                JOIN billing_plans p ON p.id=s.plan_id
                WHERE s.tenant_id=:tenant"""),
        {"tenant": tenant_id}).mappings().one_or_none()
    if sub is None:
        return {}
    limits = sub["limits"] or {}
    marketing = sub["status"] in ("trial", "active", "cancelled")
    connection.execute(
        text("""INSERT INTO tenant_entitlements
                (tenant_id, plan_code, status, max_locations, max_staff,
                 included_sms_segments, included_emails, marketing_allowed)
                VALUES (:tenant, :code, :status, :locs, :staff, :sms, :emails, :mkt)
                ON CONFLICT (tenant_id) DO UPDATE SET
                    plan_code=EXCLUDED.plan_code, status=EXCLUDED.status,
                    max_locations=EXCLUDED.max_locations, max_staff=EXCLUDED.max_staff,
                    included_sms_segments=EXCLUDED.included_sms_segments,
                    included_emails=EXCLUDED.included_emails,
                    marketing_allowed=EXCLUDED.marketing_allowed,
                    updated_at=now()"""),
        {"tenant": tenant_id, "code": sub["code"], "status": sub["status"],
         "locs": int(limits.get("max_locations", 1)),
         "staff": int(limits.get("max_staff", 5)),
         "sms": int(limits.get("included_sms_segments", 0)),
         "emails": int(limits.get("included_emails", 0)), "mkt": marketing})
    return {"tenant_id": tenant_id, "plan_code": sub["code"],
            "status": sub["status"], "marketing_allowed": marketing,
            "max_locations": int(limits.get("max_locations", 1)),
            "max_staff": int(limits.get("max_staff", 5)),
            "included_sms_segments": int(limits.get("included_sms_segments", 0)),
            "included_emails": int(limits.get("included_emails", 0))}


def ingest_billing_webhook(
    connection, *,
    raw_body: bytes,
    signature: str,
    event_type: str,
    provider_event_id: str,
    occurred_at: datetime | None,
    data: dict,
) -> dict:
    """Verify, dedupe and apply one provider billing event (FR-067).

    Returns applied / duplicate / stale (older than the last applied event).
    Unknown customers and unknown event types are rejected, never stored:
    the provider retries, and nothing is invented. Only signed, deduplicated
    events ever move subscription state — the browser success page cannot.
    """
    import hashlib
    import hmac
    if event_type not in WEBHOOK_EVENTS:
        raise CompanyModuleError(f"unknown billing event {event_type!r}")
    if not provider_event_id:
        raise CompanyModuleError("provider event id is required")
    secret = connection.execute(
        text("SELECT secret FROM billing_provider_secrets WHERE provider='stripe_test'"),
    ).scalar_one_or_none()
    if secret is None or not hmac.compare_digest(
            hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest(),
            (signature or "").strip().lower()):
        raise CompanyModuleError("billing webhook signature mismatch")
    customer_ref = (data or {}).get("customer_ref")
    tenant_hint = (data or {}).get("tenant_id")
    mapped = connection.execute(
        text("SELECT tenant_id::text AS tenant_id FROM billing_customer_map WHERE customer_ref=:c"),
        {"c": customer_ref}).scalar_one_or_none() if customer_ref else None
    if mapped is not None and tenant_hint is not None and mapped != tenant_hint:
        raise CompanyModuleError("customer reference does not belong to this tenant")
    # The first event (checkout completion) predates the customer map: the
    # provider echoes the checkout metadata tenant, authenticated by the
    # HMAC above, and the scoped session lookup below proves it.
    tenant_id = mapped or tenant_hint
    if tenant_id is None:
        raise CompanyModuleError("unknown customer reference")
    connection.execute(
        text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
    moment = occurred_at or datetime.now(timezone.utc)
    try:
        connection.execute(
            text("""INSERT INTO billing_events
                    (tenant_id, id, provider_event_id, event_type, occurred_at, payload)
                    VALUES (:tenant, :id, :peid, :type, :at, :payload)"""),
            {"tenant": tenant_id, "id": str(uuid.uuid4()), "peid": provider_event_id,
             "type": event_type, "at": moment.isoformat(),
             "payload": json.dumps(data or {})},
        )
    except IntegrityError:
        return {"status": "duplicate", "tenant_id": tenant_id}
    if event_type not in STATEFUL_EVENTS:
        # Evidence only: refunds, disputes and pending-authentication change
        # no state here; finance and recovery workflows read the trail.
        rebuild_entitlements(connection, tenant_id=tenant_id)
        return {"status": "applied", "tenant_id": tenant_id, "state_changed": False}
    last = connection.execute(
        text("""SELECT last_provider_event_at FROM tenant_subscriptions
                WHERE tenant_id=:tenant"""),
        {"tenant": tenant_id}).scalar_one_or_none()
    if last is not None and moment < last.replace(tzinfo=timezone.utc):
        return {"status": "stale", "tenant_id": tenant_id}
    _apply_stateful_event(connection, tenant_id=tenant_id, event_type=event_type,
                          data=data or {}, moment=moment)
    rebuild_entitlements(connection, tenant_id=tenant_id)
    return {"status": "applied", "tenant_id": tenant_id, "state_changed": True}


def _apply_stateful_event(connection, *, tenant_id: str, event_type: str,
                          data: dict, moment: datetime) -> None:
    """Move subscription state for signed, deduplicated, in-order events."""
    if event_type == "checkout.session.completed":
        plan = connection.execute(
            text("""SELECT id FROM billing_plans WHERE code=:code AND is_active
                    ORDER BY version DESC LIMIT 1"""),
            {"code": data.get("plan_code", "growth")}).scalar_one_or_none()
        if plan is None:
            raise CompanyModuleError("checkout references an unknown plan")
        # The echoed tenant is proven: its own checkout session must exist.
        session_row = connection.execute(
            text("""SELECT id FROM billing_checkout_sessions
                    WHERE tenant_id=:tenant AND provider_session_ref=:ref"""),
            {"tenant": tenant_id, "ref": data.get("session_ref")}).scalar_one_or_none()
        if session_row is None:
            raise CompanyModuleError("checkout session unknown to this tenant")
        connection.execute(
            text("""UPDATE tenant_subscriptions
                    SET plan_id=:plan, status='active',
                        stripe_customer_ref=:customer,
                        stripe_subscription_ref=:sub,
                        current_period_end=:period,
                        cancel_at_period_end=false,
                        last_provider_event_at=:at,
                        version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant"""),
            {"tenant": tenant_id, "plan": str(plan),
             "customer": data.get("customer_ref"), "sub": data.get("subscription_ref"),
             "period": data.get("period_end"), "at": moment.isoformat()})
        connection.execute(
            text("""INSERT INTO billing_customer_map (customer_ref, tenant_id)
                    VALUES (:c, :t) ON CONFLICT (customer_ref) DO NOTHING"""),
            {"c": data.get("customer_ref"), "t": tenant_id})
        connection.execute(
            text("""UPDATE billing_checkout_sessions SET status='complete'
                    WHERE tenant_id=:tenant AND provider_session_ref=:ref"""),
            {"tenant": tenant_id, "ref": data.get("session_ref")})
    elif event_type == "invoice.paid":
        connection.execute(
            text("""UPDATE tenant_subscriptions
                    SET status='active', current_period_end=:period,
                        last_provider_event_at=:at,
                        version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant"""),
            {"tenant": tenant_id, "period": data.get("period_end"),
             "at": moment.isoformat()})
    elif event_type == "invoice.payment_failed":
        connection.execute(
            text("""UPDATE tenant_subscriptions
                    SET status='past_due', last_provider_event_at=:at,
                        version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant AND status NOT IN ('cancelled', 'suspended')"""),
            {"tenant": tenant_id, "at": moment.isoformat()})
    elif event_type == "customer.subscription.updated":
        mapped = PROVIDER_STATUS_MAP.get(data.get("provider_status", ""))
        if mapped is None:
            return
        connection.execute(
            text("""UPDATE tenant_subscriptions
                    SET status=:status, current_period_end=COALESCE(:period, current_period_end),
                        cancel_at_period_end=COALESCE(:cancel_at, cancel_at_period_end),
                        last_provider_event_at=:at,
                        version=version+1, updated_at=now()
                    WHERE tenant_id=:tenant"""),
            {"tenant": tenant_id, "status": mapped, "period": data.get("period_end"),
             "cancel_at": data.get("cancel_at_period_end"), "at": moment.isoformat()})


class WebhookIn(BaseModel):
    id: str = Field(min_length=1, max_length=120)
    type: str
    occurred_at: datetime | None = None
    data: dict = Field(default_factory=dict)


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

    @app.post("/v1/billing/webhooks/stripe", tags=["company"])
    async def receive_billing_webhook(request: Request) -> dict:
        """Provider billing webhook. Authenticates by HMAC signature only."""
        raw_body = await request.body()
        try:
            payload = WebhookIn.model_validate_json(raw_body)
        except Exception:
            raise HTTPException(status_code=422, detail={"code": "unreadable_webhook"})
        signature = request.headers.get("x-provider-signature", "")
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                try:
                    result = ingest_billing_webhook(
                        connection, raw_body=raw_body, signature=signature,
                        event_type=payload.type, provider_event_id=payload.id,
                        occurred_at=payload.occurred_at, data=payload.data)
                except CompanyModuleError as exc:
                    message = str(exc)
                    raise HTTPException(
                        status_code=401 if "signature" in message else 422,
                        detail={"code": "webhook_rejected", "message": message})
            return result
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/billing/entitlements", tags=["company"])
    def read_entitlements(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "team.manage")
                entitlements = rebuild_entitlements(connection, tenant_id=str(tenant_id))
                if not entitlements:
                    raise HTTPException(status_code=404, detail={"code": "entitlements_not_found"})
            return entitlements
        finally:
            engine.dispose()
