"""Atomic, idempotent holds and booking commits (FR-029, TASK-020).

The guarantee this module must provide is narrow and absolute: for one staff member at one
moment, **exactly one** confirmation succeeds and every other attempt is told the slot is
unavailable. Two mechanisms combine to make that true:

- The `reservations` EXCLUDE constraint (migration 020) makes overlapping active
  reservations for the same staff member or resource a database-level impossibility, so two
  concurrent transactions cannot both commit even if they both passed every application check.
- A hold is a reservation row of its own, so a hold and a confirmation compete for the same
  range through the same constraint. A hold that loses raises an integrity error, which is
  translated into a named "slot unavailable", never a 500.

A hold carries **no personal detail** (FR-029): the caller gets a random token, the database
stores only its hash, and the owner is identified by producing the token again. An expired
hold is swept by state transition, not by `NOW()` inside an index predicate.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, StrictBool, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from modules.company import assert_bookings_allowed
from modules.crm.main import normalize_email, normalize_phone
from modules.communications.outbox import record_appointment_email_update
from modules.catalogue.scheduling_policy import (
    plan_processing_phases,
    required_resource_units,
    validate_staff_slot,
)

# Proposed hold lifetime (FR-029). Short enough that an abandoned hold frees the slot soon.
HOLD_MINUTES = 5


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class HoldRequest(BaseModel):
    service_id: uuid.UUID
    staff_id: uuid.UUID
    starts_at: str = Field(min_length=16, max_length=40)
    idempotency_key: str = Field(min_length=8, max_length=64)


class HoldTokenIn(BaseModel):
    owner_token: str = Field(min_length=16, max_length=128)


class ConfirmRequest(BaseModel):
    model_config = {"extra": "forbid"}

    owner_token: str = Field(min_length=16, max_length=128)
    client_name: str = Field(min_length=1, max_length=120)
    email: str | None = Field(default=None, min_length=3, max_length=320)
    phone: str | None = Field(default=None, min_length=3, max_length=40)
    country: str = Field(default="CA", pattern="^[A-Za-z]{2}$")
    policy_accepted: StrictBool
    policy_version: int = Field(ge=1)
    idempotency_key: str = Field(min_length=8, max_length=64)
    # Optional booking-time intent to hear marketing. It is echoed back as an intent only;
    # it grants no consent and changes no eligibility (FR-040 / QA-15 - booking never opts
    # a client in). The default is False, so omitting it is the ordinary case.
    marketing_opt_in: StrictBool = False
    # Tracked rebooking/offer token from a prompt link (FR-052). Matching
    # closes the attributed cycle; unknown tokens change nothing.
    attribution_token: str | None = Field(default=None, max_length=64)

    @field_validator("client_name", "email", "phone", mode="before")
    @classmethod
    def trim_public_contact(cls, value):
        if value is None:
            return None
        if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("invalid contact value")
        return value.strip()

    @field_validator("country")
    @classmethod
    def normalize_country(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def require_reachable_contact(self):
        if not self.client_name.strip() or not (self.email or self.phone):
            raise ValueError("name and at least one contact method are required")
        return self


def management_token_for(owner_token: str, tenant_id: str, appointment_id: str) -> str:
    """Derive a replay-stable bearer without storing plaintext; the hold token has 192 bits of entropy."""
    message = f"salon-management-v1:{tenant_id}:{appointment_id}:{owner_token}".encode("utf-8")
    return hashlib.sha256(message).hexdigest()


def register_hold_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
    resolve_public_location: Callable,
) -> None:
    def _parse_instant(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail={"code": "invalid_start_time"})
        if parsed.tzinfo is None:
            raise HTTPException(status_code=422, detail={"code": "start_time_requires_timezone"})
        return parsed

    def _service_and_staff(connection, tenant_id: str, service_id: uuid.UUID, staff_id: uuid.UUID):
        service = connection.execute(
            text("""SELECT id::text AS id, name, duration_minutes, pre_buffer_minutes,
                           post_buffer_minutes, price_minor, currency, version, requires_resource
                    FROM services WHERE tenant_id=:tenant AND id=:id AND archived_at IS NULL"""),
            {"tenant": tenant_id, "id": str(service_id)}).mappings().one_or_none()
        if service is None:
            raise HTTPException(status_code=404, detail={"code": "availability_not_found"})
        qualified = connection.execute(
            text("""SELECT 1 FROM staff_service_qualifications
                    WHERE tenant_id=:tenant AND staff_id=:staff AND service_id=:service"""),
            {"tenant": tenant_id, "staff": str(staff_id), "service": str(service_id)}).scalar_one_or_none()
        if qualified is None:
            raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
        return service

    def _sweep_expired(connection, tenant_id: str) -> None:
        """Expire stale holds by state transition, so the EXCLUDE predicate frees them."""
        connection.execute(
            text("""UPDATE reservations SET state='expired'
                    WHERE tenant_id=:tenant AND hold_id IN (
                        SELECT id FROM booking_holds
                        WHERE tenant_id=:tenant AND state='active' AND expires_at <= now())
                      AND state='active'"""),
            {"tenant": tenant_id})
        connection.execute(
            text("""UPDATE booking_holds SET state='expired'
                    WHERE tenant_id=:tenant AND state='active' AND expires_at <= now()"""),
            {"tenant": tenant_id})

    def _sync_hold_resource_reservations(connection, *, tenant_id: str, hold_id: str,
                                         resource_units: list[tuple[str, int]], occupied_from: datetime,
                                         occupied_to: datetime) -> None:
        """Make the hold's resource reservations match the locked current policy atomically."""
        current = connection.execute(
            text("""SELECT resource_id::text, resource_unit FROM reservations
                    WHERE tenant_id=:tenant AND hold_id=:hold AND resource_id IS NOT NULL"""),
            {"tenant": tenant_id, "hold": hold_id},
        ).all()
        current_units = sorted((row[0], row[1]) for row in current)
        if current_units != sorted(resource_units):
            connection.execute(
                text("""DELETE FROM reservations
                        WHERE tenant_id=:tenant AND hold_id=:hold AND resource_id IS NOT NULL"""),
                {"tenant": tenant_id, "hold": hold_id},
            )
            for resource_id, unit in resource_units:
                connection.execute(
                    text("""INSERT INTO reservations
                            (tenant_id,id,hold_id,resource_id,resource_unit,occupied_range,state)
                            VALUES (:tenant,:id,:hold,:resource,:unit,
                                    tstzrange(:from_ts,:to_ts,'[)'),'active')"""),
                    {"tenant": tenant_id, "id": str(uuid.uuid4()), "hold": hold_id,
                     "resource": resource_id, "unit": unit, "from_ts": occupied_from.isoformat(),
                     "to_ts": occupied_to.isoformat()},
                )
        else:
            connection.execute(
                text("""UPDATE reservations SET state='active'
                        WHERE tenant_id=:tenant AND hold_id=:hold AND resource_id IS NOT NULL"""),
                {"tenant": tenant_id, "hold": hold_id},
            )

    def _sync_hold_staff_reservations(connection, *, tenant_id: str, hold_id: str,
                                      staff_id: str, phases: list[dict] | None,
                                      occupied_from: datetime, occupied_to: datetime) -> None:
        connection.execute(text("DELETE FROM reservations WHERE tenant_id=:tenant AND hold_id=:hold AND staff_id IS NOT NULL"),
                           {"tenant": tenant_id, "hold": hold_id})
        assignments = ([(phase["sequence"], staff, phase["occupied_from"], phase["occupied_to"])
                        for phase in phases for staff in phase["staff_ids"]]
                       if phases is not None else [(None, staff_id, occupied_from.isoformat(), occupied_to.isoformat())])
        for sequence, staff, start, end in assignments:
            connection.execute(text("""INSERT INTO reservations
                (tenant_id,id,hold_id,staff_id,phase_sequence,occupied_range,state)
                VALUES (:tenant,:id,:hold,:staff,:sequence,tstzrange(:starts,:ends,'[)'),'active')"""),
                {"tenant": tenant_id, "id": str(uuid.uuid4()), "hold": hold_id,
                 "staff": staff, "sequence": sequence, "starts": start, "ends": end})

    @app.post("/v1/public/salons/{booking_slug}/holds", tags=["booking"], status_code=201)
    def create_hold(booking_slug: str, payload: HoldRequest) -> dict:
        """Place a five-minute hold. No personal detail is stored (FR-029)."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                location = resolve_public_location(connection, booking_slug)
                tenant_id = location["tenant_id"]
                _sweep_expired(connection, tenant_id)

                service = _service_and_staff(connection, tenant_id, payload.service_id, payload.staff_id)
                starts_at = _parse_instant(payload.starts_at)
                zone = ZoneInfo(location["timezone"])
                # The client sent an absolute instant; the offer is only valid if it is a
                # real local slot, so re-derive what a slot would have produced.
                pre = service["pre_buffer_minutes"]
                post = service["post_buffer_minutes"]
                duration = service["duration_minutes"]
                service_start = starts_at
                service_end = starts_at + timedelta(minutes=duration)
                occupied_from = starts_at - timedelta(minutes=pre)
                occupied_to = starts_at + timedelta(minutes=duration + post)
                if service_start < datetime.now(tz=dt_timezone.utc):
                    raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})

                # Serialize concurrent creates on one key: the advisory lock is taken
                # BEFORE the replay SELECT, so racers queue here instead of racing past
                # a missed SELECT into competing INSERTs. Namespaced to this tenant
                # (tenants never share idempotency keys); xact scope releases on COMMIT.
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:tenant), hashtext(:key))"),
                    {"tenant": tenant_id, "key": payload.idempotency_key})
                # Idempotency: the same key replays the same hold rather than a second
                # one. Re-read under the lock: the winner may have committed while we
                # queued. FOR UPDATE serializes any later writers on the same row.
                existing = connection.execute(
                    text("""SELECT id::text AS id, staff_id::text AS staff_id,
                                   service_id::text AS service_id, starts_at, ends_at, state,
                                   appointment_id::text AS appointment_id, processing_phases
                            FROM booking_holds
                            WHERE tenant_id=:tenant AND idempotency_key=:key FOR UPDATE"""),
                    {"tenant": tenant_id, "key": payload.idempotency_key}).mappings().one_or_none()
                if existing is not None:
                    if (existing["service_id"] != str(payload.service_id)
                            or existing["staff_id"] != str(payload.staff_id)
                            or existing["starts_at"] != starts_at):
                        # Same key, different booking: a caller bug, never a silent swap.
                        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
                    if existing["state"] == "converted":
                        # Already booked: a receipt lookup, nothing is reactivated.
                        return {"hold_id": existing["id"], "replayed": True,
                                "state": "converted", "appointment_id": existing["appointment_id"],
                                "expires_in_seconds": 0}
                    phase_plan = plan_processing_phases(
                        connection, tenant_id=tenant_id, service_id=str(payload.service_id),
                        location_id=location["id"], starts_at=starts_at,
                        preferred_staff_id=str(payload.staff_id), service_duration=duration,
                        pre_buffer_minutes=pre, post_buffer_minutes=post,
                        exclude_hold_id=existing["id"])
                    if phase_plan is None:
                        validate_staff_slot(connection, tenant_id=tenant_id, staff_id=str(payload.staff_id),
                                            service_id=str(payload.service_id), location_id=location["id"],
                                            starts_at=starts_at, duration_minutes=duration,
                                            pre_buffer_minutes=pre, post_buffer_minutes=post)
                    if existing["state"] == "expired":
                        # Same key retried after expiry: refresh this row as a new hold
                        # attempt on the same slot. The reactivation below carries the
                        # EXCLUDE guard, so a slot taken meanwhile is a 409, never a double.
                        resource_units = required_resource_units(
                            connection, tenant_id=tenant_id, service_id=str(payload.service_id),
                            location_id=str(location["id"]),
                            service_requires_resource=bool(service["requires_resource"]),
                            occupied_from=occupied_from, occupied_to=occupied_to,
                            exclude_hold_id=existing["id"])
                        token = secrets.token_urlsafe(24)
                        connection.execute(
                            text("""UPDATE booking_holds SET owner_token_hash=:hash,
                                        expires_at=:expires, state='active'
                                    WHERE tenant_id=:tenant AND id=:id"""),
                            {"hash": hash_token(token),
                             "expires": datetime.now(tz=dt_timezone.utc) + timedelta(minutes=HOLD_MINUTES),
                             "tenant": tenant_id, "id": existing["id"]})
                        try:
                            _sync_hold_staff_reservations(
                                connection, tenant_id=tenant_id, hold_id=existing["id"],
                                staff_id=str(payload.staff_id), phases=phase_plan,
                                occupied_from=occupied_from, occupied_to=occupied_to)
                            connection.execute(text("""UPDATE booking_holds SET processing_phases=CAST(:phases AS jsonb)
                                WHERE tenant_id=:tenant AND id=:id"""),
                                {"phases": json.dumps(phase_plan or []), "tenant": tenant_id, "id": existing["id"]})
                            _sync_hold_resource_reservations(
                                connection, tenant_id=tenant_id, hold_id=existing["id"],
                                resource_units=resource_units, occupied_from=occupied_from,
                                occupied_to=occupied_to)
                        except IntegrityError:
                            raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
                        return {"hold_id": existing["id"], "owner_token": token, "replayed": True,
                                "expires_in_seconds": HOLD_MINUTES * 60}
                    resource_units = required_resource_units(
                        connection, tenant_id=tenant_id, service_id=str(payload.service_id),
                        location_id=str(location["id"]),
                        service_requires_resource=bool(service["requires_resource"]),
                        occupied_from=occupied_from, occupied_to=occupied_to,
                        exclude_hold_id=existing["id"])
                    token = secrets.token_urlsafe(24)
                    connection.execute(
                        text("""UPDATE booking_holds SET owner_token_hash=:hash, expires_at=:expires,
                                state='active' WHERE tenant_id=:tenant AND id=:id"""),
                        {"hash": hash_token(token),
                         "expires": datetime.now(tz=dt_timezone.utc) + timedelta(minutes=HOLD_MINUTES),
                         "tenant": tenant_id, "id": existing["id"]})
                    try:
                        _sync_hold_staff_reservations(
                            connection, tenant_id=tenant_id, hold_id=existing["id"],
                            staff_id=str(payload.staff_id), phases=phase_plan,
                            occupied_from=occupied_from, occupied_to=occupied_to)
                        connection.execute(text("""UPDATE booking_holds SET processing_phases=CAST(:phases AS jsonb)
                            WHERE tenant_id=:tenant AND id=:id"""),
                            {"phases": json.dumps(phase_plan or []), "tenant": tenant_id, "id": existing["id"]})
                        _sync_hold_resource_reservations(
                            connection, tenant_id=tenant_id, hold_id=existing["id"],
                            resource_units=resource_units, occupied_from=occupied_from,
                            occupied_to=occupied_to)
                    except IntegrityError:
                        raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
                    return {"hold_id": existing["id"], "owner_token": token, "replayed": True,
                            "expires_in_seconds": HOLD_MINUTES * 60}

                phase_plan = plan_processing_phases(
                    connection, tenant_id=tenant_id, service_id=str(payload.service_id),
                    location_id=location["id"], starts_at=starts_at,
                    preferred_staff_id=str(payload.staff_id), service_duration=duration,
                    pre_buffer_minutes=pre, post_buffer_minutes=post)
                if phase_plan is None:
                    validate_staff_slot(connection, tenant_id=tenant_id, staff_id=str(payload.staff_id),
                                        service_id=str(payload.service_id), location_id=location["id"],
                                        starts_at=starts_at, duration_minutes=duration,
                                        pre_buffer_minutes=pre, post_buffer_minutes=post)
                resource_units = required_resource_units(
                    connection, tenant_id=tenant_id, service_id=str(payload.service_id),
                    location_id=str(location["id"]),
                    service_requires_resource=bool(service["requires_resource"]),
                    occupied_from=occupied_from, occupied_to=occupied_to)
                hold_id = uuid.uuid4()
                token = secrets.token_urlsafe(24)
                expires_at = datetime.now(tz=dt_timezone.utc) + timedelta(minutes=HOLD_MINUTES)
                # The advisory lock above already serialized racers on this key and the
                # re-read handled the queued winner, so reaching here means this caller
                # owns the key: plain INSERT, no second replay lookup.
                connection.execute(
                    text("""INSERT INTO booking_holds
                            (tenant_id,id,location_id,staff_id,service_id,starts_at,ends_at,occupied_range,
                             local_timezone,owner_token_hash,idempotency_key,expires_at,processing_phases)
                            VALUES (:tenant,:id,:location,:staff,:service,:starts,:ends,
                                    tstzrange(:occ_from,:occ_to,'[)'),:tz,:hash,:key,:expires,CAST(:phases AS jsonb))"""),
                    {"tenant": tenant_id, "id": str(hold_id), "location": location["id"],
                     "staff": str(payload.staff_id), "service": str(payload.service_id),
                     "starts": service_start.isoformat(), "ends": service_end.isoformat(),
                     "occ_from": occupied_from.isoformat(), "occ_to": occupied_to.isoformat(),
                     "tz": location["timezone"], "hash": hash_token(token),
                     "key": payload.idempotency_key, "expires": expires_at.isoformat(),
                     "phases": json.dumps(phase_plan or [])})
                try:
                    _sync_hold_staff_reservations(
                        connection, tenant_id=tenant_id, hold_id=str(hold_id),
                        staff_id=str(payload.staff_id), phases=phase_plan,
                        occupied_from=occupied_from, occupied_to=occupied_to)
                    _sync_hold_resource_reservations(
                        connection, tenant_id=tenant_id, hold_id=str(hold_id),
                        resource_units=resource_units, occupied_from=occupied_from, occupied_to=occupied_to)
                except IntegrityError:
                    # The EXCLUDE constraint rejected the range: someone else holds it.
                    # This is the one-winner guarantee, surfaced as a named conflict.
                    raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
            return {"hold_id": str(hold_id), "owner_token": token, "replayed": False,
                    "expires_in_seconds": HOLD_MINUTES * 60}
        finally:
            engine.dispose()

    @app.get("/v1/public/salons/{booking_slug}/holds/{hold_id}", tags=["booking"])
    def read_hold(booking_slug: str, hold_id: uuid.UUID, owner_token: str) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                location = resolve_public_location(connection, booking_slug)
                tenant_id = location["tenant_id"]
                _sweep_expired(connection, tenant_id)
                hold = connection.execute(
                    text("""SELECT id::text AS id, state, starts_at, ends_at, expires_at,
                                   staff_id::text AS staff_id, service_id::text AS service_id
                            FROM booking_holds WHERE tenant_id=:tenant AND id=:id
                              AND owner_token_hash=:hash"""),
                    {"tenant": tenant_id, "id": str(hold_id), "hash": hash_token(owner_token)}).mappings().one_or_none()
                if hold is None:
                    # A wrong token and a missing hold are indistinguishable (FR-028).
                    raise HTTPException(status_code=404, detail={"code": "hold_not_found"})
            return {**dict(hold), "starts_at": hold["starts_at"].isoformat(),
                    "ends_at": hold["ends_at"].isoformat(),
                    "expires_at": hold["expires_at"].isoformat()}
        finally:
            engine.dispose()

    @app.post("/v1/public/salons/{booking_slug}/holds/{hold_id}/confirm", tags=["booking"])
    def confirm_hold(booking_slug: str, hold_id: uuid.UUID, payload: ConfirmRequest, request: Request) -> dict:
        """Convert a hold into a confirmed appointment, atomically."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                location = resolve_public_location(connection, booking_slug)
                tenant_id = location["tenant_id"]
                # FR-068: restricted/suspended salons take no new bookings
                # (reschedules and cancellations of existing ones stay open).
                assert_bookings_allowed(connection, tenant_id=tenant_id)
                _sweep_expired(connection, tenant_id)

                hold = connection.execute(
                    text("""SELECT * FROM booking_holds WHERE tenant_id=:tenant AND id=:id FOR UPDATE"""),
                    {"tenant": tenant_id, "id": str(hold_id)}).mappings().one_or_none()
                if hold is None or hold["owner_token_hash"] != hash_token(payload.owner_token):
                    raise HTTPException(status_code=404, detail={"code": "hold_not_found"})
                if hold["state"] == "converted":
                    # Idempotent replay: the hold records what it became, so the receipt is
                    # a lookup rather than an inference.
                    if hold["appointment_id"] is None:
                        raise HTTPException(status_code=409, detail={"code": "hold_not_confirmable"})
                    token = management_token_for(payload.owner_token, tenant_id,
                                                str(hold["appointment_id"]))
                    return {"appointment_id": str(hold["appointment_id"]),
                            "management_token": token, "replayed": True,
                            "marketing_opt_in_recorded": False}
                if hold["state"] != "active":
                    raise HTTPException(status_code=409, detail={"code": "hold_expired"})

                policy = connection.execute(
                    text("SELECT settings, booking_policy_version FROM tenants WHERE id=:tenant FOR SHARE"),
                    {"tenant": tenant_id}).mappings().one_or_none()
                if policy is None:
                    raise HTTPException(status_code=404, detail={"code": "salon_not_found"})
                if not payload.policy_accepted:
                    raise HTTPException(status_code=422, detail={"code": "booking_policy_required"})
                if payload.policy_version != policy["booking_policy_version"]:
                    raise HTTPException(status_code=409, detail={"code": "booking_policy_changed"})

                try:
                    email = normalize_email(payload.email) if payload.email else None
                    phone = normalize_phone(payload.phone, payload.country) if payload.phone else None
                except ValueError as exc:
                    raise HTTPException(status_code=422,
                                        detail={"code": "contact_not_normalizable"}) from exc
                client_id = uuid.uuid4()
                connection.execute(
                    text("""INSERT INTO clients (tenant_id,id,name,locale,lifecycle,
                            preferred_location_id,source)
                            VALUES (:tenant,:id,:name,'en-CA','active',:location,'public_booking')"""),
                    {"tenant": tenant_id, "id": str(client_id), "name": payload.client_name,
                     "location": str(hold["location_id"])})
                contacts = [("email", email, payload.email), ("phone", phone, payload.phone)]
                for index, (kind, normalized, display) in enumerate(contacts):
                    if normalized is None:
                        continue
                    connection.execute(
                        text("""INSERT INTO client_contacts
                                (tenant_id,id,client_id,kind,normalized_value,display_value,
                                 country,is_primary,source)
                                VALUES (:tenant,:id,:client,:kind,:normalized,:display,
                                        :country,:primary,'public_booking')"""),
                        {"tenant": tenant_id, "id": str(uuid.uuid4()), "client": str(client_id),
                         "kind": kind, "normalized": normalized, "display": display,
                         "country": payload.country, "primary": index == 0 or email is None})

                service = connection.execute(
                    text("""SELECT * FROM services
                            WHERE tenant_id=:tenant AND id=:id AND archived_at IS NULL"""),
                    {"tenant": tenant_id, "id": str(hold["service_id"])}).mappings().one_or_none()
                if service is None:
                    # Archived or removed after the hold was placed: the snapshot stays on
                    # the hold, but no new appointment may confirm against it.
                    raise HTTPException(status_code=409, detail={"code": "service_no_longer_offered"})
                processing_phases = hold.get("processing_phases") or []
                current_phases = connection.execute(text("""SELECT sequence,duration_minutes,mode,staff_count
                    FROM service_processing_phases WHERE tenant_id=:tenant AND service_id=:service
                    ORDER BY sequence"""),
                    {"tenant": tenant_id, "service": str(hold["service_id"])}).mappings().all()
                held_policy = [(phase["sequence"], phase["duration_minutes"], phase["mode"], phase["staff_count"])
                               for phase in processing_phases]
                configured_policy = [(phase["sequence"], phase["duration_minutes"], phase["mode"], phase["staff_count"])
                                     for phase in current_phases]
                if held_policy != configured_policy:
                    raise HTTPException(status_code=409, detail={"code": "service_policy_changed"})
                if processing_phases:
                    for phase in processing_phases:
                        phase_start = datetime.fromisoformat(phase["starts_at"])
                        phase_end = datetime.fromisoformat(phase["ends_at"])
                        occupied_start = datetime.fromisoformat(phase["occupied_from"])
                        occupied_end = datetime.fromisoformat(phase["occupied_to"])
                        for staff in phase["staff_ids"]:
                            validate_staff_slot(
                                connection, tenant_id=tenant_id, staff_id=staff,
                                service_id=str(hold["service_id"]), location_id=location["id"],
                                starts_at=phase_start, duration_minutes=phase["duration_minutes"],
                                pre_buffer_minutes=int((phase_start-occupied_start).total_seconds()/60),
                                post_buffer_minutes=int((occupied_end-phase_end).total_seconds()/60))
                else:
                    validate_staff_slot(connection, tenant_id=tenant_id, staff_id=str(hold["staff_id"]),
                                        service_id=str(hold["service_id"]), location_id=location["id"],
                                        starts_at=hold["starts_at"], duration_minutes=service["duration_minutes"],
                                        pre_buffer_minutes=service["pre_buffer_minutes"],
                                        post_buffer_minutes=service["post_buffer_minutes"])
                resource_units = required_resource_units(
                    connection, tenant_id=tenant_id, service_id=str(hold["service_id"]),
                    location_id=str(hold["location_id"]),
                    service_requires_resource=bool(service["requires_resource"]),
                    occupied_from=hold["occupied_range"].lower,
                    occupied_to=hold["occupied_range"].upper,
                    exclude_hold_id=str(hold_id))
                try:
                    _sync_hold_resource_reservations(
                        connection, tenant_id=tenant_id, hold_id=str(hold_id),
                        resource_units=resource_units, occupied_from=hold["occupied_range"].lower,
                        occupied_to=hold["occupied_range"].upper)
                except IntegrityError:
                    raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
                clash = connection.execute(
                    text("""SELECT 1 FROM reservations
                            WHERE tenant_id=:tenant AND staff_id=:staff AND state='active'
                              AND hold_id IS DISTINCT FROM :hold
                              AND occupied_range && tstzrange(:occ_from,:occ_to,'[)')"""),
                    {"tenant": tenant_id, "staff": str(hold["staff_id"]), "hold": str(hold_id),
                     "occ_from": hold["occupied_range"].lower.isoformat(),
                     "occ_to": hold["occupied_range"].upper.isoformat()}).scalar_one_or_none() if not processing_phases else None
                if clash is not None:
                    # Another owner took the range after the hold was placed (e.g. a staff
                    # booking): the hold loses, loudly, rather than double-booking.
                    raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
                appointment_id = uuid.uuid4()
                # The client's own link to manage this booking (FR-033): an opaque bearer
                # derived from the unguessable hold owner token, scoped to this appointment,
                # with only its hash stored and expiry at the appointment end.
                management_token = management_token_for(payload.owner_token, tenant_id,
                                                        str(appointment_id))
                connection.execute(
                    text("""INSERT INTO appointments
                            (tenant_id,id,location_id,client_id,staff_id,starts_at,ends_at,local_timezone,
                             status,source,occupied_range,policy_version,management_token_hash,
                             management_token_expires_at,attribution_token)
                            VALUES (:tenant,:id,:location,:client,:staff,:starts,:ends,:tz,
                                    'confirmed','public_booking',
                                    tstzrange(:occ_from,:occ_to,'[)'),:policy_version,:mgmt_hash,:mgmt_expires,
                                    :attribution)"""),
                    {"tenant": tenant_id, "id": str(appointment_id), "location": location["id"],
                     "client": str(client_id), "staff": str(hold["staff_id"]),
                     "starts": hold["starts_at"].isoformat(), "ends": hold["ends_at"].isoformat(),
                     "tz": location["timezone"],
                     "occ_from": hold["occupied_range"].lower.isoformat(),
                     "occ_to": hold["occupied_range"].upper.isoformat(),
                     "policy_version": policy["booking_policy_version"],
                     "mgmt_hash": hash_token(management_token),
                     "mgmt_expires": hold["ends_at"].isoformat(),
                     "attribution": payload.attribution_token})
                connection.execute(
                    text("""INSERT INTO appointment_services
                            (tenant_id,id,appointment_id,sequence,service_id,service_version,service_name,
                             staff_id,price_minor,currency,duration_minutes,pre_buffer_minutes,
                             post_buffer_minutes,starts_at,ends_at,processing_phases)
                            VALUES (:tenant,:id,:appointment,1,:service,:version,:name,:staff,:price,
                                    :currency,:duration,:pre,:post,:starts,:ends,CAST(:phases AS jsonb))"""),
                    {"tenant": tenant_id, "id": str(uuid.uuid4()), "appointment": str(appointment_id),
                     "service": str(hold["service_id"]), "version": service["version"],
                     "name": service["name"], "staff": str(hold["staff_id"]),
                     "price": service["price_minor"], "currency": service["currency"],
                     "duration": service["duration_minutes"], "pre": service["pre_buffer_minutes"],
                     "post": service["post_buffer_minutes"],
                     "starts": hold["starts_at"].isoformat(), "ends": hold["ends_at"].isoformat(),
                     "phases": json.dumps(processing_phases)})
                # Move the reservation from the hold onto the appointment in one step, so
                # the range is never unguarded between the two.
                connection.execute(
                    text("""UPDATE reservations SET appointment_id=:appointment, hold_id=NULL
                            WHERE tenant_id=:tenant AND hold_id=:hold"""),
                    {"appointment": str(appointment_id), "tenant": tenant_id, "hold": str(hold_id)})
                connection.execute(
                    text("""UPDATE booking_holds SET state='converted', appointment_id=:appointment
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"appointment": str(appointment_id), "tenant": tenant_id, "id": str(hold_id)})
                connection.execute(
                    text("""INSERT INTO appointment_transitions
                            (tenant_id,id,appointment_id,from_status,to_status,actor_issuer,actor_subject,reason)
                            VALUES (:tenant,:id,:appointment,NULL,'confirmed','public','public_booking',
                                    'confirmed from hold')"""),
                    {"tenant": tenant_id, "id": str(uuid.uuid4()), "appointment": str(appointment_id)})
                insert_audit(connection, tenant_id, None, action="appointment.created",
                             resource_type="appointment", resource_id=appointment_id,
                             reason="public booking confirmed from hold",
                             request_id=request.state.request_id,
                             changed_fields=["status", "starts_at", "policy_version"])
                record_appointment_email_update(
                    connection, tenant_id=tenant_id, appointment_id=str(appointment_id),
                    purpose="booking_confirmation", trace_id=request.state.trace_id)
                # FR-052: a tracked prompt link closes its cycle atomically here.
                from modules.retention import attribute_booking
                attribute_booking(connection, tenant_id=tenant_id,
                                  attribution_token=payload.attribution_token,
                                  appointment_id=str(appointment_id))
                # FR-056: the same token attributes a campaign send in-window.
                from modules.attribution import attribute_campaign_booking
                attribute_campaign_booking(connection, tenant_id=tenant_id,
                                           attribution_token=payload.attribution_token,
                                           appointment_id=str(appointment_id))
            return {"appointment_id": str(appointment_id), "replayed": False,
                    "management_token": management_token,
                    "marketing_opt_in_recorded": False}
        finally:
            engine.dispose()

    @app.get("/v1/public/salons/{booking_slug}/manage/{appointment_id}", tags=["booking"])
    def manage_booking(booking_slug: str, appointment_id: uuid.UUID,
                       response: Response,
                       authorization: str | None = Header(default=None)) -> dict:
        """The client's own scoped view of one booking (FR-033).

        The token names exactly one appointment in one tenant and expires at that
        appointment's end. A wrong token, a missing appointment and another tenant's
        appointment are all the same 404 (FR-028) - this endpoint never reveals whether
        an appointment id exists anywhere. An expired link is a 409, distinguishable
        only to the holder of the real token.
        """
        engine = app.state.engine_factory()
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        try:
            with engine.begin() as connection:
                location = resolve_public_location(connection, booking_slug)
                tenant_id = location["tenant_id"]
                token = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
                row = connection.execute(
                    text("""SELECT a.id::text AS id,a.status,a.version,a.policy_version,
                                   a.starts_at,a.ends_at,a.local_timezone,a.management_token_expires_at,
                                   t.booking_policy_version AS current_policy_version,t.settings,
                                   s.service_name, st.display_name AS staff_name
                            FROM appointments a
                            JOIN tenants t ON t.id=a.tenant_id
                            JOIN appointment_services s ON s.tenant_id=a.tenant_id AND s.appointment_id=a.id
                            JOIN staff_profiles st ON st.tenant_id=a.tenant_id AND st.id=a.staff_id
                            WHERE a.tenant_id=:tenant AND a.id=:appt
                              AND a.management_token_hash=:hash"""),
                    {"tenant": tenant_id, "appt": str(appointment_id),
                     "hash": hash_token(token)}).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={"code": "appointment_not_found"})
                if row["management_token_expires_at"] <= datetime.now(tz=dt_timezone.utc):
                    raise HTTPException(status_code=409, detail={"code": "management_link_expired"})
            can_manage = (row["status"] in ("pending_approval", "confirmed")
                          and row["starts_at"] > datetime.now(tz=dt_timezone.utc))
            blocked_reason = None
            if can_manage and (row["policy_version"] != row["current_policy_version"]):
                can_manage, blocked_reason = False, "booking_policy_changed"
            if can_manage and str((row["settings"] or {}).get("cancellation_policy") or "").strip():
                can_manage, blocked_reason = False, "public_management_requires_salon"
            if not can_manage and blocked_reason is None:
                blocked_reason = "appointment_not_modifiable"
            # Only this booking's own safe fields leave the endpoint (FR-028).
            return {"salon": location["display_name"], "timezone": row["local_timezone"],
                    "service": row["service_name"], "staff": row["staff_name"],
                    "status": row["status"],
                    "starts_at": row["starts_at"].isoformat(),
                    "ends_at": row["ends_at"].isoformat(), "version": row["version"],
                    "can_manage": can_manage, "manage_blocked_reason": blocked_reason}
        finally:
            engine.dispose()
