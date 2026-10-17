"""Controlled duplicate merge (FR-015, TASK-013).

Two properties define this feature and are enforced here, not in the UI:

- **A shared contact never merges anything by itself.** A duplicate found by value is a
  *candidate* (010_client_contacts.sql); the merge only runs from an explicit operator
  request naming both clients. `preview` never writes.
- **A merge is recorded, not just performed.** `merge_records.manifest` lists every row
  that moved, so the act can be inspected and, where the moved rows are still identifiable,
  reversed. Consent is never promoted by a merge: merging two clients does not make one
  client's proof apply to the other's contacts (data-model.md:66, QA-20).

The module takes its dependencies by injection for the same reason `crm.main` does.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

# Which related tables move with the merged client. Each is (table, column, manifest key).
_REASSIGNMENTS = (
    ("client_contacts", "client_id", "contacts"),
    ("visit_notes", "client_id", "visit_notes"),
    ("client_preferences", "client_id", "preferences"),
    ("import_rows", "client_id", "import_rows"),
    # Operational and commercial history must remain visible on the survivor.
    ("appointments", "client_id", "appointments"),
    ("visits", "client_id", "visits"),
    ("salon_receipt_entries", "client_id", "receipts"),
    ("communications", "client_id", "communications"),
    ("closure_impacts", "client_id", "closure_impacts"),
    ("follow_up_tasks", "client_id", "follow_up_tasks"),
    ("recurring_series", "client_id", "recurring_series"),
    ("waitlist_entries", "client_id", "waitlist_entries"),
)
# Client field values are keyed by (entity_type, entity_id), not client_id.
_FIELD_VALUE_TABLE = ("client_field_values", "entity_id")


class MergePreviewIn(BaseModel):
    survivor_client_id: uuid.UUID
    merged_client_id: uuid.UUID


class MergeCommitIn(BaseModel):
    survivor_client_id: uuid.UUID
    merged_client_id: uuid.UUID
    expected_survivor_version: int = Field(ge=1)
    expected_merged_version: int = Field(ge=1)
    # Conflicts the operator saw and accepted. A commit that leaves a listed conflict
    # unacknowledged is refused, so a conflict cannot be merged by inattention.
    acknowledged_conflicts: bool = False
    idempotency_key: str = Field(min_length=8, max_length=64)


def register_merge_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
) -> None:
    def client_row_or_404(connection, tenant_id: uuid.UUID, client_id: uuid.UUID):
        row = connection.execute(
            text("""SELECT id::text AS id, name, preferred_name, lifecycle, version
                    FROM clients WHERE tenant_id=:tenant AND id=:id AND deleted_at IS NULL"""),
            {"tenant": str(tenant_id), "id": str(client_id)}).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "client_not_found"})
        return row

    def collect(connection, tenant_id: uuid.UUID, survivor_id: uuid.UUID, merged_id: uuid.UUID) -> dict:
        manifest: dict[str, list[str]] = {}
        for table, column, key in _REASSIGNMENTS:
            ids = connection.execute(
                text(f"SELECT id::text FROM {table} WHERE tenant_id=:tenant AND {column}=:client"),
                {"tenant": str(tenant_id), "client": str(merged_id)}).scalars().all()
            manifest[key] = list(ids)
        field_values = connection.execute(
            text("""SELECT id::text AS id, definition_id::text AS definition_id
                    FROM client_field_values WHERE tenant_id=:tenant
                      AND entity_type='client' AND entity_id=:client"""),
            {"tenant": str(tenant_id), "client": str(merged_id)}).mappings().all()
        manifest["field_values"] = [row["id"] for row in field_values]

        # Contacts and typed fields can collide on the survivor. Keep the merged-side
        # value attached to its retired subject; never silently replace the survivor.
        conflicts = []
        for contact in connection.execute(
                text("""SELECT id::text AS id, kind, normalized_value FROM client_contacts
                        WHERE tenant_id=:tenant AND client_id=:client"""),
                {"tenant": str(tenant_id), "client": str(merged_id)}).mappings().all():
            clash = connection.execute(
                text("""SELECT id::text FROM client_contacts WHERE tenant_id=:tenant
                        AND client_id=:survivor AND kind=:kind AND normalized_value=:value"""),
                {"tenant": str(tenant_id), "survivor": str(survivor_id),
                 "kind": contact["kind"], "value": contact["normalized_value"]}).mappings().one_or_none()
            if clash is not None:
                conflicts.append({"kind": contact["kind"], "row_id": contact["id"],
                                  "resolution": "survivor_contact_kept"})
        for value in field_values:
            clash = connection.execute(
                text("""SELECT 1 FROM client_field_values
                        WHERE tenant_id=:tenant AND entity_type='client'
                          AND entity_id=:survivor AND definition_id=:definition"""),
                {"tenant": str(tenant_id), "survivor": str(survivor_id),
                 "definition": value["definition_id"]}).scalar_one_or_none()
            if clash is not None:
                conflicts.append({"kind": "custom_field", "row_id": value["id"],
                                  "resolution": "survivor_value_kept"})
        manifest["retained"] = {"contacts": [], "field_values": []}
        # These rows describe the original subject and must not be inherited when
        # records consolidate. Keep their identifiers in the receipt for auditability.
        for table in ("consents", "suppressions", "privacy_requests", "retention_holds"):
            manifest["retained"][table] = list(connection.execute(
                text(f"SELECT id::text FROM {table} WHERE tenant_id=:tenant AND client_id=:client"),
                {"tenant": str(tenant_id), "client": str(merged_id)}).scalars().all())
        return {"manifest": manifest, "conflicts": conflicts}

    @app.post("/v1/tenants/{tenant_id}/client-merges/preview", tags=["crm"])
    def preview_merge(tenant_id: uuid.UUID, payload: MergePreviewIn,
                      identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.merge")
                if payload.survivor_client_id == payload.merged_client_id:
                    raise HTTPException(status_code=422, detail={"code": "same_client"})
                survivor = client_row_or_404(connection, tenant_id, payload.survivor_client_id)
                merged = client_row_or_404(connection, tenant_id, payload.merged_client_id)
                result = collect(connection, tenant_id, payload.survivor_client_id, payload.merged_client_id)
            return {"survivor": dict(survivor), "merged": dict(merged), **result}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/client-merges/commit", tags=["crm"])
    def commit_merge(tenant_id: uuid.UUID, payload: MergeCommitIn, request: Request,
                     identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.merge")
                if payload.survivor_client_id == payload.merged_client_id:
                    raise HTTPException(status_code=422, detail={"code": "same_client"})

                # Idempotency first: a retry of an applied merge returns its receipt and
                # must not attempt a second, now-impossible, merge (QA-10).
                existing = connection.execute(
                    text("""SELECT id::text AS id, manifest, conflicts, status FROM merge_records
                            WHERE tenant_id=:tenant AND survivor_client_id=:survivor
                              AND merged_client_id=:merged AND status='applied'
                              AND manifest->>'idempotency_key'=:key
                            ORDER BY performed_at DESC LIMIT 1"""),
                    {"tenant": str(tenant_id), "survivor": str(payload.survivor_client_id),
                     "merged": str(payload.merged_client_id), "key": payload.idempotency_key}).mappings().one_or_none()
                if existing is not None:
                    return {"merges": [existing["id"]], "manifest": existing["manifest"],
                            "conflicts": existing["conflicts"], "replayed": True}

                # Lock both profiles in a stable order so parallel merges cannot race
                # through version checks or retire a profile already claimed elsewhere.
                locked = connection.execute(
                    text("""SELECT id::text AS id, name, preferred_name, lifecycle, version, deleted_at
                            FROM clients WHERE tenant_id=:tenant AND id IN (:survivor,:merged)
                            ORDER BY id FOR UPDATE"""),
                    {"tenant": str(tenant_id), "survivor": str(payload.survivor_client_id),
                     "merged": str(payload.merged_client_id)}).mappings().all()
                by_id = {row["id"]: row for row in locked}
                survivor = by_id.get(str(payload.survivor_client_id))
                merged = by_id.get(str(payload.merged_client_id))
                # A same-key retry may have passed the first receipt check while the
                # winning transaction was still in flight. The client locks serialize
                # that race; now observe and replay the winner before evaluating tombstones.
                raced_replay = connection.execute(
                    text("""SELECT id::text AS id, manifest, conflicts FROM merge_records
                            WHERE tenant_id=:tenant AND survivor_client_id=:survivor
                              AND merged_client_id=:merged AND status='applied'
                              AND manifest->>'idempotency_key'=:key
                            ORDER BY performed_at DESC LIMIT 1"""),
                    {"tenant": str(tenant_id), "survivor": str(payload.survivor_client_id),
                     "merged": str(payload.merged_client_id), "key": payload.idempotency_key}).mappings().one_or_none()
                if raced_replay is not None:
                    return {"merges": [raced_replay["id"]], "manifest": raced_replay["manifest"],
                            "conflicts": raced_replay["conflicts"], "replayed": True}
                if survivor is None or survivor["deleted_at"] is not None:
                    raise HTTPException(status_code=404, detail={"code": "client_not_found"})
                if merged is None or merged["deleted_at"] is not None:
                    raise HTTPException(status_code=404, detail={"code": "client_not_found"})
                if payload.expected_survivor_version != survivor["version"] or \
                        payload.expected_merged_version != merged["version"]:
                    raise HTTPException(status_code=409, detail={"code": "client_version_conflict"})

                result = collect(connection, tenant_id, payload.survivor_client_id, payload.merged_client_id)
                if result["conflicts"] and not payload.acknowledged_conflicts:
                    raise HTTPException(status_code=409, detail={
                        "code": "merge_conflicts_unacknowledged", "conflicts": result["conflicts"]})

                manifest = result["manifest"]
                manifest["idempotency_key"] = payload.idempotency_key
                retained_contact_ids = {item["row_id"] for item in result["conflicts"]
                                        if item["kind"] in ("email", "phone")}
                for table, column, key in _REASSIGNMENTS:
                    if manifest[key]:
                        if key == "contacts" and retained_contact_ids:
                            manifest["retained"]["contacts"] = sorted(retained_contact_ids)
                            manifest[key] = [row_id for row_id in manifest[key]
                                             if row_id not in retained_contact_ids]
                        connection.execute(
                            text(f"UPDATE {table} SET {column}=:survivor "
                                 f"WHERE tenant_id=:tenant AND {column}=:merged" +
                                 (" AND id::text = ANY(:ids)" if key == "contacts" and retained_contact_ids else "")),
                            {"survivor": str(payload.survivor_client_id), "tenant": str(tenant_id),
                             "merged": str(payload.merged_client_id),
                             **({"ids": manifest[key]} if key == "contacts" and retained_contact_ids else {})})
                # Field values are unique per (definition, entity); a value the survivor
                # already holds cannot move and is dropped from the manifest to keep it
                # an accurate record of what actually happened.
                moved_values = []
                for value_id in manifest["field_values"]:
                    clash = connection.execute(
                        text("""SELECT 1 FROM client_field_values v
                                JOIN client_field_values s
                                  ON s.tenant_id=v.tenant_id AND s.definition_id=v.definition_id
                                 AND s.entity_type=v.entity_type AND s.entity_id=:survivor
                                WHERE v.tenant_id=:tenant AND v.id=:id"""),
                        {"tenant": str(tenant_id), "id": value_id,
                         "survivor": str(payload.survivor_client_id)}).scalar_one_or_none()
                    if clash is None:
                        connection.execute(
                            text("""UPDATE client_field_values SET entity_id=:survivor
                                    WHERE tenant_id=:tenant AND id=:id"""),
                            {"survivor": str(payload.survivor_client_id), "tenant": str(tenant_id),
                             "id": value_id})
                        moved_values.append(value_id)
                    else:
                        manifest["retained"]["field_values"].append(value_id)
                manifest["field_values"] = moved_values

                # The surviving profile's aggregate changed when linked records moved;
                # advance its optimistic-concurrency version in the same transaction.
                connection.execute(
                    text("UPDATE clients SET version=version+1 WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(payload.survivor_client_id)})

                merge_id = uuid.uuid4()
                connection.execute(
                    text("""INSERT INTO merge_records
                            (tenant_id,id,survivor_client_id,merged_client_id,manifest,conflicts,
                             acknowledged_conflicts,performed_by_issuer,performed_by_subject)
                            VALUES (:tenant,:id,:survivor,:merged,:manifest,:conflicts,
                                    :acknowledged,:issuer,:subject)"""),
                    {"tenant": str(tenant_id), "id": str(merge_id),
                     "survivor": str(payload.survivor_client_id), "merged": str(payload.merged_client_id),
                     "manifest": json.dumps(manifest), "conflicts": json.dumps(result["conflicts"]),
                     "acknowledged": payload.acknowledged_conflicts,
                     "issuer": identity.issuer, "subject": identity.subject})
                # The merged client is retired, not deleted: history and the manifest must
                # remain readable, and a restore must be able to replay the merge.
                connection.execute(
                    text("""UPDATE clients SET deleted_at=now(), version=version+1
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(payload.merged_client_id)})
                connection.execute(
                    text("""UPDATE client_duplicate_candidates
                            SET resolution='merged', resolved_at=now()
                            WHERE tenant_id=:tenant AND resolved_at IS NULL
                              AND ((client_id=:survivor AND other_client_id=:merged)
                                OR (client_id=:merged AND other_client_id=:survivor))"""),
                    {"tenant": str(tenant_id), "survivor": str(payload.survivor_client_id),
                     "merged": str(payload.merged_client_id)})
                insert_audit(connection, tenant_id, identity, action="client.merged",
                             resource_type="merge_record", resource_id=merge_id,
                             reason=f"merged client {payload.merged_client_id} into {payload.survivor_client_id}",
                             request_id=request.state.request_id,
                             changed_fields=["survivor_client_id", "merged_client_id", "manifest"])
            return {"merges": [str(merge_id)], "replayed": False,
                    "manifest": manifest, "conflicts": result["conflicts"]}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/client-merges", tags=["crm"])
    def list_merges(tenant_id: uuid.UUID, identity=Depends(require_identity),
                    limit: int = 20, offset: int = 0) -> dict:
        engine = app.state.engine_factory()
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail={"code": "invalid_limit"})
        if offset < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_offset"})
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.read")
                total = connection.execute(
                    text("SELECT count(*) FROM merge_records WHERE tenant_id=:tenant"),
                    {"tenant": str(tenant_id)}).scalar_one()
                rows = connection.execute(
                    text("""SELECT id::text AS id, survivor_client_id::text AS survivor_client_id,
                                   merged_client_id::text AS merged_client_id, status,
                                   acknowledged_conflicts, performed_at
                            FROM merge_records WHERE tenant_id=:tenant
                            ORDER BY performed_at DESC LIMIT :limit OFFSET :offset"""),
                    {"tenant": str(tenant_id), "limit": limit, "offset": offset}).mappings().all()
            return {"items": [{**dict(r), "performed_at": r["performed_at"].isoformat()} for r in rows],
                    "total": total, "limit": limit, "offset": offset}
        finally:
            engine.dispose()
