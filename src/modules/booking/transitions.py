"""Appointment lifecycle transitions and completion visits (FR-031/FR-034; TASK-024).

The appointment is a small, explicit state machine (`architecture.md:68`):

    pending_approval -> confirmed | rejected | cancelled
    confirmed        -> checked_in | cancelled | no_show
    checked_in       -> in_service | cancelled
    in_service       -> completed

Every legal move is audited and version-checked; an illegal jump is a 409, never a silent
write. Terminal moves release the reservation (cancelled/rejected/no_show), and `completed`
materialises a structured `visits` + `visit_services` row — the fact rebooking and
analytics read, snapshotted so a later catalogue edit cannot rewrite what was delivered.
A correction out of a terminal state is a compensating transition that requires a reason.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

from modules.booking.calendar import _command_receipt, _save_receipt
from modules.communications.outbox import record_appointment_email_update
from modules.retention import create_cycles_for_visit
from salon_platform.authorization import require_assigned_location

# Allowed target statuses from each source status (architecture.md:68). A status not
# listed has no outgoing edge; reaching a terminal state and leaving it is a correction.
ALLOWED: dict[str, frozenset[str]] = {
    "pending_approval": frozenset({"confirmed", "rejected", "cancelled"}),
    "confirmed": frozenset({"checked_in", "cancelled", "no_show"}),
    "checked_in": frozenset({"in_service", "cancelled"}),
    "in_service": frozenset({"completed"}),
}
TERMINAL = frozenset({"completed", "cancelled", "no_show", "rejected"})
RELEASING = frozenset({"cancelled", "rejected", "no_show"})


class TransitionIn(BaseModel):
    to_status: Literal["pending_approval", "confirmed", "checked_in", "in_service", "completed", "cancelled", "no_show", "rejected"]
    expected_version: int
    reason: str | None = Field(default=None, max_length=240)
    idempotency_key: str = Field(min_length=8, max_length=64)


def register_transition_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    @app.post("/v1/tenants/{tenant_id}/appointments/{appointment_id}/transitions", tags=["booking"])
    def transition_appointment(tenant_id: uuid.UUID, appointment_id: uuid.UUID, payload: TransitionIn,
                               request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                facts = {"appointment_id": str(appointment_id),
                         **payload.model_dump(mode="json", exclude={"idempotency_key"})}
                fingerprint, replay = _command_receipt(
                    connection, tenant_id, identity, payload.idempotency_key, "transition", facts)
                appt = connection.execute(
                    text("SELECT status, version, location_id FROM appointments WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                    {"tenant": str(tenant_id), "id": str(appointment_id)}).mappings().one_or_none()
                if appt is None:
                    raise HTTPException(status_code=404, detail={"code": "appointment_not_found"})
                require_assigned_location(role, assigned, appt["location_id"])
                if replay is not None:
                    return replay
                if appt["version"] != payload.expected_version:
                    raise HTTPException(status_code=409, detail={"code": "appointment_version_conflict"})
                current = appt["status"]
                reason = payload.reason.strip() if payload.reason else None
                if payload.to_status in RELEASING and not reason:
                    raise HTTPException(status_code=422, detail={"code": "appointment_reason_required"})
                if payload.to_status == current:
                    # Same-target retries are harmless but still bind the key to these
                    # exact facts so it cannot be reused for a different command.
                    response = {"appointment_id": str(appointment_id), "status": current,
                                "visit_id": None, "replayed": True}
                    _save_receipt(connection, tenant_id, payload.idempotency_key, fingerprint, response)
                    return response
                legal = payload.to_status in ALLOWED.get(current, frozenset())
                if not legal and current not in TERMINAL:
                    raise HTTPException(status_code=409, detail={"code": "illegal_transition"})
                if current in TERMINAL and not legal:
                    # A correction out of a terminal state needs an explicit reason
                    # (FR-031: complete/no-show correction requires reason and permission).
                    if not reason:
                        raise HTTPException(status_code=422, detail={"code": "correction_reason_required"})
                    if role != "owner":
                        raise HTTPException(status_code=403, detail={"code": "owner_correction_required"})
                connection.execute(
                    text("UPDATE appointments SET status=:to, version=version+1, updated_at=now() "
                         "WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(appointment_id), "to": payload.to_status})
                if payload.to_status in RELEASING:
                    connection.execute(
                        text("UPDATE reservations SET state='released' "
                             "WHERE tenant_id=:tenant AND appointment_id=:id AND state='active'"),
                        {"tenant": str(tenant_id), "id": str(appointment_id)})
                if current == "completed" and payload.to_status != "completed":
                    # A completion correction invalidates the visit fact. Its
                    # visit_services and rebooking cycles cascade with it.
                    connection.execute(
                        text("DELETE FROM visits WHERE tenant_id=:tenant AND appointment_id=:id"),
                        {"tenant": str(tenant_id), "id": str(appointment_id)})
                connection.execute(
                    text("""INSERT INTO appointment_transitions
                            (tenant_id,id,appointment_id,from_status,to_status,actor_issuer,actor_subject,reason)
                            VALUES (:tenant,:id,:appointment,:from,:to,:issuer,:subject,:reason)"""),
                    {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "appointment": str(appointment_id),
                     "from": current, "to": payload.to_status, "issuer": identity.issuer,
                     "subject": identity.subject, "reason": reason})
                visit_id = None
                if payload.to_status == "completed":
                    visit_id = _complete_visit(connection, tenant_id, appointment_id)
                insert_audit(connection, tenant_id, identity, action=f"appointment.{payload.to_status}",
                             resource_type="appointment", resource_id=appointment_id,
                             reason=reason or payload.to_status, request_id=request.state.request_id,
                             changed_fields=["status"])
                if payload.to_status in {"cancelled", "rejected"}:
                    record_appointment_email_update(
                        connection, tenant_id=str(tenant_id), appointment_id=str(appointment_id),
                        purpose="booking_cancelled", trace_id=request.state.trace_id)
                response = {"appointment_id": str(appointment_id), "status": payload.to_status,
                            "visit_id": visit_id, "replayed": False}
                _save_receipt(connection, tenant_id, payload.idempotency_key, fingerprint, response)
            return response
        finally:
            engine.dispose()


def _complete_visit(connection, tenant_id: str, appointment_id: str) -> str:
    """Materialise the completed-service fact for an appointment, idempotently."""
    existing = connection.execute(
        text("SELECT id::text AS id FROM visits WHERE tenant_id=:tenant AND appointment_id=:appt"),
        {"tenant": tenant_id, "appt": appointment_id}).scalar_one_or_none()
    if existing is not None:
        return existing
    appt = connection.execute(
        text("SELECT client_id, staff_id, location_id FROM appointments WHERE tenant_id=:tenant AND id=:id"),
        {"tenant": tenant_id, "id": appointment_id}).mappings().one()
    visit_id = str(uuid.uuid4())
    connection.execute(
        text("""INSERT INTO visits (tenant_id,id,appointment_id,client_id,staff_id,location_id)
                VALUES (:tenant,:id,:appt,:client,:staff,:location)"""),
        {"tenant": tenant_id, "id": visit_id, "appt": appointment_id,
         "client": str(appt["client_id"]), "staff": str(appt["staff_id"]),
         "location": str(appt["location_id"])})
    lines = connection.execute(
        text("""SELECT id::text AS id, sequence, service_id, service_version, service_name, staff_id,
                       price_minor, currency, duration_minutes, pre_buffer_minutes, post_buffer_minutes,
                       starts_at, ends_at
                FROM appointment_services WHERE tenant_id=:tenant AND appointment_id=:appt ORDER BY sequence"""),
        {"tenant": tenant_id, "appt": appointment_id}).mappings().all()
    for line in lines:
        connection.execute(
            text("""INSERT INTO visit_services
                    (tenant_id,id,visit_id,appointment_service_id,sequence,service_id,service_version,
                     service_name,staff_id,price_minor,currency,duration_minutes,pre_buffer_minutes,
                     post_buffer_minutes,starts_at,ends_at)
                    VALUES (:tenant,:id,:visit,:appt_service,:sequence,:service,:version,:name,:staff,
                            :price,:currency,:duration,:pre,:post,:starts,:ends)"""),
            {"tenant": tenant_id, "id": str(uuid.uuid4()), "visit": visit_id,
             "appt_service": line["id"], "sequence": line["sequence"], "service": str(line["service_id"]),
             "version": line["service_version"], "name": line["service_name"], "staff": str(line["staff_id"]),
             "price": line["price_minor"], "currency": line["currency"], "duration": line["duration_minutes"],
             "pre": line["pre_buffer_minutes"], "post": line["post_buffer_minutes"],
             "starts": line["starts_at"].isoformat(), "ends": line["ends_at"].isoformat()})
    # TASK-035 / FR-049: the same transaction opens the rebooking due cycle(s)
    # for these completed lines, so a committed completion always has its due
    # date and a rolled-back one never leaves an orphan cycle.
    create_cycles_for_visit(connection, tenant_id=tenant_id, visit_id=visit_id)
    return visit_id

