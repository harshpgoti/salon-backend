"""Weekly hours and dated exceptions (FR-022, FR-024, TASK-017).

Two invariants drive this module:

- **An interval is local wall-clock within one day.** `closes_at` must be after `opens_at`;
  an overnight window is rejected and expressed as two intervals instead, so a rule is never
  ambiguous across a DST transition (qa-plan.md QA-09).
- **A change versions the rule; it never overwrites history.** Publishing a new weekly
  pattern supersedes the active rows (stamping `superseded_at`) and writes fresh ones, so a
  past date still resolves to the hours that actually applied (FR-024).

Dependencies are injected for the same reason the sibling modules inject them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date, datetime, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text


class HoursInterval(BaseModel):
    weekday: int = Field(ge=0, le=6)
    opens_at: str = Field(pattern=r"^\d{2}:\d{2}$")
    closes_at: str = Field(pattern=r"^\d{2}:\d{2}$")


class PublishHoursIn(BaseModel):
    intervals: list[HoursInterval] = Field(default_factory=list)
    effective_from: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ExceptionIn(BaseModel):
    exception_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    kind: str = Field(pattern="^(closed|custom)$")
    opens_at: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    closes_at: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    label: str | None = Field(default=None, max_length=120)


class PublishExceptionIn(BaseModel):
    published: bool = True


def _minutes(value: str) -> int:
    hours, minutes = value.split(":")
    return int(hours) * 60 + int(minutes)


def validate_interval(opens_at: str, closes_at: str) -> None:
    """Reject a zero-length or overnight interval within a single local day."""
    if _minutes(closes_at) <= _minutes(opens_at):
        raise HTTPException(status_code=422, detail={"code": "interval_closes_before_opens"})


def validate_local_date(value: str, location_timezone: str) -> date:
    """Reject a date the location's timezone cannot represent as a local day."""
    try:
        zone = ZoneInfo(location_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(status_code=422, detail={"code": "invalid_location_timezone",
                                                     "timezone": location_timezone})
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise HTTPException(status_code=422, detail={"code": "invalid_date"})
    # A reasonable horizon: a date a century out is a typo, not a plan.
    if not (date(2000, 1, 1) <= parsed <= date(2100, 12, 31)):
        raise HTTPException(status_code=422, detail={"code": "date_out_of_range"})
    # Instantiating a naive local time on that date exercises the DST table; a zone that
    # cannot represent it is a configuration error, not an availability answer.
    try:
        datetime.combine(parsed, time(12, 0), tzinfo=zone)
    except (ValueError, OverflowError):
        raise HTTPException(status_code=422, detail={"code": "date_not_representable"})
    return parsed


def register_hours_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    def location_or_404(connection, tenant_id: uuid.UUID, location_id: uuid.UUID):
        row = connection.execute(
            text("""SELECT id::text AS id, display_name, timezone FROM locations
                    WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": str(tenant_id), "id": str(location_id)}).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "location_not_found"})
        return row

    @app.put("/v1/tenants/{tenant_id}/locations/{location_id}/hours", tags=["catalogue"])
    def publish_hours(tenant_id: uuid.UUID, location_id: uuid.UUID, payload: PublishHoursIn,
                      request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "hours.manage")
                location = location_or_404(connection, tenant_id, location_id)
                effective_from = payload.effective_from or date.today().isoformat()
                validate_local_date(effective_from, location["timezone"])
                for interval in payload.intervals:
                    validate_interval(interval.opens_at, interval.closes_at)
                # Overlapping intervals on the same weekday would produce a schedule no
                # single reading of the rule can defend, so they are refused.
                by_day: dict[int, list[tuple[int, int]]] = {}
                for interval in payload.intervals:
                    spans = by_day.setdefault(interval.weekday, [])
                    spans.append((_minutes(interval.opens_at), _minutes(interval.closes_at)))
                for weekday, spans in by_day.items():
                    ordered = sorted(spans)
                    for previous, current in zip(ordered, ordered[1:]):
                        if current[0] < previous[1]:
                            raise HTTPException(status_code=422, detail={"code": "hours_overlap",
                                                                         "weekday": weekday})

                # Supersede the currently-active pattern rather than editing it in place:
                # a past date must still resolve to the hours that actually applied.
                connection.execute(
                    text("""UPDATE location_hours SET superseded_at=now(), updated_at=now(),
                            version=version+1
                            WHERE tenant_id=:tenant AND location_id=:location
                              AND superseded_at IS NULL"""),
                    {"tenant": str(tenant_id), "location": str(location_id)})
                created = []
                for interval in payload.intervals:
                    row = connection.execute(
                        text("""INSERT INTO location_hours
                                (tenant_id,id,location_id,weekday,opens_at,closes_at,effective_from)
                                VALUES (:tenant,:id,:location,:weekday,:opens,:closes,:from_date)
                                RETURNING id::text AS id, weekday, opens_at::text AS opens_at,
                                          closes_at::text AS closes_at"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "location": str(location_id),
                         "weekday": interval.weekday, "opens": interval.opens_at,
                         "closes": interval.closes_at, "from_date": effective_from}).mappings().one()
                    created.append(dict(row))
                insert_audit(connection, tenant_id, identity, action="location_hours.published",
                             resource_type="location", resource_id=location_id,
                             reason="weekly hours published", request_id=request.state.request_id,
                             changed_fields=["intervals", "effective_from"])
            return {"location_id": str(location_id), "effective_from": effective_from, "intervals": created}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/locations/{location_id}/hours", tags=["catalogue"])
    def get_hours(tenant_id: uuid.UUID, location_id: uuid.UUID, identity=Depends(require_identity),
                  include_history: bool = False) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "hours.read")
                location_or_404(connection, tenant_id, location_id)
                where = "tenant_id=:tenant AND location_id=:location"
                if not include_history:
                    where += " AND superseded_at IS NULL"
                rows = connection.execute(
                    text(f"""SELECT id::text AS id, weekday, to_char(opens_at,'HH24:MI') AS opens_at,
                                    to_char(closes_at,'HH24:MI') AS closes_at, effective_from, effective_to,
                                    superseded_at, version
                             FROM location_hours WHERE {where}
                             ORDER BY weekday, opens_at"""),
                    {"tenant": str(tenant_id), "location": str(location_id)}).mappings().all()
            return {"items": [{**dict(r),
                               "effective_from": r["effective_from"].isoformat(),
                               "effective_to": r["effective_to"].isoformat() if r["effective_to"] else None,
                               "superseded_at": r["superseded_at"].isoformat() if r["superseded_at"] else None}
                              for r in rows]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/locations/{location_id}/exceptions", tags=["catalogue"], status_code=201)
    def create_exception(tenant_id: uuid.UUID, location_id: uuid.UUID, payload: ExceptionIn,
                         request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        exception_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "hours.manage")
                location = location_or_404(connection, tenant_id, location_id)
                validate_local_date(payload.exception_date, location["timezone"])
                if payload.kind == "custom":
                    if payload.opens_at is None or payload.closes_at is None:
                        raise HTTPException(status_code=422, detail={"code": "custom_exception_needs_hours"})
                    validate_interval(payload.opens_at, payload.closes_at)
                elif payload.opens_at or payload.closes_at:
                    raise HTTPException(status_code=422, detail={"code": "closed_exception_has_no_hours"})
                existing = connection.execute(
                    text("""SELECT 1 FROM location_exceptions WHERE tenant_id=:tenant
                            AND location_id=:location AND exception_date=:on"""),
                    {"tenant": str(tenant_id), "location": str(location_id),
                     "on": payload.exception_date}).scalar_one_or_none()
                if existing is not None:
                    raise HTTPException(status_code=409, detail={"code": "exception_already_exists"})
                connection.execute(
                    text("""INSERT INTO location_exceptions
                            (tenant_id,id,location_id,exception_date,kind,opens_at,closes_at,label)
                            VALUES (:tenant,:id,:location,:on,:kind,:opens,:closes,:label)"""),
                    {"tenant": str(tenant_id), "id": str(exception_id), "location": str(location_id),
                     "on": payload.exception_date, "kind": payload.kind,
                     "opens": payload.opens_at, "closes": payload.closes_at, "label": payload.label})
                insert_audit(connection, tenant_id, identity, action="location_exception.created",
                             resource_type="location", resource_id=location_id,
                             reason=f"{payload.kind} exception on {payload.exception_date}",
                             request_id=request.state.request_id,
                             changed_fields=["exception_date", "kind"])
            return {"id": str(exception_id), "exception_date": payload.exception_date,
                    "kind": payload.kind, "opens_at": payload.opens_at,
                    "closes_at": payload.closes_at, "published_at": None, "version": 1}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/locations/{location_id}/exceptions", tags=["catalogue"])
    def list_exceptions(tenant_id: uuid.UUID, location_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "hours.read")
                location_or_404(connection, tenant_id, location_id)
                rows = connection.execute(
                    text("""SELECT id::text AS id, exception_date, kind, to_char(opens_at,'HH24:MI') AS opens_at,
                                   to_char(closes_at,'HH24:MI') AS closes_at, label, published_at, version
                            FROM location_exceptions WHERE tenant_id=:tenant AND location_id=:location
                            ORDER BY exception_date"""),
                    {"tenant": str(tenant_id), "location": str(location_id)}).mappings().all()
            return {"items": [{**dict(r),
                               "exception_date": r["exception_date"].isoformat(),
                               "published_at": r["published_at"].isoformat() if r["published_at"] else None}
                              for r in rows]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/locations/{location_id}/exceptions/{exception_id}/publish",
              tags=["catalogue"])
    def publish_exception(tenant_id: uuid.UUID, location_id: uuid.UUID, exception_id: uuid.UUID,
                          payload: PublishExceptionIn, request: Request,
                          identity=Depends(require_identity)) -> dict:
        """Publish or retract an exception, so clients see a reviewed change (FR-024)."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "hours.manage")
                location_or_404(connection, tenant_id, location_id)
                current = connection.execute(
                    text("""SELECT * FROM location_exceptions
                            WHERE tenant_id=:tenant AND location_id=:location AND id=:id FOR UPDATE"""),
                    {"tenant": str(tenant_id), "location": str(location_id),
                     "id": str(exception_id)}).mappings().one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "exception_not_found"})
                updated = connection.execute(
                    text("""UPDATE location_exceptions
                            SET published_at = CASE WHEN :published THEN now() ELSE NULL END,
                                version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id RETURNING *
                            """),
                    {"published": payload.published, "tenant": str(tenant_id),
                     "id": str(exception_id)}).mappings().one()
                insert_audit(connection, tenant_id, identity, action="location_exception.published",
                             resource_type="location", resource_id=location_id,
                             reason=f"exception {'published' if payload.published else 'retracted'}",
                             request_id=request.state.request_id,
                             changed_fields=["published_at"])
            return {**dict(updated), "id": str(updated["id"]), "location_id": str(updated["location_id"]),
                    "exception_date": updated["exception_date"].isoformat(),
                    "opens_at": updated["opens_at"].strftime("%H:%M") if updated["opens_at"] else None,
                    "closes_at": updated["closes_at"].strftime("%H:%M") if updated["closes_at"] else None,
                    "published_at": updated["published_at"].isoformat() if updated["published_at"] else None}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/locations/{location_id}/public-hours", tags=["catalogue"])
    def public_hours(tenant_id: uuid.UUID, location_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        """The current and upcoming published hours a client would see."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "hours.read")
                location = location_or_404(connection, tenant_id, location_id)
                weekly = connection.execute(
                    text("""SELECT weekday, to_char(opens_at,'HH24:MI') AS opens_at,
                                   to_char(closes_at,'HH24:MI') AS closes_at
                            FROM location_hours WHERE tenant_id=:tenant AND location_id=:location
                              AND superseded_at IS NULL ORDER BY weekday, opens_at"""),
                    {"tenant": str(tenant_id), "location": str(location_id)}).mappings().all()
                upcoming = connection.execute(
                    text("""SELECT exception_date, kind, to_char(opens_at,'HH24:MI') AS opens_at,
                                   to_char(closes_at,'HH24:MI') AS closes_at, label
                            FROM location_exceptions WHERE tenant_id=:tenant AND location_id=:location
                              AND published_at IS NOT NULL AND exception_date >= CURRENT_DATE
                            ORDER BY exception_date"""),
                    {"tenant": str(tenant_id), "location": str(location_id)}).mappings().all()
            return {"timezone": location["timezone"],
                    "weekly": [dict(r) for r in weekly],
                    "exceptions": [{**dict(r), "exception_date": r["exception_date"].isoformat()}
                                   for r in upcoming]}
        finally:
            engine.dispose()