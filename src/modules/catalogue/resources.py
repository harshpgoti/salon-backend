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
from pydantic import BaseModel, Field
from sqlalchemy import text


class ResourceIn(BaseModel):
    location_id: uuid.UUID
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(pattern="^(chair|room|equipment)$")


class RequirementsIn(BaseModel):
    resource_ids: list[uuid.UUID] = Field(default_factory=list)


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
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "resources.manage")
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
                            (tenant_id,id,location_id,name,kind)
                            VALUES (:tenant,:id,:location,:name,:kind)"""),
                    {"tenant": str(tenant_id), "id": str(resource_id), "location": str(payload.location_id),
                     "name": payload.name, "kind": payload.kind})
                insert_audit(connection, tenant_id, identity, action="resource.created",
                             resource_type="resource", resource_id=resource_id,
                             reason=f"{payload.kind} {payload.name} created",
                             request_id=request.state.request_id, changed_fields=["name", "kind"])
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
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "resources.read")
                where = "tenant_id=:tenant"
                params: dict = {"tenant": str(tenant_id)}
                if not include_inactive:
                    where += " AND active"
                if location_id is not None:
                    where += " AND location_id=:location"
                    params["location"] = str(location_id)
                rows = connection.execute(
                    text(f"""SELECT id::text AS id, location_id::text AS location_id, name, kind,
                                    active, version FROM bookable_resources WHERE {where}
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
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "resources.manage")
                service = connection.execute(
                    text("SELECT 1 FROM services WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(service_id)}).scalar_one_or_none()
                if service is None:
                    raise HTTPException(status_code=404, detail={"code": "service_not_found"})
                # Every named resource must exist in this tenant. A resource from another
                # tenant is a 404; ownership is checked before anything is written.
                for resource_id in payload.resource_ids:
                    resource_or_404(connection, tenant_id, resource_id)
                connection.execute(
                    text("""DELETE FROM service_resource_requirements
                            WHERE tenant_id=:tenant AND service_id=:service"""),
                    {"tenant": str(tenant_id), "service": str(service_id)})
                for resource_id in payload.resource_ids:
                    connection.execute(
                        text("""INSERT INTO service_resource_requirements
                                (tenant_id,id,service_id,resource_id) VALUES (:tenant,:id,:service,:resource)"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                         "service": str(service_id), "resource": str(resource_id)})
                # A service that needs a resource is marked so, keeping the flag and the
                # requirement list from disagreeing.
                connection.execute(
                    text("""UPDATE services SET requires_resource=(SELECT count(*) > 0
                              FROM service_resource_requirements WHERE tenant_id=:tenant AND service_id=:service)
                            WHERE tenant_id=:tenant AND id=:service"""),
                    {"tenant": str(tenant_id), "service": str(service_id)})
                insert_audit(connection, tenant_id, identity, action="service.resources_set",
                             resource_type="service", resource_id=service_id,
                             reason="service resource requirements set",
                             request_id=request.state.request_id, changed_fields=["resource_ids"])
            return {"service_id": str(service_id), "resource_ids": [str(r) for r in payload.resource_ids]}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/services/{service_id}/resources", tags=["catalogue"])
    def get_service_resources(tenant_id: uuid.UUID, service_id: uuid.UUID,
                              identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "resources.read")
                exists = connection.execute(
                    text("SELECT 1 FROM services WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(service_id)}).scalar_one_or_none()
                if exists is None:
                    raise HTTPException(status_code=404, detail={"code": "service_not_found"})
                rows = connection.execute(
                    text("""SELECT r.id::text AS id, r.name, r.kind, r.location_id::text AS location_id,
                                   q.quantity
                            FROM service_resource_requirements q
                            JOIN bookable_resources r
                              ON r.tenant_id=q.tenant_id AND r.id=q.resource_id
                            WHERE q.tenant_id=:tenant AND q.service_id=:service
                            ORDER BY r.name"""),
                    {"tenant": str(tenant_id), "service": str(service_id)}).mappings().all()
            return {"items": [dict(r) for r in rows]}
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/resources/{resource_id}", tags=["catalogue"])
    def update_resource(tenant_id: uuid.UUID, resource_id: uuid.UUID, payload: dict, request: Request,
                        identity=Depends(require_identity)) -> dict:
        """Rename or (de)activate a resource. A resource is never moved between locations."""
        engine = app.state.engine_factory()
        changes = {k: v for k, v in payload.items() if k in ("name", "active")}
        if not changes:
            raise HTTPException(status_code=422, detail={"code": "resource_change_required"})
        if "active" in changes and not isinstance(changes["active"], bool):
            raise HTTPException(status_code=422, detail={"code": "invalid_active_value"})
        if "name" in changes and not (isinstance(changes["name"], str) and 1 <= len(changes["name"].strip()) <= 120):
            raise HTTPException(status_code=422, detail={"code": "invalid_resource_name"})
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "resources.manage")
                resource_or_404(connection, tenant_id, resource_id)
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