"""Waitlist entries, expiring slot offers and their atomic claim (FR-038; TASK-065).

A cancellation frees a slot. Rather than let it go to waste, a salon puts the
freed start time up for a client already waiting for that service: a *waitlist
entry* says who is waiting and when, and a *slot offer* says "this exact start
time is yours if you take it before it expires". The offer carries an owner
token (the same hash-only pattern as a hold, FR-029) that the salon hands the
client out-of-band.

The load-bearing decision is what an offer is **not**: it holds no reservation.
The freed slot stays bookable by anyone until the client claims it, and the
claim is the moment that must win. The claim locks its offer row, sweeps lapsed
offers by state transition, then inserts the appointment, its service snapshot
and its `reservations` row through the *same* EXCLUDE constraint a manual
booking or a hold confirmation uses (migration 020). So:

- two competing claims: the offer row's state decides the second one (409
  `offer_claimed`), and if anything else already took the range, the EXCLUDE
  constraint raises IntegrityError, the whole booking rolls back and the loser
  is told `slot_unavailable`. Never a 500, never a double booking.
- a claim books an appointment and nothing else. It records **no** consent row
  and triggers **no** outbound message (FR-040 / QA-15): being handed a slot a
  cancellation freed is not opting in to marketing. The claim path therefore
  has no frequency cap to bypass, because it sends nothing.

A retried claim replays the appointment its idempotency key created, so a
client who taps a link twice gets the same booking, not two.
"""

from __future__ import annotations

import secrets
import json
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from modules.booking.availability import resolve_public_location
from modules.booking.holds import hash_token
from modules.company import assert_bookings_allowed
from modules.catalogue.scheduling_policy import (
    insert_staff_reservations, plan_processing_phases, required_resource_units,
    validate_staff_slot,
)
from salon_platform.authorization import require_assigned_location

# Proposed offer lifetime (FR-038). Long enough for a client to see a message and
# act, short enough that a slot nobody reacts to returns to the bookable pool.
DEFAULT_OFFER_MINUTES = 60
MIN_OFFER_MINUTES = 5
MAX_OFFER_MINUTES = 1440

ENTRY_STATES = ("open", "withdrawn", "fulfilled")
OFFER_STATES = ("offered", "claimed", "retracted", "expired")


class WaitlistEntryIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_id: uuid.UUID
    service_id: uuid.UUID
    location_id: uuid.UUID | None = None
    # Optional preference. Null means "any qualified staff at this location",
    # which the offer resolves to one concrete staff member at offer time.
    staff_id: uuid.UUID | None = None
    date_from: date
    date_to: date


class SlotOfferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    starts_at: str = Field(min_length=16, max_length=40)
    idempotency_key: str = Field(min_length=8, max_length=64)
    expires_in_minutes: int = DEFAULT_OFFER_MINUTES


class ClaimOfferIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_token: str = Field(min_length=16, max_length=128)
    idempotency_key: str = Field(min_length=8, max_length=64)


def _parse_instant(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "invalid_start_time"})
    if parsed.tzinfo is None:
        raise HTTPException(status_code=422, detail={"code": "start_time_requires_timezone"})
    return parsed


def _sweep_expired_offers(connection, tenant_id: str) -> None:
    """Expire lapsed offers by state transition, so the next read sees the truth."""
    connection.execute(
        text("""UPDATE slot_offers SET state='expired', updated_at=now()
                WHERE tenant_id=:tenant AND state='offered' AND expires_at <= now()"""),
        {"tenant": tenant_id})


def _service_snapshot(connection, tenant_id: str, service_id: str):
    return connection.execute(
        text("""SELECT id::text AS id, name, version, duration_minutes, pre_buffer_minutes,
                       post_buffer_minutes, price_minor, currency, requires_resource
                FROM services WHERE tenant_id=:tenant AND id=:id AND archived_at IS NULL"""),
        {"tenant": tenant_id, "id": service_id}).mappings().one_or_none()


def _required_resources(connection, *, tenant_id: str, service: dict, location_id: str,
                        occupied_from: datetime, occupied_to: datetime) -> list[tuple[str, int]]:
    return required_resource_units(
        connection, tenant_id=tenant_id, service_id=service["id"], location_id=location_id,
        service_requires_resource=service["requires_resource"],
        occupied_from=occupied_from, occupied_to=occupied_to)


