"""Validated, provider-hosted customer deposits (FR-038; TASK-066).

This feature was deferred behind gate G-DEPOSIT-SCOPE and is now built with the product
owner's explicit approval. The payment provider is SYNTHETIC and in-app: there are no real
Stripe or card calls, and whether a payment succeeds, fails, or is disputed is a request
field (`simulation`) the synthetic provider honors. No card data is collected, stored, or
transmitted anywhere in this module.

A customer deposit is money the SALON collects up front to secure a booking. It is the
salon's own merchant money and lives in its own append-only deposit ledger (migration 066).
The one invariant this whole module protects (AC-1): deposit money NEVER appears in the
company SaaS ledger. Nothing here reads or writes company_finance_ledger's sources
(billing_events / finance_adjustments); the salon's deposit float is a separate, tenant
scoped, signed minor-unit ledger, and a test proves the two never mix. Refunds and releases
are compensating ledger entries (negative), never edits of a posted row, so the history of
what moved and when stays intact (AC-1/AC-2).

Two held windows, deliberately separate:
  - hold_expires_at  - the pay-by deadline for a *pending* intent (the client has not paid).
  - held_expires_at  - the window held funds stay holdable once *held* (the client has paid).
The sweep (failed booking recovery) releases a deposit once its own window lapses and, if the
booking is still confirmed, cancels it and releases the slot - exactly a booking whose deposit
never secured payment recovering its reservation.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone as dt_timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from modules.booking.availability import resolve_public_location

# Deposit state machine (mirrors the CHECK in migration 066).
DEPOSIT_STATES = ("pending", "held", "captured", "released", "disputed", "refunded")
# The synthetic provider's simulated outcomes.
SIMULATIONS = ("success", "fail", "dispute")
# Synthetic provider issuer recorded on public callback transitions.
PROVIDER_ISSUER = "https://payments.example.invalid/synthetic"


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class DepositCreateIn(BaseModel):
    amount_minor: int = Field(gt=0)
    idempotency_key: str = Field(min_length=8, max_length=64)
    hold_minutes: int = Field(default=1440, gt=0)


class DepositPayIn(BaseModel):
    pay_token: str = Field(min_length=16, max_length=128)
    simulation: str = Field(default="success")


class DepositRefundIn(BaseModel):
    idempotency_key: str = Field(min_length=8, max_length=64)
    amount_minor: int | None = Field(default=None, gt=0)


def register_deposit_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    """Register the deposit routes. The orchestrator wires this into the app factory.

    The public pay callback resolves its tenant via resolve_public_location (imported from
    modules.booking.availability), so no public-resolver argument is needed here; every
    staff-side route scopes through membership_scope + require_permission.
    """

    def _deposit_state(connection, tenant_id: str, deposit_id: str) -> dict | None:
        return connection.execute(
            text("""SELECT state, amount_minor, appointment_id::text AS appointment_id
                    FROM deposits WHERE tenant_id=:tenant AND id=:id FOR UPDATE"""),
            {"tenant": tenant_id, "id": deposit_id}).mappings().one_or_none()

    def _held_total(connection, tenant_id: str, deposit_id: str) -> int:
        """Money that has ever been held for this deposit (the positive holds only)."""
        return int(connection.execute(
            text("""SELECT COALESCE(sum(amount_minor),0) FROM deposit_ledger_entries
                    WHERE tenant_id=:tenant AND deposit_id=:dep AND kind IN ('hold','capture')"""),
            {"tenant": tenant_id, "dep": deposit_id}).scalar_one())

    def _refunded_total(connection, tenant_id: str, deposit_id: str) -> int:
        return int(connection.execute(
            text("""SELECT COALESCE(sum(amount_minor),0) FROM deposit_ledger_entries
                    WHERE tenant_id=:tenant AND deposit_id=:dep AND kind='refund'"""),
            {"tenant": tenant_id, "dep": deposit_id}).scalar_one())

    def _record_transition(connection, tenant_id: str, deposit_id: str, *, from_state,
                           to_state, issuer, subject, reason) -> None:
        """Append-only deposit state history (parallels appointment_transitions)."""
        connection.execute(
            text("""INSERT INTO deposit_transitions
                    (tenant_id,id,deposit_id,from_state,to_state,actor_issuer,actor_subject,reason)
                    VALUES (:tenant,:id,:deposit,:from,:to,:issuer,:subject,:reason)"""),
            {"tenant": tenant_id, "id": str(uuid.uuid4()), "deposit": deposit_id,
             "from": from_state, "to": to_state, "issuer": issuer, "subject": subject,
             "reason": reason})

    @app.post("/v1/tenants/{tenant_id}/appointments/{appointment_id}/deposits",
              tags=["booking"], status_code=201)
    def create_deposit(tenant_id: uuid.UUID, appointment_id: uuid.UUID, payload: DepositCreateIn,
                       request: Request, identity=Depends(require_identity)) -> dict:
        """Create a pending deposit intent and return a provider-hosted pay URL.

        The pay token is random and only its sha256 hash is stored (FR-029); the holder is
        identified by producing the token again on the public pay callback.
        """
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                appt = connection.execute(
                    text("""SELECT l.booking_slug FROM appointments a
                            JOIN locations l ON l.tenant_id=a.tenant_id AND l.id=a.location_id
                            WHERE a.tenant_id=:tenant AND a.id=:id"""),
                    {"tenant": str(tenant_id), "id": str(appointment_id)}).mappings().one_or_none()
                if appt is None:
                    raise HTTPException(status_code=404, detail={"code": "appointment_not_found"})

                # Serialize concurrent creates on one key BEFORE the replay lookup, so racers
                # queue rather than race past a missed SELECT into competing INSERTs. Tenant
                # namespaced; xact scope releases on COMMIT (same mechanism as booking holds).
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:tenant), hashtext(:key))"),
                    {"tenant": str(tenant_id), "key": payload.idempotency_key})
                existing = connection.execute(
                    text("SELECT id::text AS id FROM deposits "
                         "WHERE tenant_id=:tenant AND idempotency_key=:key FOR UPDATE"),
                    {"tenant": str(tenant_id), "key": payload.idempotency_key}).one_or_none()
                if existing is not None:
                    # Idempotent replay: the same intent. The client only stored the token's
                    # hash, so the exact token cannot be reproduced; a fresh token is minted
                    # and the hash updated on the same row - the deposit id and terms are the
                    # same durable intent (booking-holds idempotency pattern).
                    deposit_id = existing[0]
                    token = secrets.token_urlsafe(24)
                    connection.execute(
                        text("UPDATE deposits SET pay_token_hash=:hash, updated_at=now() "
                             "WHERE tenant_id=:tenant AND id=:id"),
                        {"hash": hash_token(token), "tenant": str(tenant_id), "id": deposit_id})
                    pay_url = (f"/v1/public/salons/{appt['booking_slug']}/deposits/"
                               f"{deposit_id}/pay?token={token}")
                    return {"deposit_id": deposit_id, "pay_url": pay_url, "replayed": True}

                deposit_id = str(uuid.uuid4())
                token = secrets.token_urlsafe(24)
                provider_ref = f"dep_syn_{secrets.token_hex(8)}"
                expires_at = datetime.now(tz=dt_timezone.utc) + timedelta(minutes=payload.hold_minutes)
                connection.execute(
                    text("""INSERT INTO deposits
                            (tenant_id,id,appointment_id,amount_minor,state,pay_token_hash,
                             hold_minutes,hold_expires_at,provider_ref,idempotency_key)
                            VALUES (:tenant,:id,:appointment,:amount,'pending',:hash,
                                    :minutes,:expires,:provider,:key)"""),
                    {"tenant": str(tenant_id), "id": deposit_id, "appointment": str(appointment_id),
                     "amount": payload.amount_minor, "hash": hash_token(token),
                     "minutes": payload.hold_minutes, "expires": expires_at.isoformat(),
                     "provider": provider_ref, "key": payload.idempotency_key})
                insert_audit(connection, tenant_id, identity, action="deposit.created",
                             resource_type="deposit", resource_id=deposit_id,
                             reason=f"amount {payload.amount_minor}",
                             request_id=request.state.request_id,
                             changed_fields=["amount_minor", "state"])
            pay_url = (f"/v1/public/salons/{appt['booking_slug']}/deposits/"
                       f"{deposit_id}/pay?token={token}")
            return {"deposit_id": deposit_id, "pay_url": pay_url, "replayed": False}
        finally:
            engine.dispose()

    @app.post("/v1/public/salons/{booking_slug}/deposits/{deposit_id}/pay", tags=["booking"])
    def pay_deposit(booking_slug: str, deposit_id: uuid.UUID, payload: DepositPayIn) -> dict:
        """The synthetic provider callback for a hosted deposit payment.

        success -> held + a positive `hold` entry; dispute -> disputed (hold then the
        contesting `dispute` entry, so the money nets out of clean float); fail -> released
        and retryable, with no money entry because no funds moved. Replaying the same outcome
        returns the current state; a mismatched outcome on a settled deposit is a named 409.
        """
        engine = app.state.engine_factory()
        if payload.simulation not in SIMULATIONS:
            raise HTTPException(status_code=422, detail={"code": "invalid_simulation"})
        try:
            with engine.begin() as connection:
                location = resolve_public_location(connection, booking_slug)
                tenant_id = location["tenant_id"]
                deposit = connection.execute(
                    text("""SELECT state, amount_minor, hold_minutes FROM deposits
                            WHERE tenant_id=:tenant AND id=:id AND pay_token_hash=:hash FOR UPDATE"""),
                    {"tenant": tenant_id, "id": str(deposit_id),
                     "hash": hash_token(payload.pay_token)}).mappings().one_or_none()
                if deposit is None:
                    # A wrong token and a missing deposit are indistinguishable (FR-028).
                    raise HTTPException(status_code=404, detail={"code": "deposit_not_found"})
                state = deposit["state"]

                if payload.simulation == "success":
                    if state in ("held", "captured"):
                        # Idempotent replay of a payment that already settled.
                        return {"deposit_id": str(deposit_id), "state": state, "replayed": True}
                    if state not in ("pending", "released"):
                        raise HTTPException(status_code=409, detail={"code": "deposit_not_payable"})
                    provider_ref = f"dep_syn_{secrets.token_hex(8)}"
                    connection.execute(
                        text("""INSERT INTO deposit_ledger_entries
                                (tenant_id,deposit_id,kind,amount_minor,provider_ref,note)
                                VALUES (:tenant,:deposit,'hold',:amount,:ref,'provider held deposit')"""),
                        {"tenant": tenant_id, "deposit": str(deposit_id),
                         "amount": deposit["amount_minor"], "ref": provider_ref})
                    held_expires = datetime.now(tz=dt_timezone.utc) + timedelta(minutes=deposit["hold_minutes"])
                    connection.execute(
                        text("""UPDATE deposits SET state='held', provider_ref=:ref,
                                held_expires_at = :held_expires,
                                version=version+1, updated_at=now()
                                WHERE tenant_id=:tenant AND id=:id"""),
                        {"ref": provider_ref, "tenant": tenant_id, "id": str(deposit_id),
                         "held_expires": held_expires.isoformat()})
                    _record_transition(connection, tenant_id, str(deposit_id), from_state=state,
                                       to_state="held", issuer=PROVIDER_ISSUER,
                                       subject="synthetic_provider",
                                       reason="synthetic provider held deposit")
                    return {"deposit_id": str(deposit_id), "state": "held", "replayed": False}

                if payload.simulation == "dispute":
                    if state == "disputed":
                        return {"deposit_id": str(deposit_id), "state": "disputed", "replayed": True}
                    if state not in ("pending", "released", "held"):
                        raise HTTPException(status_code=409, detail={"code": "deposit_not_payable"})
                    provider_ref = f"dep_syn_{secrets.token_hex(8)}"
                    # The provider captured the payment (hold) and then contested it
                    # (dispute); the two net out of clean float while contested.
                    connection.execute(
                        text("""INSERT INTO deposit_ledger_entries
                                (tenant_id,deposit_id,kind,amount_minor,provider_ref,note)
                                VALUES (:tenant,:deposit,'hold',:amount,:ref,'provider captured')"""),
                        {"tenant": tenant_id, "deposit": str(deposit_id),
                         "amount": deposit["amount_minor"], "ref": provider_ref})
                    connection.execute(
                        text("""INSERT INTO deposit_ledger_entries
                                (tenant_id,deposit_id,kind,amount_minor,provider_ref,note)
                                VALUES (:tenant,:deposit,'dispute',:neg,:ref,'provider dispute')"""),
                        {"tenant": tenant_id, "deposit": str(deposit_id),
                         "neg": -deposit["amount_minor"], "ref": provider_ref})
                    connection.execute(
                        text("""UPDATE deposits SET state='disputed', provider_ref=:ref,
                                held_expires_at=NULL, version=version+1, updated_at=now()
                                WHERE tenant_id=:tenant AND id=:id"""),
                        {"ref": provider_ref, "tenant": tenant_id, "id": str(deposit_id)})
                    _record_transition(connection, tenant_id, str(deposit_id), from_state=state,
                                       to_state="disputed", issuer=PROVIDER_ISSUER,
                                       subject="synthetic_provider",
                                       reason="synthetic provider disputed deposit")
                    return {"deposit_id": str(deposit_id), "state": "disputed", "replayed": False}

                # simulation == "fail"
                if state == "released":
                    return {"deposit_id": str(deposit_id), "state": "released", "replayed": True}
                if state not in ("pending",):
                    raise HTTPException(status_code=409, detail={"code": "deposit_not_payable"})
                # The payment failed, so no money moved: only the state closes. This is why a
                # failed payment stays retryable - there is no hold entry to later compensate.
                connection.execute(
                    text("""UPDATE deposits SET state='released', version=version+1,
                            updated_at=now() WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": tenant_id, "id": str(deposit_id)})
                _record_transition(connection, tenant_id, str(deposit_id), from_state=state,
                                   to_state="released", issuer=PROVIDER_ISSUER,
                                   subject="synthetic_provider", reason="synthetic provider failed payment")
                return {"deposit_id": str(deposit_id), "state": "released", "replayed": False,
                        "retryable": True}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/deposits/{deposit_id}", tags=["booking"])
    def read_deposit(tenant_id: uuid.UUID, deposit_id: uuid.UUID,
                     identity=Depends(require_identity)) -> dict:
        """Return a deposit with its append-only ledger entries."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.read")
                deposit = connection.execute(
                    text("""SELECT id::text AS id, appointment_id::text AS appointment_id,
                                    amount_minor, currency, state, hold_minutes, hold_expires_at,
                                    held_expires_at, provider_ref, version, created_at
                             FROM deposits WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(deposit_id)}).mappings().one_or_none()
                if deposit is None:
                    raise HTTPException(status_code=404, detail={"code": "deposit_not_found"})
                entries = connection.execute(
                    text("""SELECT id::text AS id, kind, amount_minor, currency, provider_ref,
                                    note, entry_at FROM deposit_ledger_entries
                            WHERE tenant_id=:tenant AND deposit_id=:dep
                            ORDER BY entry_at, id"""),
                    {"tenant": str(tenant_id), "dep": str(deposit_id)}).mappings().all()
            balance = int(sum(e["amount_minor"] for e in entries))
            body = {**dict(deposit),
                    "hold_expires_at": deposit["hold_expires_at"].isoformat(),
                    "created_at": deposit["created_at"].isoformat()}
            if deposit["held_expires_at"] is not None:
                body["held_expires_at"] = deposit["held_expires_at"].isoformat()
            body["ledger_balance_minor"] = balance
            body["entries"] = [{**dict(e), "entry_at": e["entry_at"].isoformat()} for e in entries]
            return body
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/deposits/{deposit_id}/refund", tags=["booking"])
    def refund_deposit(tenant_id: uuid.UUID, deposit_id: uuid.UUID, payload: DepositRefundIn,
                       request: Request, identity=Depends(require_identity)) -> dict:
        """Refund a held/captured deposit with a compensating ledger entry (never an edit).

        Only a deposit that has collected money can be refunded; a released/pending deposit
        has nothing to give back and is a named 409. Refunding a held deposit releases the
        remainder: on a full refund the deposit becomes 'refunded'.
        """
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:tenant), hashtext(:key))"),
                    {"tenant": str(tenant_id), "key": payload.idempotency_key})
                deposit = _deposit_state(connection, str(tenant_id), str(deposit_id))
                if deposit is None:
                    raise HTTPException(status_code=404, detail={"code": "deposit_not_found"})
                if deposit["state"] not in ("held", "captured"):
                    raise HTTPException(status_code=409, detail={"code": "deposit_not_refundable"})
                held = _held_total(connection, str(tenant_id), str(deposit_id))
                # Refund ledger entries are negative compensations. Add that signed
                # total to the positive holds so each prior refund reduces the balance.
                remaining = held + _refunded_total(connection, str(tenant_id), str(deposit_id))
                amount = payload.amount_minor if payload.amount_minor is not None else remaining
                if amount <= 0 or amount > remaining:
                    raise HTTPException(status_code=422, detail={"code": "invalid_refund_amount"})
                provider_ref = f"dep_syn_{secrets.token_hex(8)}"
                connection.execute(
                    text("""INSERT INTO deposit_ledger_entries
                            (tenant_id,deposit_id,kind,amount_minor,provider_ref,note)
                            VALUES (:tenant,:deposit,'refund',:neg,:ref,'deposit refund')"""),
                    {"tenant": str(tenant_id), "deposit": str(deposit_id),
                     "neg": -amount, "ref": provider_ref})
                new_state = "refunded" if amount >= remaining else deposit["state"]
                connection.execute(
                    text("""UPDATE deposits SET state=:state, version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"state": new_state, "tenant": str(tenant_id), "id": str(deposit_id)})
                _record_transition(connection, str(tenant_id), str(deposit_id),
                                   from_state=deposit["state"], to_state=new_state,
                                   issuer=identity.issuer, subject=identity.subject,
                                   reason="deposit refund issued")
                insert_audit(connection, tenant_id, identity, action="deposit.refunded",
                             resource_type="deposit", resource_id=deposit_id,
                             reason=f"refund {amount}",
                             request_id=request.state.request_id,
                             changed_fields=["state"])
            return {"deposit_id": str(deposit_id), "state": new_state,
                    "refunded_minor": amount, "replayed": False}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/deposits/sweep", tags=["booking"])
    def sweep_deposits(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        """Hold expiry / failed booking recovery.

        Every pending deposit past its pay window, or held deposit past its held window, is
        released. If the linked booking is still confirmed it is cancelled, its reservation is
        released (freeing the slot), and an appointment transition is recorded. A held deposit
        additionally gets a compensating negative `release` ledger entry; a never-paid deposit
        has no money to release, so it is only recorded as a state transition.
        """
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                stale = connection.execute(
                    text("""SELECT id::text AS id, state, amount_minor,
                                    appointment_id::text AS appointment_id
                             FROM deposits
                             WHERE tenant_id=:tenant
                               AND ((state='pending' AND hold_expires_at <= now())
                                    OR (state='held' AND held_expires_at <= now()))
                             ORDER BY created_at FOR UPDATE"""),
                    {"tenant": str(tenant_id)}).mappings().all()
                released = 0
                for dep in stale:
                    prior = dep["state"]
                    if dep["appointment_id"] is not None:
                        appt = connection.execute(
                            text("SELECT status FROM appointments WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                            {"tenant": str(tenant_id), "id": dep["appointment_id"]}).scalar_one_or_none()
                        if appt is not None and appt != "cancelled":
                            connection.execute(
                                text("""UPDATE appointments SET status='cancelled',
                                        cancellation_reason='deposit expired',
                                        version=version+1, updated_at=now()
                                        WHERE tenant_id=:tenant AND id=:id"""),
                                {"tenant": str(tenant_id), "id": dep["appointment_id"]})
                            connection.execute(
                                text("""UPDATE reservations SET state='released'
                                        WHERE tenant_id=:tenant AND appointment_id=:id AND state='active'"""),
                                {"tenant": str(tenant_id), "id": dep["appointment_id"]})
                            connection.execute(
                                text("""INSERT INTO appointment_transitions
                                        (tenant_id,id,appointment_id,from_status,to_status,
                                         actor_issuer,actor_subject,reason)
                                        VALUES (:tenant,:id,:appointment,:from,'cancelled',
                                                'system','deposit_sweep','deposit expired')"""),
                                {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                                 "appointment": dep["appointment_id"], "from": appt})
                    if prior == "held":
                        # Compensating entry: money that was held is released back to the customer.
                        connection.execute(
                            text("""INSERT INTO deposit_ledger_entries
                                    (tenant_id,deposit_id,kind,amount_minor,provider_ref,note)
                                    VALUES (:tenant,:deposit,'release',:neg,'dep_syn_sweep',
                                            'deposit hold expired')"""),
                            {"tenant": str(tenant_id), "deposit": dep["id"],
                             "neg": -dep["amount_minor"]})
                    connection.execute(
                        text("""UPDATE deposits SET state='released', held_expires_at=NULL,
                                version=version+1, updated_at=now()
                                WHERE tenant_id=:tenant AND id=:id"""),
                        {"tenant": str(tenant_id), "id": dep["id"]})
                    _record_transition(connection, str(tenant_id), dep["id"], from_state=prior,
                                       to_state="released", issuer="system",
                                       subject="deposit_sweep",
                                       reason="deposit hold/pay window expired")
                    released += 1
            return {"released": released}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/deposits", tags=["booking"])
    def deposit_ledger(tenant_id: uuid.UUID,
                       identity=Depends(require_identity)) -> dict:
        """The salon's separate deposit-ledger balance: sum of its entries (AC-1).

        This surface is entirely disjoint from the company SaaS ledger - it is the salon
        owner's reconcilable merchant float, queried only within its own tenant.
        """
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.read")
                balance = int(connection.execute(
                    text("SELECT COALESCE(sum(amount_minor),0) FROM deposit_ledger_entries "
                         "WHERE tenant_id=:tenant"),
                    {"tenant": str(tenant_id)}).scalar_one())
            return {"tenant_id": str(tenant_id), "balance_minor": balance, "currency": "CAD"}
        finally:
            engine.dispose()
