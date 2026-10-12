from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine


class TenantContextError(RuntimeError):
    """Raised when a query attempts to use absent or conflicting tenant scope."""


def set_tenant_context(connection: Connection, tenant_id: UUID | str) -> str:
    canonical_id = str(UUID(str(tenant_id)))
    current = connection.execute(text("SELECT nullif(current_setting('app.tenant_id', true), '')")).scalar_one_or_none()
    if current is not None and current != canonical_id:
        raise TenantContextError("transaction is already scoped to a different tenant")
    connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": canonical_id})
    return canonical_id


@contextmanager
def tenant_transaction(engine: Engine, tenant_id: UUID | str) -> Iterator[Connection]:
    with engine.begin() as connection:
        set_tenant_context(connection, tenant_id)
        yield connection


def list_tenant_locations(connection: Connection, tenant_id: UUID | str) -> list[dict[str, str]]:
    canonical_id = str(UUID(str(tenant_id)))
    current = connection.execute(text("SELECT nullif(current_setting('app.tenant_id', true), '')")).scalar_one_or_none()
    if current != canonical_id:
        raise TenantContextError("location query requires a matching transaction tenant")
    rows = connection.execute(
        text("""SELECT id::text AS id, slug, booking_slug, display_name, timezone
               FROM locations WHERE tenant_id=:tenant_id ORDER BY created_at"""),
        {"tenant_id": canonical_id},
    ).mappings().all()
    return [dict(row) for row in rows]
