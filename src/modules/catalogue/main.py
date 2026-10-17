"""Versioned service catalogue (FR-019, TASK-015).

Two invariants drive this module and are enforced at the boundary, not in the UI:

- **Money is an integer count of CAD minor units.** A price is accepted as `price_minor`
  (cents) and never as a decimal string that might round; `currency` is fixed to CAD
  (requirements.md:56).
- **Archiving never rewrites history.** `archive` sets `archived_at` and bumps the version;
  the row and its values remain readable so appointment history naming a since-archived
  service still resolves (FR-019). A reopened service clears the stamp rather than
  creating a duplicate.

Dependencies are injected for the same reason the sibling modules inject them.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

MAX_PRICE_MINOR = 100_000_000


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class ServiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    category_id: uuid.UUID | None = None
    duration_minutes: int = Field(ge=5, le=1440)
    pre_buffer_minutes: int = Field(default=0, ge=0, le=240)
    post_buffer_minutes: int = Field(default=0, ge=0, le=240)
    price_minor: int = Field(ge=0, le=MAX_PRICE_MINOR)
    rebooking_interval_days: int | None = Field(default=None, ge=1, le=730)
    requires_resource: bool = False


class ServicePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    category_id: uuid.UUID | None = None
    duration_minutes: int | None = Field(default=None, ge=5, le=1440)
    pre_buffer_minutes: int | None = Field(default=None, ge=0, le=240)
    post_buffer_minutes: int | None = Field(default=None, ge=0, le=240)
    price_minor: int | None = Field(default=None, ge=0, le=MAX_PRICE_MINOR)
    rebooking_interval_days: int | None = Field(default=None, ge=1, le=730)
    expected_version: int = Field(ge=1)


    @field_validator("name", "duration_minutes", "pre_buffer_minutes",
                     "post_buffer_minutes", "price_minor")
    @classmethod
    def required_patch_value(cls, value):
        # Omission uses the default without validation; explicit null is invalid.
        if value is None:
            raise ValueError("required service fields cannot be null")
        return value


class OverrideIn(BaseModel):
    staff_ref: uuid.UUID
    duration_minutes: int | None = Field(default=None, ge=5, le=1440)
    price_minor: int | None = Field(default=None, ge=0, le=MAX_PRICE_MINOR)


def register_catalogue_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    def service_or_404(connection, tenant_id: uuid.UUID, service_id: uuid.UUID):
        row = connection.execute(
            text("""SELECT * FROM services WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": str(tenant_id), "id": str(service_id)}).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "service_not_found"})
        return row

    def serialize(row) -> dict:
        data = dict(row)
        for key in ("created_at", "updated_at", "archived_at"):
            if data.get(key) is not None:
                data[key] = data[key].isoformat()
        for key in ("id", "category_id"):
            if data.get(key) is not None:
                data[key] = str(data[key])
        return data

    @app.post("/v1/tenants/{tenant_id}/service-categories", tags=["catalogue"], status_code=201)
    def create_category(tenant_id: uuid.UUID, payload: CategoryIn, request: Request,
                        identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        category_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "services.manage")
                duplicate = connection.execute(
                    text("""SELECT 1 FROM service_categories WHERE tenant_id=:tenant AND name=:name"""),
                    {"tenant": str(tenant_id), "name": payload.name}).scalar_one_or_none()
                if duplicate is not None:
                    raise HTTPException(status_code=409, detail={"code": "category_name_in_use"})
                connection.execute(
                    text("""INSERT INTO service_categories (tenant_id,id,name)
                            VALUES (:tenant,:id,:name)"""),
                    {"tenant": str(tenant_id), "id": str(category_id), "name": payload.name})
                insert_audit(connection, tenant_id, identity, action="service_category.created",
                             resource_type="service_category", resource_id=category_id,
                             reason="service category created", request_id=request.state.request_id,
                             changed_fields=["name"])
            return {"id": str(category_id), "name": payload.name, "version": 1}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/service-categories", tags=["catalogue"])
    def list_categories(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "services.read")
                rows = connection.execute(
                    text("""SELECT id::text AS id, name, display_order, version, archived_at
                            FROM service_categories WHERE tenant_id=:tenant
                            ORDER BY display_order, name"""),
                    {"tenant": str(tenant_id)}).mappings().all()
            return {"items": [serialize(r) for r in rows]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/services", tags=["catalogue"], status_code=201)
    def create_service(tenant_id: uuid.UUID, payload: ServiceIn, request: Request,
                       identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        service_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "services.manage")
                if payload.category_id is not None:
                    owned = connection.execute(
                        text("SELECT 1 FROM service_categories WHERE tenant_id=:tenant AND id=:id"),
                        {"tenant": str(tenant_id), "id": str(payload.category_id)}).scalar_one_or_none()
                    if owned is None:
                        raise HTTPException(status_code=404, detail={"code": "category_not_found"})
                connection.execute(
                    text("""INSERT INTO services
                            (tenant_id,id,category_id,name,description,duration_minutes,
                             pre_buffer_minutes,post_buffer_minutes,price_minor,
                             rebooking_interval_days,requires_resource)
                            VALUES (:tenant,:id,:category,:name,:description,:duration,
                                    :pre,:post,:price,:rebooking,:resource)"""),
                    {"tenant": str(tenant_id), "id": str(service_id),
                     "category": str(payload.category_id) if payload.category_id else None,
                     "name": payload.name, "description": payload.description,
                     "duration": payload.duration_minutes, "pre": payload.pre_buffer_minutes,
                     "post": payload.post_buffer_minutes, "price": payload.price_minor,
                     "rebooking": payload.rebooking_interval_days,
                     "resource": payload.requires_resource})
                insert_audit(connection, tenant_id, identity, action="service.created",
                             resource_type="service", resource_id=service_id,
                             reason=f"service {payload.name} created",
                             request_id=request.state.request_id,
                             changed_fields=["name", "duration_minutes", "price_minor"])
                row = service_or_404(connection, tenant_id, service_id)
            return serialize(row)
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/services", tags=["catalogue"])
    def list_services(tenant_id: uuid.UUID, identity=Depends(require_identity),
                      include_archived: bool = False) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "services.read")
                where = "tenant_id=:tenant" if include_archived else "tenant_id=:tenant AND archived_at IS NULL"
                rows = connection.execute(
                    text(f"""SELECT id::text AS id, category_id::text AS category_id, name, description,
                                    duration_minutes, pre_buffer_minutes, post_buffer_minutes,
                                    price_minor, currency, rebooking_interval_days, requires_resource,
                                    version, archived_at, created_at, updated_at
                             FROM services WHERE {where} ORDER BY name"""),
                    {"tenant": str(tenant_id)}).mappings().all()
            return {"items": [serialize(r) for r in rows]}
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/services/{service_id}", tags=["catalogue"])
    def update_service(tenant_id: uuid.UUID, service_id: uuid.UUID, payload: ServicePatch, request: Request,
                       identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "services.manage")
                current = connection.execute(
                    text("SELECT * FROM services WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                    {"tenant": str(tenant_id), "id": str(service_id)}).mappings().one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "service_not_found"})
                if payload.expected_version != current["version"]:
                    raise HTTPException(status_code=409, detail={"code": "service_version_conflict"})
                changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
                if not changes:
                    raise HTTPException(status_code=422, detail={"code": "service_change_required"})
                if "category_id" in changes and changes["category_id"] is not None:
                    owned = connection.execute(
                        text("SELECT 1 FROM service_categories WHERE tenant_id=:tenant AND id=:id"),
                        {"tenant": str(tenant_id), "id": str(changes["category_id"])}).scalar_one_or_none()
                    if owned is None:
                        raise HTTPException(status_code=404, detail={"code": "category_not_found"})
                    changes["category_id"] = str(changes["category_id"])
                assignments = ", ".join(f"{key}=:{key}" for key in changes)
                updated = connection.execute(
                    text(f"""UPDATE services SET {assignments}, version=version+1, updated_at=now()
                             WHERE tenant_id=:tenant AND id=:id RETURNING *"""),
                    {"tenant": str(tenant_id), "id": str(service_id), **changes}).mappings().one()
                insert_audit(connection, tenant_id, identity, action="service.updated",
                             resource_type="service", resource_id=service_id,
                             reason="service updated", request_id=request.state.request_id,
                             changed_fields=list(changes.keys()))
            return serialize(updated)
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/services/{service_id}/archive", tags=["catalogue"])
    def archive_service(tenant_id: uuid.UUID, service_id: uuid.UUID, request: Request,
                        identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "services.manage")
                row = service_or_404(connection, tenant_id, service_id)
                if row["archived_at"] is not None:
                    return {**serialize(row), "already_archived": True}
                # Archiving is a stamp plus a version bump: appointment history that names
                # this service must keep resolving, so the row is never deleted (FR-019).
                updated = connection.execute(
                    text("""UPDATE services SET archived_at=now(), version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id RETURNING *"""),
                    {"tenant": str(tenant_id), "id": str(service_id)}).mappings().one()
                insert_audit(connection, tenant_id, identity, action="service.archived",
                             resource_type="service", resource_id=service_id,
                             reason="service archived", request_id=request.state.request_id,
                             changed_fields=["archived_at"])
            return serialize(updated)
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/services/{service_id}/restore", tags=["catalogue"])
    def restore_service(tenant_id: uuid.UUID, service_id: uuid.UUID, request: Request,
                        identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "services.manage")
                service_or_404(connection, tenant_id, service_id)
                updated = connection.execute(
                    text("""UPDATE services SET archived_at=NULL, version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id RETURNING *"""),
                    {"tenant": str(tenant_id), "id": str(service_id)}).mappings().one()
                insert_audit(connection, tenant_id, identity, action="service.restored",
                             resource_type="service", resource_id=service_id,
                             reason="service restored", request_id=request.state.request_id,
                             changed_fields=["archived_at"])
            return serialize(updated)
        finally:
            engine.dispose()

    @app.put("/v1/tenants/{tenant_id}/services/{service_id}/overrides", tags=["catalogue"])
    def upsert_override(tenant_id: uuid.UUID, service_id: uuid.UUID, payload: OverrideIn, request: Request,
                        identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "services.manage")
                service_or_404(connection, tenant_id, service_id)
                row = connection.execute(
                    text("""INSERT INTO service_staff_overrides
                            (tenant_id,id,service_id,staff_ref,duration_minutes,price_minor)
                            VALUES (:tenant,:id,:service,:staff,:duration,:price)
                            ON CONFLICT (tenant_id, service_id, staff_ref)
                            DO UPDATE SET duration_minutes=EXCLUDED.duration_minutes,
                                          price_minor=EXCLUDED.price_minor,
                                          version=service_staff_overrides.version+1,
                                          updated_at=now()
                            RETURNING *"""),
                    {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "service": str(service_id),
                     "staff": str(payload.staff_ref), "duration": payload.duration_minutes,
                     "price": payload.price_minor}).mappings().one()
                insert_audit(connection, tenant_id, identity, action="service_override.set",
                             resource_type="service", resource_id=service_id,
                             reason="staff override set", request_id=request.state.request_id,
                             changed_fields=["staff_ref", "duration_minutes", "price_minor"])
            return serialize(row)
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/services/{service_id}/overrides", tags=["catalogue"])
    def list_overrides(tenant_id: uuid.UUID, service_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "services.read")
                service_or_404(connection, tenant_id, service_id)
                rows = connection.execute(
                    text("""SELECT id::text AS id, staff_ref::text AS staff_ref, duration_minutes,
                                   price_minor, version FROM service_staff_overrides
                            WHERE tenant_id=:tenant AND service_id=:service ORDER BY staff_ref"""),
                    {"tenant": str(tenant_id), "service": str(service_id)}).mappings().all()
            return {"items": [dict(r) for r in rows]}
        finally:
            engine.dispose()