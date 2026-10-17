"""Bookable resources and service requirements (FR-021, TASK-018).

The invariant enforced here is ownership, not capacity arithmetic: a service and a resource
only belong together when both are owned by the same tenant *and* the resource sits at a
location the tenant owns. A requirement that names a resource from another location or
another tenant is refused, because it would produce a booking that can never be satisfied
(FR-021).

R1 keeps capacity at exactly one per resource (requirements.md:58); the schema's
`quantity = 1` check makes that a stored fact rather than a runtime convention.

Dependencies are injected for the same reason the sibling modules inject them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from salon_platform.authorization import require_assigned_location
from modules.catalogue.scheduling_policy import lock_resource_schedule, lock_service_resource_policy


class ResourceIn(BaseModel):
    location_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(pattern="^(chair|room|equipment)$")
    capacity: int = Field(default=1, ge=1, le=100)


class RequirementsIn(BaseModel):
    location_id: uuid.UUID
    expected_version: int = Field(ge=1)
    resource_ids: list[uuid.UUID] = Field(default_factory=list)
    quantities: dict[uuid.UUID, int] = Field(default_factory=dict)


class ProcessingPhaseIn(BaseModel):
    sequence: int = Field(ge=1, le=12)
    duration_minutes: int = Field(ge=1, le=1440)
    mode: str = Field(pattern="^(attended|gap)$")
    staff_count: int = Field(ge=0, le=4)

    @model_validator(mode="after")
    def validate_mode_staff(self):
        if ((self.mode == "attended" and self.staff_count < 1)
                or (self.mode == "gap" and self.staff_count != 0)):
            raise ValueError("phase staff count does not match its mode")
        return self


class ProcessingPhasesIn(BaseModel):
    expected_version: int = Field(ge=1)
    phases: list[ProcessingPhaseIn] = Field(max_length=12)

    @field_validator("phases")
    @classmethod
    def ordered_phase_numbers(cls, phases):
        if [phase.sequence for phase in phases] != list(range(1, len(phases) + 1)):
            raise ValueError("phase sequences must be contiguous")
        return phases


def register_resource_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    def resource_or_404(connection, tenant_id: uuid.UUID, resource_id: uuid.UUID):
        row = connection.execute(
            text("""SELECT * FROM bookable_resources WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": str(tenant_id), "id": str(resource_id)}).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "resource_not_found"})
        return row

    def serialize(row) -> dict:
        data = dict(row)
        for key in ("created_at", "updated_at"):
            if data.get(key) is not None:
                data[key] = data[key].isoformat()
        for key in ("id", "location_id"):
            if data.get(key) is not None:
                data[key] = str(data[key])
        return data

    @app.post("/v1/tenants/{tenant_id}/resources", tags=["catalogue"], status_code=201)
    def create_resource(tenant_id: uuid.UUID, payload: ResourceIn, request: Request,
                        identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        resource_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "resources.manage")
                require_assigned_location(role, assigned, payload.location_id)
                owned = connection.execute(
                    text("SELECT 1 FROM locations WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(payload.location_id)}).scalar_one_or_none()
                if owned is None:
                    raise HTTPException(status_code=404, detail={"code": "location_not_found"})
                duplicate = connection.execute(
                    text("""SELECT 1 FROM bookable_resources WHERE tenant_id=:tenant
                            AND location_id=:location AND name=:name"""),
                    {"tenant": str(tenant_id), "location": str(payload.location_id),
                     "name": payload.name}).scalar_one_or_none()
                if duplicate is not None:
                    raise HTTPException(status_code=409, detail={"code": "resource_name_in_use"})
                connection.execute(
                    text("""INSERT INTO bookable_resources
                            (tenant_id,id,location_id,name,kind,capacity)
                            VALUES (:tenant,:id,:location,:name,:kind,:capacity)"""),
                    {"tenant": str(tenant_id), "id": str(resource_id), "location": str(payload.location_id),
                     "name": payload.name, "kind": payload.kind, "capacity": payload.capacity})
                insert_audit(connection, tenant_id, identity, action="resource.created",
                             resource_type="resource", resource_id=resource_id,
                             reason=f"{payload.kind} {payload.name} created",
                             request_id=request.state.request_id, changed_fields=["name", "kind", "capacity"])
                row = connection.execute(
                    text("SELECT * FROM bookable_resources WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(resource_id)}).mappings().one()
            return serialize(row)
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/resources", tags=["catalogue"])
    def list_resources(tenant_id: uuid.UUID, identity=Depends(require_identity),
                       location_id: uuid.UUID | None = None, include_inactive: bool = False) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "resources.read")
                where = "tenant_id=:tenant"
                params: dict = {"tenant": str(tenant_id)}
                if not include_inactive:
                    where += " AND active"
                if location_id is not None:
                    require_assigned_location(role, assigned, location_id)
                    where += " AND location_id=:location"
                    params["location"] = str(location_id)
                elif role != "owner":
                    where += " AND location_id=ANY(:locations)"
                    params["locations"] = assigned
                rows = connection.execute(
                    text(f"""SELECT id::text AS id, location_id::text AS location_id, name, kind,
                                    capacity, active, version FROM bookable_resources WHERE {where}
                             ORDER BY kind, name"""),
                    params).mappings().all()
            return {"items": [serialize(r) for r in rows]}
        finally:
            engine.dispose()

    @app.put("/v1/tenants/{tenant_id}/services/{service_id}/resources", tags=["catalogue"])
    def set_service_resources(tenant_id: uuid.UUID, service_id: uuid.UUID, payload: RequirementsIn,
                              request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "resources.manage")
                require_assigned_location(role, assigned, payload.location_id)
                service = connection.execute(
                    text("SELECT version FROM services WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                    {"tenant": str(tenant_id), "id": str(service_id)}).scalar_one_or_none()
                if service is None:
                    raise HTTPException(status_code=404, detail={"code": "service_not_found"})
                if service != payload.expected_version:
                    raise HTTPException(status_code=409, detail={"code": "service_version_conflict"})
                location = connection.execute(
                    text("SELECT 1 FROM locations WHERE tenant_id=:tenant AND id=:location"),
                    {"tenant": str(tenant_id), "location": str(payload.location_id)}).scalar_one_or_none()
                if location is None:
                    raise HTTPException(status_code=404, detail={"code": "location_not_found"})
                lock_service_resource_policy(connection, str(tenant_id), str(service_id), str(payload.location_id))
                if len(set(payload.resource_ids)) != len(payload.resource_ids):
                    raise HTTPException(status_code=422, detail={"code": "duplicate_resource_requirement"})
                if set(payload.quantities) - set(payload.resource_ids):
                    raise HTTPException(status_code=422, detail={"code": "quantity_for_unselected_resource"})
                previous = connection.execute(
                    text("""SELECT resource_id::text FROM service_resource_requirements
                            WHERE tenant_id=:tenant AND service_id=:service AND location_id=:location"""),
                    {"tenant": str(tenant_id), "service": str(service_id),
                     "location": str(payload.location_id)}).scalars().all()
                # Validate the complete replacement before deleting the current rows.
                for resource_id in payload.resource_ids:
                    resource = resource_or_404(connection, tenant_id, resource_id)
                    if resource["location_id"] != payload.location_id:
                        raise HTTPException(status_code=422, detail={"code": "resource_location_mismatch"})
                    quantity = payload.quantities.get(resource_id, 1)
                    if not 1 <= quantity <= resource["capacity"]:
                        raise HTTPException(status_code=422,
                                            detail={"code": "resource_quantity_exceeds_capacity"})
                for resource_id in sorted(set(previous) | {str(value) for value in payload.resource_ids}):
                    lock_resource_schedule(connection, str(tenant_id), resource_id)
                # Capacity can change between the optimistic validation above and
                # lock acquisition. Re-read every selected pool while holding the
                # same schedule lock used by capacity updates.
                for resource_id in payload.resource_ids:
                    resource = resource_or_404(connection, tenant_id, resource_id)
                    quantity = payload.quantities.get(resource_id, 1)
                    if resource["location_id"] != payload.location_id:
                        raise HTTPException(status_code=422, detail={"code": "resource_location_mismatch"})
                    if quantity > resource["capacity"]:
                        raise HTTPException(status_code=422,
                                            detail={"code": "resource_quantity_exceeds_capacity"})
                connection.execute(
                    text("""DELETE FROM service_resource_requirements
                            WHERE tenant_id=:tenant AND service_id=:service AND location_id=:location"""),
                    {"tenant": str(tenant_id), "service": str(service_id), "location": str(payload.location_id)})
                for resource_id in payload.resource_ids:
                    connection.execute(
                        text("""INSERT INTO service_resource_requirements
                                (tenant_id,id,service_id,location_id,resource_id,quantity)
                                VALUES (:tenant,:id,:service,:location,:resource,:quantity)"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                         "service": str(service_id), "location": str(payload.location_id),
                         "resource": str(resource_id), "quantity": payload.quantities.get(resource_id, 1)})
                # A service that needs a resource is marked so, keeping the flag and the
                # requirement list from disagreeing.
                version = connection.execute(
                    text("""UPDATE services SET requires_resource=(SELECT count(*) > 0
                              FROM service_resource_requirements WHERE tenant_id=:tenant AND service_id=:service),
                              version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:service RETURNING version"""),
                    {"tenant": str(tenant_id), "service": str(service_id)}).scalar_one()
                insert_audit(connection, tenant_id, identity, action="service.resources_set",
                             resource_type="service", resource_id=service_id,
                             reason="service resource requirements set",
                             request_id=request.state.request_id,
                             changed_fields=["location_id", "resource_ids", "version"])
            return {"service_id": str(service_id), "location_id": str(payload.location_id),
                    "resource_ids": [str(r) for r in payload.resource_ids], "version": version}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/services/{service_id}/resources", tags=["catalogue"])
    def get_service_resources(tenant_id: uuid.UUID, service_id: uuid.UUID, location_id: uuid.UUID,
                              identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "resources.read")
                require_assigned_location(role, assigned, location_id)
                exists = connection.execute(
                    text("SELECT 1 FROM services WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(service_id)}).scalar_one_or_none()
                if exists is None:
                    raise HTTPException(status_code=404, detail={"code": "service_not_found"})
                location = connection.execute(
                    text("SELECT 1 FROM locations WHERE tenant_id=:tenant AND id=:location"),
                    {"tenant": str(tenant_id), "location": str(location_id)}).scalar_one_or_none()
                if location is None:
                    raise HTTPException(status_code=404, detail={"code": "location_not_found"})
                rows = connection.execute(
                    text("""SELECT r.id::text AS id, r.name, r.kind, r.capacity,
                                   r.location_id::text AS location_id, q.quantity
                            FROM service_resource_requirements q
                            JOIN bookable_resources r
                              ON r.tenant_id=q.tenant_id AND r.id=q.resource_id
                            WHERE q.tenant_id=:tenant AND q.service_id=:service AND q.location_id=:location
                            ORDER BY r.name"""),
                    {"tenant": str(tenant_id), "service": str(service_id), "location": str(location_id)}).mappings().all()
                version = connection.execute(
                    text("SELECT version FROM services WHERE tenant_id=:tenant AND id=:service"),
                    {"tenant": str(tenant_id), "service": str(service_id)}).scalar_one()
            return {"service_id": str(service_id), "location_id": str(location_id), "version": version,
                    "items": [dict(r) for r in rows]}
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/resources/{resource_id}", tags=["catalogue"])
    def update_resource(tenant_id: uuid.UUID, resource_id: uuid.UUID, payload: dict, request: Request,
                        identity=Depends(require_identity)) -> dict:
        """Rename or (de)activate a resource. A resource is never moved between locations."""
        engine = app.state.engine_factory()
        changes = {k: v for k, v in payload.items() if k in ("name", "active", "capacity")}
        if not changes:
            raise HTTPException(status_code=422, detail={"code": "resource_change_required"})
        if "active" in changes and not isinstance(changes["active"], bool):
            raise HTTPException(status_code=422, detail={"code": "invalid_active_value"})
        if "capacity" in changes and (not isinstance(changes["capacity"], int)
                                       or isinstance(changes["capacity"], bool)
                                       or not 1 <= changes["capacity"] <= 100):
            raise HTTPException(status_code=422, detail={"code": "invalid_resource_capacity"})
        if "name" in changes and not (isinstance(changes["name"], str) and 1 <= len(changes["name"].strip()) <= 120):
            raise HTTPException(status_code=422, detail={"code": "invalid_resource_name"})
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "resources.manage")
                resource = resource_or_404(connection, tenant_id, resource_id)
                require_assigned_location(role, assigned, resource["location_id"])
                lock_resource_schedule(connection, str(tenant_id), str(resource_id))
                resource = resource_or_404(connection, tenant_id, resource_id)
                if "capacity" in changes:
                    configured_quantity = connection.execute(
                        text("""SELECT max(quantity) FROM service_resource_requirements
                                WHERE tenant_id=:tenant AND resource_id=:resource"""),
                        {"tenant": str(tenant_id), "resource": str(resource_id)}).scalar_one()
                    if configured_quantity is not None and configured_quantity > changes["capacity"]:
                        raise HTTPException(status_code=409,
                                            detail={"code": "capacity_below_configured_quantity"})
                    highest_reserved_unit = connection.execute(
                        text("""SELECT max(resource_unit) FROM reservations WHERE tenant_id=:tenant
                                AND resource_id=:resource AND state='active'"""),
                        {"tenant": str(tenant_id), "resource": str(resource_id)}).scalar_one()
                    if highest_reserved_unit is not None and highest_reserved_unit > changes["capacity"]:
                        raise HTTPException(status_code=409,
                                            detail={"code": "capacity_below_active_reservation"})
                assignments = ", ".join(f"{key}=:{key}" for key in changes)
                updated = connection.execute(
                    text(f"""UPDATE bookable_resources SET {assignments}, version=version+1,
                            updated_at=now() WHERE tenant_id=:tenant AND id=:id RETURNING *"""),
                    {"tenant": str(tenant_id), "id": str(resource_id), **changes}).mappings().one()
                insert_audit(connection, tenant_id, identity, action="resource.updated",
                             resource_type="resource", resource_id=resource_id,
                             reason="resource updated", request_id=request.state.request_id,
                             changed_fields=list(changes.keys()))
            return serialize(updated)
        finally:
            engine.dispose()

    @app.put("/v1/tenants/{tenant_id}/services/{service_id}/phases", tags=["catalogue"])
    def set_service_phases(tenant_id: uuid.UUID, service_id: uuid.UUID,
                           payload: ProcessingPhasesIn, request: Request,
                           identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "services.manage")
                service = connection.execute(
                    text("SELECT duration_minutes, version FROM services "
                         "WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                    {"tenant": str(tenant_id), "id": str(service_id)}).mappings().one_or_none()
                if service is None:
                    raise HTTPException(status_code=404, detail={"code": "service_not_found"})
                if service["version"] != payload.expected_version:
                    raise HTTPException(status_code=409, detail={"code": "service_version_conflict"})
                if (not payload.phases
                        or sum(p.duration_minutes for p in payload.phases) != service["duration_minutes"]
                        or not any(p.mode == "attended" for p in payload.phases)):
                    raise HTTPException(status_code=422, detail={"code": "phase_duration_mismatch"})
                connection.execute(
                    text("DELETE FROM service_processing_phases "
                         "WHERE tenant_id=:tenant AND service_id=:service"),
                    {"tenant": str(tenant_id), "service": str(service_id)})
                for phase in payload.phases:
                    connection.execute(
                        text("""INSERT INTO service_processing_phases
                            (tenant_id,service_id,sequence,duration_minutes,mode,staff_count)
                            VALUES (:tenant,:service,:sequence,:duration,:mode,:staff_count)"""),
                        {"tenant": str(tenant_id), "service": str(service_id),
                         "sequence": phase.sequence, "duration": phase.duration_minutes,
                         "mode": phase.mode, "staff_count": phase.staff_count})
                updated = connection.execute(
                    text("""UPDATE services SET version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:service RETURNING version"""),
                    {"tenant": str(tenant_id), "service": str(service_id)}).scalar_one()
                insert_audit(connection, tenant_id, identity, action="service.phases_set",
                             resource_type="service", resource_id=service_id,
                             reason="service processing phases changed",
                             request_id=request.state.request_id,
                             changed_fields=["processing_phases", "version"])
            return {"service_id": str(service_id), "version": updated,
                    "phases": _serialize_phases(payload.phases)}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/services/{service_id}/phases", tags=["catalogue"])
    def get_service_phases(tenant_id: uuid.UUID, service_id: uuid.UUID,
                           identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "services.read")
                service = connection.execute(
                    text("SELECT version FROM services WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(service_id)}).mappings().one_or_none()
                if service is None:
                    raise HTTPException(status_code=404, detail={"code": "service_not_found"})
                rows = connection.execute(
                    text("""SELECT sequence,duration_minutes,mode,staff_count
                            FROM service_processing_phases WHERE tenant_id=:tenant AND service_id=:service
                            ORDER BY sequence"""),
                    {"tenant": str(tenant_id), "service": str(service_id)}).mappings().all()
            phases = [ProcessingPhaseIn.model_validate(dict(row)) for row in rows]
            return {"service_id": str(service_id), "version": service["version"],
                    "phases": _serialize_phases(phases)}
        finally:
            engine.dispose()


def _serialize_phases(phases) -> list[dict]:
    offset = 0
    result = []
    for phase in phases:
        result.append({**phase.model_dump(), "start_offset_minutes": offset})
        offset += phase.duration_minutes
    return result
