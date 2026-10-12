"""CSV client import: preview, provenance and resumable commit (FR-014, TASK-012).

The seam is the import job, not the file: a preview parses and validates without writing a
client, and a commit writes only rows the operator already reviewed. Two properties matter
more than convenience and are enforced here rather than in the UI:

- A row without consent proof is imported but never becomes marketing-eligible
  (data-model.md:66, QA-15). Absence of evidence is not permission.
- A cell starting with a spreadsheet formula lead-in is escaped before it is ever echoed
  back into a downloadable file (FR-014), so an import cannot become an attack on the
  operator's own spreadsheet.

Dependencies are injected for the same reason the client routes inject them: this module
owns its domain and does not import the identity app.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text

# Formula lead-ins a spreadsheet would execute if the value were written unescaped
# (FR-014, QA-20). Leading whitespace is stripped first, because " =1+1" still executes.
_FORMULA_LEADINS = ("=", "+", "-", "@")
MAX_IMPORT_ROWS = 5000
MAX_CELL_LENGTH = 320
# Column names an operator may map to. Anything else in the header is ignored, not an error.
MAPPABLE_FIELDS = ("name", "preferred_name", "email", "phone", "lifecycle", "locale", "consent_proof")


def escape_spreadsheet_cell(value: str) -> str:
    """Neutralize a formula lead-in by prefixing a single quote (FR-014)."""
    stripped = value.lstrip()
    if stripped[:1] in _FORMULA_LEADINS:
        return "'" + value
    return value


def parse_csv(text_content: str) -> tuple[list[str], list[list[str]]]:
    """Read a CSV into (header, rows). Rejects a file with no header."""
    reader = csv.reader(io.StringIO(text_content))
    try:
        header = next(reader)
    except StopIteration:
        raise HTTPException(status_code=422, detail={"code": "import_file_empty"})
    header = [escape_spreadsheet_cell(cell).strip() for cell in header]
    rows = [row for row in reader if any(cell.strip() for cell in row)]
    if len(rows) > MAX_IMPORT_ROWS:
        raise HTTPException(status_code=422, detail={"code": "import_too_many_rows", "max": MAX_IMPORT_ROWS})
    return header, rows


class ImportPreviewIn(BaseModel):
    filename: str = Field(min_length=1, max_length=240)
    # The file content is sent as text; the API never writes it to disk.
    content: str = Field(min_length=1, max_length=8_000_000)
    mapping: dict[str, str] = Field(default_factory=dict)


class ImportCommitIn(BaseModel):
    expected_version: int = Field(ge=1)
    # A commit is idempotent on this key: re-sending the same key returns the same receipt.
    idempotency_key: str = Field(min_length=8, max_length=64)


class ConsentIn(BaseModel):
    consent_proof: str = Field(min_length=1, max_length=240)
    marketing_eligible: bool


def _build_row(raw_cells: list[str], header: list[str], mapping: dict[str, str]) -> tuple[dict, list[str]]:
    """Map raw cells to normalized fields; return (normalized, error_codes)."""
    normalized: dict[str, object] = {}
    errors: list[str] = []
    by_index = {column: index for index, column in enumerate(header)}
    for target, column in mapping.items():
        if target not in MAPPABLE_FIELDS:
            errors.append("unknown_mapping_target")
            continue
        index = by_index.get(column)
        if index is None:
            errors.append("mapped_column_missing")
            continue
        value = escape_spreadsheet_cell(raw_cells[index].strip()) if index < len(raw_cells) else ""
        if len(value) > MAX_CELL_LENGTH:
            errors.append("value_too_long")
            continue
        if value:
            normalized[target] = value
    if not normalized.get("name"):
        errors.append("name_required")
    return normalized, errors


def register_import_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
    normalize_email: Callable[[str], str],
    normalize_phone: Callable[[str, str], str],
) -> None:
    def import_or_404(connection, tenant_id: uuid.UUID, import_id: uuid.UUID):
        row = connection.execute(
            text("""SELECT id::text AS id, filename, mapping, status, total_rows, valid_rows,
                           error_rows, duplicate_rows, committed_rows, version, created_at
                    FROM imports WHERE tenant_id=:tenant AND id=:id"""),
            {"tenant": str(tenant_id), "id": str(import_id)}).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "import_not_found"})
        return row

    @app.post("/v1/tenants/{tenant_id}/imports/preview", tags=["crm"], status_code=201)
    def preview_import(tenant_id: uuid.UUID, payload: ImportPreviewIn, request: Request,
                       identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        import_id = uuid.uuid4()
        try:
            header, rows = parse_csv(payload.content)
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                connection.execute(
                    text("""INSERT INTO imports (tenant_id,id,filename,mapping,status,total_rows,
                                                 created_by_issuer,created_by_subject)
                            VALUES (:tenant,:id,:filename,:mapping,'previewed',:total,:issuer,:subject)"""),
                    {"tenant": str(tenant_id), "id": str(import_id), "filename": payload.filename,
                     "mapping": json.dumps(payload.mapping), "total": len(rows),
                     "issuer": identity.issuer, "subject": identity.subject})
                valid = error_count = duplicate_count = 0
                for offset, raw_cells in enumerate(rows):
                    normalized, errors = _build_row(raw_cells, header, payload.mapping)
                    # Normalization is exercised during preview so the operator sees the
                    # same failures a commit would produce, not a surprise later.
                    contacts: list[tuple[str, str]] = []
                    if email := normalized.get("email"):
                        try:
                            contacts.append(("email", normalize_email(str(email))))
                        except ValueError:
                            errors.append("invalid_email")
                    if phone := normalized.get("phone"):
                        try:
                            contacts.append(("phone", normalize_phone(str(phone))))
                        except ValueError:
                            errors.append("invalid_phone")
                    if errors:
                        error_count += 1
                        status = "error"
                    else:
                        status = "valid"
                        valid += 1
                    # A duplicate is a candidate for review, never an automatic merge
                    # (data-model.md:32). The receipt counts them; it does not act.
                    for kind, value in contacts:
                        found = connection.execute(
                            text("""SELECT 1 FROM client_contacts WHERE tenant_id=:tenant
                                    AND kind=:kind AND normalized_value=:value LIMIT 1"""),
                            {"tenant": str(tenant_id), "kind": kind, "value": value}).scalar_one_or_none()
                        if found is not None:
                            duplicate_count += 1
                            normalized["duplicate_candidate"] = True
                            break
                    consent = normalized.get("consent_proof")
                    connection.execute(
                        text("""INSERT INTO import_rows
                                (tenant_id,id,import_id,row_number,raw,normalized,error_codes,
                                 consent_proof,marketing_eligible,status)
                                VALUES (:tenant,:id,:import,:row,:raw,:normalized,:errors,
                                        :proof,:eligible,:status)"""),
                        {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "import": str(import_id),
                         "row": offset + 1, "raw": json.dumps(raw_cells),
                         "normalized": json.dumps(normalized), "errors": errors,
                         "proof": str(consent) if consent else None,
                         # Eligibility is proof-driven, never file-driven: a row that
                         # asserts "yes" in a column but carries no proof stays false.
                         "eligible": False, "status": status})
                connection.execute(
                    text("""UPDATE imports SET valid_rows=:valid, error_rows=:errors,
                            duplicate_rows=:dupes, updated_at=now() WHERE tenant_id=:tenant AND id=:id"""),
                    {"tenant": str(tenant_id), "id": str(import_id), "valid": valid,
                     "errors": error_count, "dupes": duplicate_count})
                insert_audit(connection, tenant_id, identity, action="import.previewed",
                             resource_type="import", resource_id=import_id,
                             reason=f"import preview of {payload.filename}",
                             request_id=request.state.request_id,
                             changed_fields=["mapping", "total_rows"])
            return {"id": str(import_id), "filename": payload.filename, "total_rows": len(rows),
                    "valid_rows": valid, "error_rows": error_count, "duplicate_rows": duplicate_count,
                    "status": "previewed", "version": 1}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/imports", tags=["crm"])
    def list_imports(tenant_id: uuid.UUID, identity=Depends(require_identity),
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
                    text("SELECT count(*) FROM imports WHERE tenant_id=:tenant"),
                    {"tenant": str(tenant_id)}).scalar_one()
                rows = connection.execute(
                    text("""SELECT id::text AS id, filename, status, total_rows, valid_rows,
                                   error_rows, duplicate_rows, committed_rows, version, created_at
                            FROM imports WHERE tenant_id=:tenant
                            ORDER BY created_at DESC LIMIT :limit OFFSET :offset"""),
                    {"tenant": str(tenant_id), "limit": limit, "offset": offset}).mappings().all()
            return {"items": [{**dict(r), "created_at": r["created_at"].isoformat()} for r in rows],
                    "total": total, "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/imports/{import_id}/rows", tags=["crm"])
    def list_import_rows(tenant_id: uuid.UUID, import_id: uuid.UUID, identity=Depends(require_identity),
                         status: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        engine = app.state.engine_factory()
        if limit < 1 or limit > 200:
            raise HTTPException(status_code=422, detail={"code": "invalid_limit"})
        if offset < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_offset"})
        if status is not None and status not in ("pending", "valid", "error", "committed"):
            raise HTTPException(status_code=422, detail={"code": "invalid_status"})
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.read")
                import_or_404(connection, tenant_id, import_id)
                where = "tenant_id=:tenant AND import_id=:import"
                params: dict = {"tenant": str(tenant_id), "import": str(import_id)}
                if status:
                    where += " AND status=:status"
                    params["status"] = status
                total = connection.execute(
                    text(f"SELECT count(*) FROM import_rows WHERE {where}"), params).scalar_one()
                rows = connection.execute(
                    text(f"""SELECT row_number, normalized, error_codes, consent_proof,
                                    marketing_eligible, status, client_id::text AS client_id
                             FROM import_rows WHERE {where}
                             ORDER BY row_number LIMIT :limit OFFSET :offset"""),
                    {**params, "limit": limit, "offset": offset}).mappings().all()
            return {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/imports/{import_id}/commit", tags=["crm"])
    def commit_import(tenant_id: uuid.UUID, import_id: uuid.UUID, payload: ImportCommitIn, request: Request,
                      identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                record = connection.execute(
                    text("SELECT * FROM imports WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                    {"tenant": str(tenant_id), "id": str(import_id)}).mappings().one_or_none()
                if record is None:
                    raise HTTPException(status_code=404, detail={"code": "import_not_found"})
                # Idempotency is checked before the version guard: a client retrying the
                # identical request after a lost response resends the version it read,
                # which no longer matches. Returning the original receipt is the correct
                # answer to a replay (FR-014, QA-10); a conflict would be wrong.
                if record["status"] == "committed":
                    if record["mapping"].get("idempotency_key") == payload.idempotency_key:
                        return {"id": str(import_id), "status": "committed",
                                "committed_rows": record["committed_rows"], "replayed": True}
                    raise HTTPException(status_code=409, detail={"code": "import_already_committed"})
                if payload.expected_version != record["version"]:
                    raise HTTPException(status_code=409, detail={"code": "import_version_conflict"})
                rows = connection.execute(
                    text("""SELECT row_number, normalized, consent_proof, client_id
                            FROM import_rows WHERE tenant_id=:tenant AND import_id=:import
                              AND status='valid' ORDER BY row_number"""),
                    {"tenant": str(tenant_id), "import": str(import_id)}).mappings().all()
                committed = 0
                for row in rows:
                    normalized = row["normalized"] or {}
                    client_id = uuid.uuid4()
                    connection.execute(
                        text("""INSERT INTO clients (tenant_id,id,name,preferred_name,locale,
                                                     lifecycle,source)
                                VALUES (:tenant,:id,:name,:preferred,:locale,:lifecycle,'import')"""),
                        {"tenant": str(tenant_id), "id": str(client_id),
                         "name": normalized.get("name"), "preferred": normalized.get("preferred_name"),
                         "locale": normalized.get("locale", "en-CA"),
                         "lifecycle": normalized.get("lifecycle", "active")})
                    for kind in ("email", "phone"):
                        if value := normalized.get(kind):
                            try:
                                normalized_value = (normalize_email(value) if kind == "email"
                                                    else normalize_phone(value))
                            except ValueError:
                                continue
                            connection.execute(
                                text("""INSERT INTO client_contacts
                                        (tenant_id,id,client_id,kind,normalized_value,display_value,source,is_primary)
                                        VALUES (:tenant,:id,:client,:kind,:normalized,:display,'import',true)"""),
                                {"tenant": str(tenant_id), "id": str(uuid.uuid4()), "client": str(client_id),
                                 "kind": kind, "normalized": normalized_value, "display": value})
                    # Marketing eligibility is granted from the recorded proof alone. The
                    # row is marked committed either way; ineligibility is not an error.
                    eligible = bool(row["consent_proof"])
                    connection.execute(
                        text("""UPDATE import_rows SET status='committed', client_id=:client,
                                marketing_eligible=:eligible WHERE tenant_id=:tenant AND import_id=:import
                                AND row_number=:row"""),
                        {"client": str(client_id), "eligible": eligible, "tenant": str(tenant_id),
                         "import": str(import_id), "row": row["row_number"]})
                    committed += 1
                mapping = dict(record["mapping"])
                mapping["idempotency_key"] = payload.idempotency_key
                connection.execute(
                    text("""UPDATE imports SET status='committed', committed_rows=:count,
                            mapping=:mapping, version=version+1, updated_at=now()
                            WHERE tenant_id=:tenant AND id=:id"""),
                    {"count": committed, "mapping": json.dumps(mapping),
                     "tenant": str(tenant_id), "id": str(import_id)})
                insert_audit(connection, tenant_id, identity, action="import.committed",
                             resource_type="import", resource_id=import_id,
                             reason=f"import committed {committed} row(s)",
                             request_id=request.state.request_id,
                             changed_fields=["committed_rows", "status"])
            return {"id": str(import_id), "status": "committed", "committed_rows": committed,
                    "replayed": False}
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/imports/{import_id}/rows/{row_number}/consent", tags=["crm"])
    def set_row_consent(tenant_id: uuid.UUID, import_id: uuid.UUID, row_number: int, payload: ConsentIn,
                        request: Request, identity=Depends(require_identity)) -> dict:
        """Record consent proof for a row and make it marketing-eligible in one explicit act."""
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                import_or_404(connection, tenant_id, import_id)
                updated = connection.execute(
                    text("""UPDATE import_rows SET consent_proof=:proof, marketing_eligible=:eligible
                            WHERE tenant_id=:tenant AND import_id=:import AND row_number=:row
                            RETURNING row_number, consent_proof, marketing_eligible"""),
                    {"proof": payload.consent_proof, "eligible": payload.marketing_eligible,
                     "tenant": str(tenant_id), "import": str(import_id), "row": row_number}).mappings().one_or_none()
                if updated is None:
                    raise HTTPException(status_code=404, detail={"code": "import_row_not_found"})
                insert_audit(connection, tenant_id, identity, action="import_row.consent_recorded",
                             resource_type="import_row", resource_id=import_id,
                             reason=f"consent proof recorded for row {row_number}",
                             request_id=request.state.request_id,
                             changed_fields=["consent_proof", "marketing_eligible"])
            return dict(updated)
        finally:
            engine.dispose()