"""Manual salon receipt ledger (FR-036; TASK-025).

A *recorded* receipt is money the salon logged at checkout — cash, card, an adjustment —
**not** verified card settlement and **not** SaaS subscription revenue. The ledger is
append-only: a refund or adjustment is a new compensating entry, never an edit of a posted
one, so what was recorded and when is never rewritten. Every total is reported as
`recorded`, never `settled` (AC-2: recorded receipts are labelled separately from booked
value and from our own subscription revenue).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from salon_platform.authorization import require_assigned_location

METHODS = ("cash", "card", "other")
KINDS = ("payment", "refund", "adjustment")


class ReceiptIn(BaseModel):
    kind: str
    amount_minor: int = Field(strict=True, ge=-2147483648, le=2147483647)
    method: str = "cash"
    external_reference: str | None = Field(default=None, max_length=120)
    note: str | None = Field(default=None, max_length=500)
    idempotency_key: str = Field(min_length=8, max_length=64)


def register_receipt_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    def _appointment_client(connection, tenant_id: str, appointment_id: str,
                            role: str, assigned: list[uuid.UUID]) -> str:
        row = connection.execute(
            text("SELECT client_id::text AS client_id, location_id FROM appointments WHERE tenant_id=:tenant AND id=:id"),
            {"tenant": tenant_id, "id": appointment_id}).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "appointment_not_found"})
        require_assigned_location(role, assigned, row["location_id"])
        return row["client_id"]

    @app.post("/v1/tenants/{tenant_id}/appointments/{appointment_id}/receipt-entries",
              tags=["booking"], status_code=201)
    def post_receipt(tenant_id: uuid.UUID, appointment_id: uuid.UUID, payload: ReceiptIn,
                     request: Request, identity=Depends(require_identity)) -> dict:
        if payload.kind not in KINDS:
            raise HTTPException(status_code=422, detail={"code": "invalid_kind"})
        if payload.method not in METHODS:
            raise HTTPException(status_code=422, detail={"code": "invalid_method"})
        if payload.kind == "payment" and payload.amount_minor <= 0:
            raise HTTPException(status_code=422, detail={"code": "payment_must_be_positive"})
        if payload.amount_minor == 0:
            raise HTTPException(status_code=422, detail={"code": "amount_must_be_nonzero"})
        if payload.kind == "refund" and payload.amount_minor >= 0:
            raise HTTPException(status_code=422, detail={"code": "refund_must_be_negative"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                client_id = _appointment_client(connection, str(tenant_id), str(appointment_id), role, assigned)
                facts = {"appointment_id": str(appointment_id), "kind": payload.kind,
                         "amount_minor": payload.amount_minor, "method": payload.method,
                         "external_reference": payload.external_reference, "note": payload.note}
                # The lock precedes the replay read so concurrent retries see the winner.
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:tenant), hashtext(:key))"),
                    {"tenant": str(tenant_id), "key": "receipt:" + payload.idempotency_key})
                existing = connection.execute(
                    text("""SELECT id::text AS id, appointment_id::text AS appointment_id,
                                   kind, amount_minor, method, external_reference, note
                            FROM salon_receipt_entries
                            WHERE tenant_id=:tenant AND idempotency_key=:key"""),
                    {"tenant": str(tenant_id), "key": payload.idempotency_key}).mappings().one_or_none()
                if existing is not None:
                    if any(existing[field] != value for field, value in facts.items()):
                        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
                    return {"entry_id": existing["id"], "replayed": True}
                entry_id = uuid.uuid4()
                connection.execute(
                    text("""INSERT INTO salon_receipt_entries
                            (tenant_id,id,appointment_id,client_id,amount_minor,currency,method,kind,
                             external_reference,note,idempotency_key)
                            VALUES (:tenant,:id,:appointment,:client,:amount,'CAD',:method,:kind,
                                    :reference,:note,:key)"""),
                    {"tenant": str(tenant_id), "id": str(entry_id), "appointment": str(appointment_id),
                     "client": client_id, "amount": payload.amount_minor, "method": payload.method,
                     "kind": payload.kind, "reference": payload.external_reference,
                     "note": payload.note, "key": payload.idempotency_key})
                insert_audit(connection, tenant_id, identity, action="receipt.recorded",
                             resource_type="receipt_entry", resource_id=entry_id,
                             reason=f"{payload.kind} {payload.amount_minor}",
                             request_id=request.state.request_id,
                             changed_fields=["amount_minor", "kind"])
            return {"entry_id": str(entry_id), "replayed": False}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/appointments/{appointment_id}/receipt-entries", tags=["booking"])
    def list_receipts(tenant_id: uuid.UUID, appointment_id: uuid.UUID, request: Request,
                      identity=Depends(require_identity), limit: int = 100, offset: int = 0) -> dict:
        engine = app.state.engine_factory()
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail={"code": "invalid_limit"})
        if offset < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_offset"})
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.read")
                _appointment_client(connection, str(tenant_id), str(appointment_id), role, assigned)
                rows = connection.execute(
                    text("""SELECT id::text AS id, kind, amount_minor, currency, method,
                                   external_reference, note, posted_at
                            FROM salon_receipt_entries
                            WHERE tenant_id=:tenant AND appointment_id=:appointment
                            ORDER BY posted_at, id LIMIT :limit OFFSET :offset"""),
                    {"tenant": str(tenant_id), "appointment": str(appointment_id),
                     "limit": limit, "offset": offset}).mappings().all()
                total = connection.execute(
                    text("SELECT count(*) FROM salon_receipt_entries WHERE tenant_id=:tenant AND appointment_id=:appointment"),
                    {"tenant": str(tenant_id), "appointment": str(appointment_id)}).scalar_one()
            items = [{**dict(r), "posted_at": r["posted_at"].isoformat()} for r in rows]
            return {"items": items, "total": total, "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/appointments/{appointment_id}/receipt-total", tags=["booking"])
    def receipt_total(tenant_id: uuid.UUID, appointment_id: uuid.UUID, request: Request,
                      identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.read")
                _appointment_client(connection, str(tenant_id), str(appointment_id), role, assigned)
                recorded = connection.execute(
                    text("SELECT COALESCE(sum(amount_minor),0) FROM salon_receipt_entries "
                         "WHERE tenant_id=:tenant AND appointment_id=:appointment"),
                    {"tenant": str(tenant_id), "appointment": str(appointment_id)}).scalar_one()
            # Explicitly a recorded total, never a settled one (FR-036, AC-2).
            return {"appointment_id": str(appointment_id), "recorded_total_minor": int(recorded),
                    "currency": "CAD", "status": "recorded"}
        finally:
            engine.dispose()

