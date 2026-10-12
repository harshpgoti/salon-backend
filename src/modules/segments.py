"""Audience segments over consenting clients (FR-053; TASK-038).

A segment is a named, versioned JSON filter AST. The operator set is fixed —
``and``/``or``/``not`` plus ``service``, ``location``, ``tag``,
``min_spend_minor``, ``max_spend_minor`` and ``inactive_days`` — validated
recursively at write time (depth and node caps included). Anything else is a
422: an invalid AST never resolves, so it can never silently widen to the
whole client list.

- AC-1: history (visits), service (completed lines), tags, home location and
  recorded net spend (payments + adjustments − refunds) filter the tenant's
  clients; resolution is tenant-scoped by construction.
- AC-2: audiences resolve at read time with marketing consent and
  suppression applied then. Shared household contacts never merge identities
  and never inflate counts: the preview reports distinct clients, distinct
  reachable numbers, and how many clients share a number.

Campaign execution (TASK-039) calls :func:`resolve_segment`; the preview
route shows the same numbers an approval would lock.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

__all__ = [
    "SegmentModuleError",
    "register_segment_routes",
    "resolve_segment",
    "validate_segment_ast",
]

MAX_AST_DEPTH = 5
MAX_AST_NODES = 20

_LEAF_KEYS = frozenset({
    "service", "location", "tag", "min_spend_minor", "max_spend_minor",
    "inactive_days",
})


class SegmentModuleError(Exception):
    """Base exception for segment operations."""


def validate_segment_ast(node, *, depth: int = 0, counter: list | None = None) -> None:
    """Reject anything outside the fixed operator set (AC-2).

    Raises :class:`SegmentModuleError` on unknown operators, wrong shapes,
    bad uuids, negative money, or runaway depth/breadth. Valid nodes pass
    silently; the resolved SQL is built from this same whitelist, so checked
    here means safe there.
    """
    if counter is None:
        counter = [0]
    counter[0] += 1
    if counter[0] > MAX_AST_NODES:
        raise SegmentModuleError("segment filter is too large")
    if depth > MAX_AST_DEPTH:
        raise SegmentModuleError("segment filter nests too deeply")
    if not isinstance(node, dict) or len(node) != 1:
        raise SegmentModuleError("each filter node must be a single-key object")
    op, value = next(iter(node.items()))
    if op in ("and", "or"):
        if not isinstance(value, list) or not value:
            raise SegmentModuleError(f"{op} needs a non-empty list")
        for child in value:
            validate_segment_ast(child, depth=depth + 1, counter=counter)
    elif op == "not":
        validate_segment_ast(value, depth=depth + 1, counter=counter)
    elif op in ("service", "location"):
        try:
            uuid.UUID(str(value))
        except ValueError:
            raise SegmentModuleError(f"{op} must be a UUID") from None
    elif op == "tag":
        if not isinstance(value, str) or not (1 <= len(value.strip()) <= 40):
            raise SegmentModuleError("tag must be 1-40 characters")
    elif op in ("min_spend_minor", "max_spend_minor"):
        if not isinstance(value, int) or value < 0:
            raise SegmentModuleError(f"{op} must be a non-negative integer")
    elif op == "inactive_days":
        if not isinstance(value, int) or not (1 <= value <= 3650):
            raise SegmentModuleError("inactive_days must be 1-3650")
    else:
        raise SegmentModuleError(f"unknown filter operator {op!r}")


def _build_where(node, params: dict, seed: list) -> str:
    """Compile a validated AST to parameterized SQL over alias ``c`` (clients)."""
    op, value = next(iter(node.items()))
    if op == "and":
        return "(" + " AND ".join(_build_where(kid, params, seed) for kid in value) + ")"
    if op == "or":
        return "(" + " OR ".join(_build_where(kid, params, seed) for kid in value) + ")"
    if op == "not":
        return "(NOT " + _build_where(value, params, seed) + ")"
    key = f"p{len(seed)}"
    seed.append(key)
    if op == "service":
        params[key] = str(value)
        return (f"EXISTS (SELECT 1 FROM visit_services vs "
                f"JOIN visits v ON v.tenant_id=vs.tenant_id AND v.id=vs.visit_id "
                f"WHERE vs.tenant_id=c.tenant_id AND v.client_id=c.id "
                f"AND vs.service_id=:{key})")
    if op == "location":
        params[key] = str(value)
        return f"c.preferred_location_id = :{key}"
    if op == "tag":
        params[key] = value.strip().lower()
        return (f"EXISTS (SELECT 1 FROM client_tags t "
                f"WHERE t.tenant_id=c.tenant_id AND t.client_id=c.id AND t.tag=:{key})")
    if op == "min_spend_minor":
        params[key] = value
        return (f"(SELECT COALESCE(SUM(CASE WHEN e.kind='refund' THEN -e.amount_minor "
                f"ELSE e.amount_minor END), 0) FROM salon_receipt_entries e "
                f"WHERE e.tenant_id=c.tenant_id AND e.client_id=c.id) >= :{key}")
    if op == "max_spend_minor":
        params[key] = value
        return (f"(SELECT COALESCE(SUM(CASE WHEN e.kind='refund' THEN -e.amount_minor "
                f"ELSE e.amount_minor END), 0) FROM salon_receipt_entries e "
                f"WHERE e.tenant_id=c.tenant_id AND e.client_id=c.id) <= :{key}")
    if op == "inactive_days":
        params[key] = value
        return (f"NOT EXISTS (SELECT 1 FROM visits v "
                f"WHERE v.tenant_id=c.tenant_id AND v.client_id=c.id "
                f"AND v.completed_at > now() - (:{key} || ' days')::interval)")
    raise SegmentModuleError(f"unknown filter operator {op!r}")  # unreachable post-validation


def resolve_segment(connection, *, tenant_id: str, segment_id: str,
                    channel: str) -> dict:
    """Resolve an audience: eligible clients plus the exclusion breakdown.

    Candidates come from the validated AST (tenant-scoped, distinct). Each
    candidate then passes marketing consent and suppression for the channel;
    failures are counted as exclusions, never silently included. Shared
    contact values are reported (``shared_numbers``) without merging or
    double-counting anyone.
    """
    from modules.communications.consent import check_consented, check_suppressed
    if channel not in ("email", "sms", "whatsapp"):
        raise SegmentModuleError(f"unknown channel {channel!r}")
    row = connection.execute(
        text("""SELECT definition FROM client_segments
                WHERE tenant_id=:tenant AND id=:id"""),
        {"tenant": tenant_id, "id": segment_id}).mappings().one_or_none()
    if row is None:
        raise SegmentModuleError("unknown segment")
    validate_segment_ast(row["definition"])
    params: dict = {"tenant": tenant_id}
    where = _build_where(row["definition"], params, [])
    candidates = connection.execute(
        text(f"""SELECT c.id::text AS client_id FROM clients c
                 WHERE c.tenant_id=:tenant AND c.deleted_at IS NULL AND {where}
                 ORDER BY c.created_at"""),
        params).mappings().all()
    client_ids = [r["client_id"] for r in candidates]
    eligible: list[str] = []
    exclusions = {"no_consent": 0, "suppressed": 0, "unreachable": 0}
    numbers: dict[str, int] = {}
    for client_id in client_ids:
        contact = connection.execute(
            text("""SELECT kind, normalized_value FROM client_contacts
                    WHERE tenant_id=:tenant AND client_id=:client
                    ORDER BY is_primary DESC, created_at LIMIT 1"""),
            {"tenant": tenant_id, "client": client_id}).mappings().one_or_none()
        if contact is None:
            exclusions["unreachable"] += 1
            continue
        if not check_consented(connection, tenant_id=tenant_id, client_id=client_id,
                               channel=channel, purpose="marketing"):
            exclusions["no_consent"] += 1
            continue
        if check_suppressed(connection, tenant_id=tenant_id,
                            contact_value=contact["normalized_value"], channel=channel):
            exclusions["suppressed"] += 1
            continue
        eligible.append(client_id)
        numbers[contact["normalized_value"]] = numbers.get(contact["normalized_value"], 0) + 1
    return {"segment_id": segment_id, "channel": channel,
            "candidates": len(client_ids), "eligible": eligible,
            "eligible_count": len(eligible),
            "reachable_numbers": len(numbers),
            "shared_numbers": sum(1 for n in numbers.values() if n > 1),
            "exclusions": exclusions}


class SegmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    definition: dict


class TagIn(BaseModel):
    tag: str = Field(min_length=1, max_length=40)


def register_segment_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    @app.post("/v1/tenants/{tenant_id}/segments", tags=["retention"], status_code=201)
    def create_segment(tenant_id: uuid.UUID, payload: SegmentIn,
                       request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.manage")
                try:
                    validate_segment_ast(payload.definition)
                except SegmentModuleError as exc:
                    raise HTTPException(status_code=422, detail={
                        "code": "invalid_segment", "message": str(exc)})
                try:
                    row = connection.execute(
                        text("""INSERT INTO client_segments (tenant_id, id, name, definition)
                                VALUES (:tenant, :id, :name, :definition)
                                RETURNING id::text AS id, version"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()),
                         "name": payload.name.strip(),
                         "definition": json.dumps(payload.definition)},
                    ).mappings().one()
                except IntegrityError:
                    raise HTTPException(status_code=409, detail={"code": "segment_name_taken"})
                insert_audit(connection, tenant_id, identity, action="retention.segment_created",
                             resource_type="client_segment", resource_id=row["id"],
                             reason=payload.name.strip(),
                             request_id=request.state.request_id,
                             changed_fields=["name", "definition"])
            return dict(row)
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/segments/{segment_id}/preview", tags=["retention"])
    def preview_segment(tenant_id: uuid.UUID, segment_id: uuid.UUID, payload: dict,
                        identity=Depends(require_identity)) -> dict:
        channel = payload.get("channel", "email")
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.manage")
                try:
                    result = resolve_segment(connection, tenant_id=str(tenant_id),
                                             segment_id=str(segment_id), channel=channel)
                except SegmentModuleError as exc:
                    raise HTTPException(status_code=422, detail={
                        "code": "segment_unresolvable", "message": str(exc)})
            result["eligible"] = len(result["eligible"])
            return result
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/segments", tags=["retention"])
    def list_segments(tenant_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "retention.manage")
                rows = connection.execute(
                    text("""SELECT id::text AS id, name, version FROM client_segments
                            WHERE tenant_id=:tenant ORDER BY name"""),
                    {"tenant": str(tenant_id)}).mappings().all()
            return {"items": [dict(r) for r in rows]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/clients/{client_id}/tags", tags=["retention"], status_code=201)
    def add_tag(tenant_id: uuid.UUID, client_id: uuid.UUID, payload: TagIn,
                request: Request, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                exists = connection.execute(
                    text("SELECT 1 FROM clients WHERE tenant_id=:t AND id=:c"),
                    {"t": str(tenant_id), "c": str(client_id)}).scalar_one_or_none()
                if exists is None:
                    raise HTTPException(status_code=404, detail={"code": "client_not_found"})
                tag = payload.tag.strip().lower()
                try:
                    connection.execute(
                        text("""INSERT INTO client_tags (tenant_id, client_id, tag)
                                VALUES (:t, :c, :tag) ON CONFLICT DO NOTHING"""),
                        {"t": str(tenant_id), "c": str(client_id), "tag": tag})
                except IntegrityError as exc:
                    raise HTTPException(status_code=422, detail={"code": "invalid_tag"})
                insert_audit(connection, tenant_id, identity, action="client.tag_added",
                             resource_type="client", resource_id=client_id,
                             reason=tag, request_id=request.state.request_id,
                             changed_fields=["tags"])
            return {"client_id": str(client_id), "tag": tag}
        finally:
            engine.dispose()