def register_waitlist_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    @app.post("/v1/tenants/{tenant_id}/waitlist", tags=["booking"], status_code=201)
    def create_waitlist_entry(tenant_id: uuid.UUID, payload: WaitlistEntryIn, request: Request,
                              identity=Depends(require_identity)) -> dict:
        """Add a client to the waitlist for a service inside a date window."""
        engine = app.state.engine_factory()
        if payload.date_to < payload.date_from:
            raise HTTPException(status_code=422, detail={"code": "date_range_inverted"})
        entry_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                tenant = str(tenant_id)
                client_ok = connection.execute(
                    text("SELECT 1 FROM clients WHERE tenant_id=:tenant AND id=:id AND deleted_at IS NULL"),
                    {"tenant": tenant, "id": str(payload.client_id)}).scalar_one_or_none()
                if client_ok is None:
                    raise HTTPException(status_code=404, detail={"code": "client_not_found"})
                service = _service_snapshot(connection, tenant, str(payload.service_id))
                if service is None:
                    raise HTTPException(status_code=404, detail={"code": "service_not_found"})
                location_id = str(payload.location_id) if payload.location_id else None
                if location_id is None and role != "owner":
                    if len(assigned) != 1:
                        raise HTTPException(status_code=422, detail={"code": "location_required"})
                    location_id = str(assigned[0])
                if location_id is None and payload.staff_id is not None:
                    location_id = connection.execute(
                        text("""SELECT sl.location_id::text FROM staff_locations sl
                                WHERE sl.tenant_id=:tenant AND sl.staff_id=:staff
                                ORDER BY sl.created_at LIMIT 1"""),
                        {"tenant": tenant, "staff": str(payload.staff_id)}).scalar_one_or_none()
                if location_id is None:
                    location_id = connection.execute(
                        text("""SELECT preferred_location_id::text FROM clients
                                WHERE tenant_id=:tenant AND id=:id"""),
                        {"tenant": tenant, "id": str(payload.client_id)}).scalar_one_or_none()
                if location_id is None:
                    location_id = connection.execute(
                        text("SELECT id::text FROM locations WHERE tenant_id=:tenant ORDER BY created_at LIMIT 1"),
                        {"tenant": tenant}).scalar_one_or_none()
                if location_id is None:
                    raise HTTPException(status_code=404, detail={"code": "location_not_found"})
                require_assigned_location(role, assigned, uuid.UUID(location_id))
                location = connection.execute(
                    text("SELECT timezone FROM locations WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": tenant, "id": location_id}).mappings().one_or_none()
                if location is None:
                    raise HTTPException(status_code=404, detail={"code": "location_not_found"})
                local_today = datetime.now(ZoneInfo(location["timezone"])).date()
                if (payload.date_from < local_today or payload.date_to > local_today + timedelta(days=365)
                        or (payload.date_to - payload.date_from).days > 365):
                    raise HTTPException(status_code=422, detail={"code": "date_range_out_of_range"})
                if payload.staff_id is not None:
                    qualified = connection.execute(
                        text("""SELECT 1 FROM staff_profiles s
                                JOIN staff_locations sl ON sl.tenant_id=s.tenant_id AND sl.staff_id=s.id
                                JOIN staff_service_qualifications q
                                  ON q.tenant_id=s.tenant_id AND q.staff_id=s.id AND q.service_id=:service
                                WHERE s.tenant_id=:tenant AND s.id=:staff AND s.is_bookable
                                  AND sl.location_id=:location"""),
                        {"tenant": tenant, "staff": str(payload.staff_id),
                         "service": str(payload.service_id), "location": location_id}).scalar_one_or_none()
                    if qualified is None:
                        raise HTTPException(status_code=409, detail={"code": "staff_not_qualified"})
                connection.execute(
                    text("""INSERT INTO waitlist_entries
                            (tenant_id,id,location_id,client_id,service_id,staff_id,date_from,date_to)
                            VALUES (:tenant,:id,:location,:client,:service,:staff,:from,:to)"""),
                    {"tenant": tenant, "id": str(entry_id), "location": location_id,
                     "client": str(payload.client_id), "service": str(payload.service_id),
                     "staff": str(payload.staff_id) if payload.staff_id is not None else None,
                     "from": payload.date_from.isoformat(), "to": payload.date_to.isoformat()})
                insert_audit(connection, tenant_id, identity, action="waitlist.entry_created",
                             resource_type="waitlist_entry", resource_id=entry_id,
                             reason="client added to the waitlist",
                             request_id=request.state.request_id,
                             changed_fields=["client_id", "service_id", "location_id", "staff_id", "date_from", "date_to"])
            return {"entry_id": str(entry_id), "state": "open",
                    "client_id": str(payload.client_id), "service_id": str(payload.service_id),
                    "location_id": location_id,
                    "staff_id": str(payload.staff_id) if payload.staff_id else None,
                    "date_from": payload.date_from.isoformat(),
                    "date_to": payload.date_to.isoformat()}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/waitlist", tags=["booking"])
    def list_waitlist_entries(tenant_id: uuid.UUID, identity=Depends(require_identity),
                              state: str | None = None,
                              location_id: uuid.UUID | None = None,
                              limit: int = Query(default=50, ge=1, le=100),
                              offset: int = Query(default=0, ge=0)) -> dict:
        """Bounded list of this tenant's waitlist entries, earliest window first."""
        engine = app.state.engine_factory()
        if state is not None and state not in ENTRY_STATES:
            raise HTTPException(status_code=422, detail={"code": "invalid_state"})
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                filters = ["tenant_id=:tenant"]
                params: dict = {"tenant": str(tenant_id)}
                if location_id is not None:
                    require_assigned_location(role, assigned, location_id)
                    filters.append("location_id=:location")
                    params["location"] = str(location_id)
                elif role != "owner":
                    filters.append("location_id=ANY(CAST(:locations AS uuid[]))")
                    params["locations"] = [str(value) for value in assigned]
                if state is not None:
                    filters.append("state=:state")
                    params["state"] = state
                where_sql = " AND ".join(filters)
                rows = connection.execute(
                    text(f"""SELECT id::text AS id, state, client_id::text AS client_id,
                                    service_id::text AS service_id, staff_id::text AS staff_id,
                                    location_id::text AS location_id, date_from, date_to,
                                    created_at, updated_at
                             FROM waitlist_entries WHERE {where_sql}
                             ORDER BY date_from, created_at LIMIT :limit OFFSET :offset"""),
                    {**params, "limit": limit, "offset": offset}).mappings().all()
                total = connection.execute(
                    text(f"SELECT count(*) FROM waitlist_entries WHERE {where_sql}"),
                    params).scalar_one()
            items = [{**dict(r), "date_from": r["date_from"].isoformat(),
                      "date_to": r["date_to"].isoformat(),
                      "created_at": r["created_at"].isoformat(),
                      "updated_at": r["updated_at"].isoformat()} for r in rows]
            return {"items": items, "total": total, "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/waitlist/{entry_id}/offers", tags=["booking"], status_code=201)
    def create_slot_offer(tenant_id: uuid.UUID, entry_id: uuid.UUID, payload: SlotOfferIn,
                          request: Request, identity=Depends(require_identity)) -> dict:
        """Offer one concrete start time to a waiting client, and return its token.

        The token is the bearer credential for the claim and only its hash is
        stored: the salon delivers the token out-of-band and the client proves
        possession of it. The offer does not reserve the slot, so the range stays
        bookable until the claim wins it.
        """
        engine = app.state.engine_factory()
        if payload.expires_in_minutes < MIN_OFFER_MINUTES or payload.expires_in_minutes > MAX_OFFER_MINUTES:
            raise HTTPException(status_code=422, detail={"code": "invalid_offer_expiry"})
        starts_at = _parse_instant(payload.starts_at)
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                tenant = str(tenant_id)
                assert_bookings_allowed(connection, tenant_id=tenant)
                entry = connection.execute(
                    text("""SELECT e.id::text AS id, e.state, e.location_id::text AS location_id,
                                   e.service_id::text AS service_id, e.staff_id::text AS staff_id,
                                   e.date_from, e.date_to,
                                   l.timezone AS local_timezone
                            FROM waitlist_entries e JOIN locations l
                              ON l.tenant_id=e.tenant_id AND l.id=e.location_id
                            WHERE e.tenant_id=:tenant AND e.id=:id FOR UPDATE OF e"""),
                    {"tenant": tenant, "id": str(entry_id)}).mappings().one_or_none()
                if entry is None:
                    raise HTTPException(status_code=404, detail={"code": "waitlist_entry_not_found"})
                require_assigned_location(role, assigned, uuid.UUID(entry["location_id"]))
                if entry["state"] != "open":
                    raise HTTPException(status_code=409, detail={"code": "waitlist_entry_not_open"})

                # Serialize concurrent offers on one key before the replay read, so
                # racers queue here rather than racing past a missed SELECT.
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:tenant), hashtext(:key))"),
                    {"tenant": tenant, "key": payload.idempotency_key})
                existing = connection.execute(
                    text("""SELECT id::text AS id, entry_id::text AS entry_id, starts_at, state,
                                   appointment_id::text AS appointment_id, expires_at
                            FROM slot_offers WHERE tenant_id=:tenant AND idempotency_key=:key
                            FOR UPDATE"""),
                    {"tenant": tenant, "key": payload.idempotency_key}).mappings().one_or_none()
                if existing is not None:
                    # Same key, different offer: a caller bug, never a silent swap.
                    if existing["entry_id"] != entry["id"] or existing["starts_at"] != starts_at:
                        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
                    if existing["state"] == "claimed":
                        # Already booked: a receipt lookup, nothing is re-offered.
                        return {"offer_id": existing["id"], "owner_token": None, "replayed": True,
                                "state": "claimed", "appointment_id": existing["appointment_id"],
                                "expires_at": existing["expires_at"].isoformat()}
                    if existing["state"] == "offered":
                        # Still live. The token cannot be re-derived from its hash and
                        # minting a second one would strand the link already delivered,
                        # so the replay reports the offer without a new token.
                        return {"offer_id": existing["id"], "owner_token": None, "replayed": True,
                                "state": "offered", "expires_at": existing["expires_at"].isoformat()}

                pending = connection.execute(
                    text("""SELECT 1 FROM slot_offers
                            WHERE tenant_id=:tenant AND entry_id=:entry AND state='offered'
                              AND expires_at > now() LIMIT 1"""),
                    {"tenant": tenant, "entry": entry["id"]}).scalar_one_or_none()
                if pending is not None:
                    raise HTTPException(status_code=409, detail={"code": "offer_already_pending"})

                service = _service_snapshot(connection, tenant, entry["service_id"])
                if service is None:
                    # Archived after the entry was written: no new offer may be made
                    # against a service the salon no longer sells.
                    raise HTTPException(status_code=409, detail={"code": "service_no_longer_offered"})
                duration = service["duration_minutes"]
                pre = service["pre_buffer_minutes"]
                post = service["post_buffer_minutes"]
                ends_at = starts_at + timedelta(minutes=duration)
                occupied_from = starts_at - timedelta(minutes=pre)
                occupied_to = starts_at + timedelta(minutes=duration + post)
                now = datetime.now(tz=dt_timezone.utc)
                if occupied_from < now:
                    raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})
                local_day = starts_at.astimezone(ZoneInfo(entry["local_timezone"])).date()
                if not entry["date_from"] <= local_day <= entry["date_to"]:
                    raise HTTPException(status_code=409, detail={"code": "slot_outside_waitlist_window"})

                candidates = connection.execute(
                    text("""SELECT s.id::text FROM staff_profiles s
                            JOIN staff_service_qualifications q
                              ON q.tenant_id=s.tenant_id AND q.staff_id=s.id AND q.service_id=:service
                            JOIN staff_locations sl
                              ON sl.tenant_id=s.tenant_id AND sl.staff_id=s.id AND sl.location_id=:location
                            WHERE s.tenant_id=:tenant AND s.is_bookable
                              AND (s.deactivated_on IS NULL OR s.deactivated_on>:day)
                              AND (CAST(:preferred AS uuid) IS NULL OR s.id=CAST(:preferred AS uuid))
                            ORDER BY s.display_name, s.id"""),
                    {"tenant": tenant, "service": entry["service_id"],
                     "location": entry["location_id"], "day": local_day,
                     "preferred": entry["staff_id"]}).scalars().all()
                staff_id = None
                for candidate in candidates:
                    try:
                        phase_plan = plan_processing_phases(
                            connection, tenant_id=tenant, service_id=entry["service_id"],
                            location_id=entry["location_id"], starts_at=starts_at,
                            preferred_staff_id=candidate, service_duration=duration,
                            pre_buffer_minutes=pre, post_buffer_minutes=post)
                        if phase_plan is None:
                            validate_staff_slot(
                                connection, tenant_id=tenant, staff_id=candidate,
                                service_id=entry["service_id"], location_id=entry["location_id"],
                                starts_at=starts_at, duration_minutes=duration,
                                pre_buffer_minutes=pre, post_buffer_minutes=post)
                        staff_busy = connection.execute(
                            text("""SELECT 1 FROM reservations WHERE tenant_id=:tenant
                                    AND staff_id=:staff AND state='active'
                                    AND occupied_range && tstzrange(:starts,:ends,'[)') LIMIT 1"""),
                            {"tenant": tenant, "staff": candidate,
                             "starts": occupied_from.isoformat(),
                             "ends": occupied_to.isoformat()}).scalar_one_or_none() if phase_plan is None else None
                        if staff_busy is not None:
                            continue
                        _required_resources(connection, tenant_id=tenant, service=service,
                                            location_id=entry["location_id"],
                                            occupied_from=occupied_from, occupied_to=occupied_to)
                        staff_id = candidate
                        break
                    except HTTPException:
                        continue
                if staff_id is None:
                    raise HTTPException(status_code=409, detail={"code": "slot_unavailable"})

                offer_id = existing["id"] if existing is not None else str(uuid.uuid4())
                token = secrets.token_urlsafe(24)
                expires_at = now + timedelta(minutes=payload.expires_in_minutes)
                if existing is None:
                    connection.execute(
                        text("""INSERT INTO slot_offers
                                (tenant_id,id,entry_id,location_id,staff_id,service_id,starts_at,ends_at,
                                 occupied_range,local_timezone,owner_token_hash,idempotency_key,
                                 expires_at)
                                VALUES (:tenant,:id,:entry,:location,:staff,:service,:starts,:ends,
                                        tstzrange(:occ_from,:occ_to,'[)'),:tz,:hash,:key,:expires)"""),
                        {"tenant": tenant, "id": offer_id, "entry": entry["id"],
                         "location": entry["location_id"], "staff": staff_id,
                         "service": entry["service_id"],
                         "starts": starts_at.isoformat(), "ends": ends_at.isoformat(),
                         "occ_from": occupied_from.isoformat(), "occ_to": occupied_to.isoformat(),
                         "tz": entry["local_timezone"], "hash": hash_token(token),
                         "key": payload.idempotency_key, "expires": expires_at.isoformat()})
                else:
                    # The previous attempt lapsed or was retracted: re-offer the same
                    # row with a fresh token and window (mirrors a hold's reactivation).
                    # The claim still carries the EXCLUDE guard, so a slot taken in the
                    # meantime is a 409 there, never a double booking.
                    connection.execute(
                        text("""UPDATE slot_offers SET state='offered', owner_token_hash=:hash,
                                   expires_at=:expires, appointment_id=NULL, updated_at=now()
                                WHERE tenant_id=:tenant AND id=:id"""),
                        {"hash": hash_token(token), "expires": expires_at.isoformat(),
                         "tenant": tenant, "id": offer_id})
                insert_audit(connection, tenant_id, identity, action="waitlist.offer_created",
                             resource_type="slot_offer", resource_id=uuid.UUID(offer_id),
                             reason="slot offered to a waitlist entry",
                             request_id=request.state.request_id,
                             changed_fields=["starts_at", "staff_id", "state", "expires_at"])
            return {"offer_id": offer_id, "owner_token": token, "replayed": False,
                    "state": "offered", "expires_at": expires_at.isoformat(),
                    "entry_id": entry["id"], "starts_at": starts_at.isoformat()}
        finally:
            engine.dispose()

    @app.post("/v1/public/salons/{booking_slug}/offers/{offer_id}/claim", tags=["booking"],
              status_code=201)
    def claim_slot_offer(booking_slug: str, offer_id: uuid.UUID, payload: ClaimOfferIn,
                         request: Request) -> dict:
        """Turn a waitlist offer into a confirmed appointment, atomically.

        The offer row is locked, lapsed offers are swept by state transition, and
        only then is the booking attempted. A lost race rolls the booking back to
        its savepoint, retracts the offer so a stale link cannot keep competing
        for a slot it already lost, and answers 409 `slot_unavailable` - the loser
        never hears about a slot it does not own.

        FR-038 / FR-040: this endpoint books an appointment and nothing else. It
        records no consent row and triggers no outbound message - claiming a slot
        a cancellation freed is not opting a client in to marketing.
        """
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                location = resolve_public_location(connection, booking_slug)
                tenant = location["tenant_id"]
                # Claims and salon withdrawal always lock the waitlist entry before
                # the offer. Looking up the entry id is intentionally unlocked; the
                # entry and offer are both re-read/locked below in the shared order.
                entry_id_for_lock = connection.execute(
                    text("SELECT entry_id::text FROM slot_offers WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": tenant, "id": str(offer_id)}).scalar_one_or_none()
                if entry_id_for_lock is None:
                    raise HTTPException(status_code=404, detail={"code": "offer_not_found"})
                entry_lock = connection.execute(
                    text("SELECT state FROM waitlist_entries WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                    {"tenant": tenant, "id": entry_id_for_lock}).mappings().one_or_none()
                if entry_lock is None:
                    raise HTTPException(status_code=404, detail={"code": "offer_not_found"})

                # One key, one booking: racers on the same claim key queue here
                # instead of racing past a missed replay read into two INSERTs.
                connection.execute(
                    text("SELECT pg_advisory_xact_lock(hashtext(:tenant), hashtext(:key))"),
                    {"tenant": tenant, "key": payload.idempotency_key})
                replayed_claim = connection.execute(
                    text("""SELECT o.id::text AS id, o.appointment_id::text AS appointment_id,
                                   o.owner_token_hash
                            FROM slot_offers o JOIN waitlist_entries e
                              ON e.tenant_id=o.tenant_id AND e.id=o.entry_id
                            WHERE o.tenant_id=:tenant AND o.claim_idempotency_key=:key
                              AND o.state='claimed' FOR UPDATE OF o"""),
                    {"tenant": tenant, "key": payload.idempotency_key}).mappings().one_or_none()
                if replayed_claim is not None:
                    if replayed_claim["id"] != str(offer_id):
                        raise HTTPException(status_code=409, detail={"code": "idempotency_conflict"})
                    if replayed_claim["owner_token_hash"] != hash_token(payload.owner_token):
                        raise HTTPException(status_code=404, detail={"code": "offer_not_found"})
                    return JSONResponse(status_code=200, content={
                        "appointment_id": replayed_claim["appointment_id"], "replayed": True})

                # A validated replay creates nothing; billing restricts new claims.
                assert_bookings_allowed(connection, tenant_id=tenant)
                offer = connection.execute(
                    text("""SELECT o.id::text AS id, o.state, o.owner_token_hash, o.starts_at,
                                   o.ends_at, o.expires_at, o.occupied_range, o.local_timezone,
                                   o.location_id::text AS location_id, o.staff_id::text AS staff_id,
                                   o.service_id::text AS service_id, o.entry_id::text AS entry_id,
                                   e.client_id::text AS entry_client_id, e.state AS entry_state
                            FROM slot_offers o JOIN waitlist_entries e
                              ON e.tenant_id=o.tenant_id AND e.id=o.entry_id
                            WHERE o.tenant_id=:tenant AND o.id=:id FOR UPDATE OF o"""),
                    {"tenant": tenant, "id": str(offer_id)}).mappings().one_or_none()
                # A wrong token and a missing offer are indistinguishable (FR-028): the
                # hash is the lookup key, so a claim naming the wrong client is not the
                # holder of this offer either.
                if offer is None or offer["owner_token_hash"] != hash_token(payload.owner_token):
                        raise HTTPException(status_code=404, detail={"code": "offer_not_found"})
                if offer["state"] == "expired":
                    return JSONResponse(status_code=409, content={"detail": {"code": "offer_expired"}})
                if offer["state"] == "retracted":
                    raise HTTPException(status_code=409, detail={"code": "offer_retracted"})
                if offer["state"] == "claimed":
                    raise HTTPException(status_code=409, detail={"code": "offer_claimed"})
                if offer["state"] != "offered":
                    raise HTTPException(status_code=409, detail={"code": "offer_claimed"})
                if offer["expires_at"] <= datetime.now(tz=dt_timezone.utc):
                    connection.execute(
                        text("""UPDATE slot_offers SET state='expired', updated_at=now()
                                WHERE tenant_id=:tenant AND id=:id"""),
                        {"tenant": tenant, "id": str(offer_id)})
                    return JSONResponse(status_code=409, content={"detail": {"code": "offer_expired"}})
                if offer["entry_state"] != "open":
                    raise HTTPException(status_code=409, detail={"code": "waitlist_entry_not_open"})

                client_ok = connection.execute(
                    text("SELECT 1 FROM clients WHERE tenant_id=:tenant AND id=:id AND deleted_at IS NULL"),
                    {"tenant": tenant, "id": offer["entry_client_id"]}).scalar_one_or_none()
                if client_ok is None:
                    raise HTTPException(status_code=404, detail={"code": "client_not_found"})
                service = _service_snapshot(connection, tenant, offer["service_id"])
                if service is None:
                    # The snapshot survives on the offer, but no new appointment may
                    # confirm against a service the salon has since archived.
                    raise HTTPException(status_code=409, detail={"code": "service_no_longer_offered"})

                try:
                    phase_plan = plan_processing_phases(
                        connection, tenant_id=tenant, service_id=offer["service_id"],
                        location_id=offer["location_id"], starts_at=offer["starts_at"],
                        preferred_staff_id=offer["staff_id"],
                        service_duration=service["duration_minutes"],
                        pre_buffer_minutes=service["pre_buffer_minutes"],
                        post_buffer_minutes=service["post_buffer_minutes"])
                    if phase_plan is None:
                        validate_staff_slot(
                            connection, tenant_id=tenant, staff_id=offer["staff_id"],
                            service_id=offer["service_id"], location_id=offer["location_id"],
                            starts_at=offer["starts_at"], duration_minutes=service["duration_minutes"],
                            pre_buffer_minutes=service["pre_buffer_minutes"],
                            post_buffer_minutes=service["post_buffer_minutes"])
                    resource_ids = _required_resources(
                        connection, tenant_id=tenant, service=service,
                        location_id=offer["location_id"],
                        occupied_from=offer["occupied_range"].lower,
                        occupied_to=offer["occupied_range"].upper)
                except HTTPException as exc:
                    if exc.status_code != 409:
                        raise
                    connection.execute(
                        text("""UPDATE slot_offers SET state='retracted', updated_at=now()
                                WHERE tenant_id=:tenant AND id=:id"""),
                        {"tenant": tenant, "id": str(offer_id)})
                    return JSONResponse(status_code=409, content={"detail": {"code": "slot_unavailable"}})

                appointment_id = uuid.uuid4()
                occupied_from = offer["occupied_range"].lower.isoformat()
                occupied_to = offer["occupied_range"].upper.isoformat()
                savepoint = connection.begin_nested()
                try:
                    # The appointment deliberately carries no idempotency key: the claim
                    # command's key lives on the offer row, and its uniqueness there is
                    # what makes "same key, same appointment" a database fact.
                    connection.execute(
                        text("""INSERT INTO appointments
                                (tenant_id,id,location_id,client_id,staff_id,starts_at,ends_at,
                                 local_timezone,status,source,occupied_range,policy_version)
                                VALUES (:tenant,:id,:location,:client,:staff,:starts,:ends,
                                        :tz,'confirmed','public_booking',
                                        tstzrange(:occ_from,:occ_to,'[)'),1)"""),
                        {"tenant": tenant, "id": str(appointment_id), "location": offer["location_id"],
                         "client": offer["entry_client_id"], "staff": offer["staff_id"],
                         "starts": offer["starts_at"].isoformat(), "ends": offer["ends_at"].isoformat(),
                         "tz": offer["local_timezone"],
                         "occ_from": occupied_from, "occ_to": occupied_to})
                    connection.execute(
                        text("""INSERT INTO appointment_services
                                (tenant_id,id,appointment_id,sequence,service_id,service_version,
                                 service_name,staff_id,price_minor,currency,duration_minutes,
                                pre_buffer_minutes,post_buffer_minutes,starts_at,ends_at,processing_phases)
                                VALUES (:tenant,:id,:appointment,1,:service,:version,:name,:staff,
                                        :price,:currency,:duration,:pre,:post,:starts,:ends,CAST(:phases AS jsonb))"""),
                        {"tenant": tenant, "id": str(uuid.uuid4()), "appointment": str(appointment_id),
                         "service": offer["service_id"], "version": service["version"],
                         "name": service["name"], "staff": offer["staff_id"],
                         "price": service["price_minor"], "currency": service["currency"],
                         "duration": service["duration_minutes"], "pre": service["pre_buffer_minutes"],
                         "post": service["post_buffer_minutes"],
                         "starts": offer["starts_at"].isoformat(), "ends": offer["ends_at"].isoformat(),
                         "phases": json.dumps(phase_plan or [])})
                    # The same EXCLUDE constraint every other booking goes through, so a
                    # range someone else took in the meantime cannot be claimed.
                    insert_staff_reservations(
                        connection, tenant_id=tenant, staff_id=offer["staff_id"],
                        appointment_id=str(appointment_id), phases=phase_plan,
                        occupied_from=offer["occupied_range"].lower,
                        occupied_to=offer["occupied_range"].upper)
                    for resource_id, resource_unit in resource_ids:
                        connection.execute(
                            text("""INSERT INTO reservations
                                    (tenant_id,id,appointment_id,resource_id,resource_unit,occupied_range,state)
                                    VALUES (:tenant,:id,:appointment,:resource,:unit,
                                            tstzrange(:occ_from,:occ_to,'[)'),'active')"""),
                            {"tenant": tenant, "id": str(uuid.uuid4()),
                             "appointment": str(appointment_id), "resource": resource_id,
                             "unit": resource_unit,
                             "occ_from": occupied_from, "occ_to": occupied_to})
                    savepoint.commit()
                except IntegrityError as exc:
                    # Roll the failed booking back to its savepoint, then retract the
                    # offer inside the still-open transaction: a stale link must not
                    # keep competing for a slot it already lost, and nothing is left
                    # half-booked.
                    savepoint.rollback()
                    conflict = str(exc.orig)
                    if ("reservations_no_staff_overlap" in conflict
                            or "reservations_no_resource_overlap" in conflict):
                        connection.execute(
                            text("""UPDATE slot_offers SET state='retracted', updated_at=now()
                                    WHERE tenant_id=:tenant AND id=:id"""),
                            {"tenant": tenant, "id": str(offer_id)})
                        return JSONResponse(status_code=409, content={"detail": {"code": "slot_unavailable"}})
                    raise
                connection.execute(
                    text("""UPDATE slot_offers SET state='claimed', appointment_id=:appointment,
                               claim_idempotency_key=:key, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"appointment": str(appointment_id), "key": payload.idempotency_key,
                     "tenant": tenant, "id": str(offer_id)})
                connection.execute(
                    text("""UPDATE waitlist_entries SET state='fulfilled', updated_at=now()
                            WHERE tenant_id=:tenant AND id=:entry AND state='open'"""),
                    {"tenant": tenant, "entry": offer["entry_id"]})
                connection.execute(
                    text("""INSERT INTO appointment_transitions
                            (tenant_id,id,appointment_id,from_status,to_status,actor_issuer,
                             actor_subject,reason)
                            VALUES (:tenant,:id,:appointment,NULL,'confirmed','public',
                                    'waitlist_claim','claimed a waitlist offer')"""),
                    {"tenant": tenant, "id": str(uuid.uuid4()), "appointment": str(appointment_id)})
            return {"appointment_id": str(appointment_id), "replayed": False}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/waitlist/{entry_id}/withdraw", tags=["booking"])
    def withdraw_waitlist_entry(tenant_id: uuid.UUID, entry_id: uuid.UUID, request: Request,
                                identity=Depends(require_identity)) -> dict:
        """Withdraw a waitlist entry and retract every offer still open on it."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "appointments.manage")
                tenant = str(tenant_id)
                entry = connection.execute(
                    text("SELECT state, location_id FROM waitlist_entries WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                    {"tenant": tenant, "id": str(entry_id)}).mappings().one_or_none()
                if entry is None:
                    raise HTTPException(status_code=404, detail={"code": "waitlist_entry_not_found"})
                require_assigned_location(role, assigned, entry["location_id"])
                if entry["state"] == "withdrawn":
                    # Idempotent: a repeated withdraw reports the state, not an error.
                    return {"entry_id": str(entry_id), "state": "withdrawn", "replayed": True}
                if entry["state"] == "fulfilled":
                    # A fulfilled entry is a booking, not an open request.
                    raise HTTPException(status_code=409, detail={"code": "waitlist_entry_fulfilled"})
                connection.execute(
                    text("""UPDATE waitlist_entries SET state='withdrawn', updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": tenant, "id": str(entry_id)})
                retracted = connection.execute(
                    text("""UPDATE slot_offers SET state='retracted', updated_at=now()
                            WHERE tenant_id=:tenant AND entry_id=:id AND state='offered'"""),
                    {"tenant": tenant, "id": str(entry_id)}).rowcount
                insert_audit(connection, tenant_id, identity, action="waitlist.entry_withdrawn",
                             resource_type="waitlist_entry", resource_id=entry_id,
                             reason="waitlist entry withdrawn",
                             request_id=request.state.request_id,
                             changed_fields=["state"] + (["slot_offers.state"] if retracted else []))
            return {"entry_id": str(entry_id), "state": "withdrawn", "replayed": False,
                    "offers_retracted": int(retracted)}
        finally:
            engine.dispose()
