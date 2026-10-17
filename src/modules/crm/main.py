"""CRM application services: client profiles and normalized, verified contacts.

FR-009 (typed tenant-owned client record), FR-010 (E.164/conservative normalization,
preserved display input, verification and deliverability status) and FR-018 (a client may
exist independently at unrelated salons - never matched, merged or pooled across tenants).

Dependencies are injected rather than imported so this module owns its domain and the
identity app does not have to be imported from here.
"""

from __future__ import annotations

import re
import uuid
import json
import math
from typing import Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

EMAIL_SHAPE = re.compile(r"^[^\s@]+@[^\s@.]+(\.[^\s@.]+)+$")
PHONE_DIGITS = re.compile(r"\d")
# NUL and other C0 controls are rejected up front. A NUL reaching the driver raises a
# driver-level error and a 500, and an embedded newline is not a name anyone typed.
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")


def _no_control_characters(value: str) -> str:
    if CONTROL_CHARACTERS.search(value):
        raise ValueError("must not contain control characters")
    return value


def normalize_email(raw: str) -> str:
    """Conservative normalization (data-model.md:66).

    The domain is lowercased; the local part is left byte-for-byte. Provider-specific
    dot or plus-tag removal is never applied, because two addresses that differ only that
    way are not reliably the same person.
    """
    value = raw.strip()
    if value.count("@") != 1 or not EMAIL_SHAPE.match(value):
        raise ValueError("must be a valid email address")
    local, domain = value.split("@")
    return f"{local}@{domain.lower()}"


def normalize_phone(raw: str, country: str = "CA") -> str:
    """E.164 normalization with required country context (data-model.md:66)."""
    value = raw.strip()
    plus = value.startswith("+")
    digits = "".join(PHONE_DIGITS.findall(value))
    if not digits:
        raise ValueError("must contain digits")
    if not plus:
        if country.upper() != "CA":
            raise ValueError("a non-Canadian phone must be given in international +E.164 form")
        if len(digits) == 10:
            digits = "1" + digits
        elif not (len(digits) == 11 and digits.startswith("1")):
            raise ValueError("must be 10 digits, 11 digits starting 1, or international +E.164")
    elif not 8 <= len(digits) <= 15:
        raise ValueError("international numbers must have 8 to 15 digits")
    return "+" + digits


class ContactIn(BaseModel):
    kind: str = Field(pattern="^(email|phone)$")
    value: str = Field(min_length=3, max_length=320)
    country: str = Field(default="CA", pattern="^[A-Za-z]{2}$")
    is_primary: bool = False

    @field_validator("country")
    @classmethod
    def upper_country(cls, value: str) -> str:
        return _no_control_characters(value.upper())

    @field_validator("value", mode="before")
    @classmethod
    def reject_controls(cls, value: object) -> object:
        return _no_control_characters(value) if isinstance(value, str) else value


class ContactStatusIn(BaseModel):
    """Verification and deliverability are server-owned status, never client assertions
    on creation - FR-010 requires them maintained, so there has to be a way to set them."""

    verification: str = Field(pattern="^(unverified|pending|verified|failed)$")
    deliverability: str | None = Field(default=None, pattern="^(unknown|deliverable|undeliverable)$")


class ClientIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    preferred_name: str | None = Field(default=None, min_length=1, max_length=120)
    locale: str = Field(default="en-CA", pattern="^(en-CA|fr-CA)$")
    preferred_location_id: uuid.UUID | None = None
    lifecycle: str = Field(default="lead", pattern="^(lead|active|lapsed|archived)$")
    source: str = Field(default="staff", pattern="^(staff|public_booking|import)$")
    # FR-010: a staff-created client may have no reachable contact at all.
    contacts: list[ContactIn] = Field(default_factory=list, max_length=20)

    @field_validator("name", "preferred_name")
    @classmethod
    def trimmed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must contain a non-whitespace character")
        return _no_control_characters(cleaned)


class ClientPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    preferred_name: str | None = Field(default=None, max_length=120)
    locale: str | None = Field(default=None, pattern="^(en-CA|fr-CA)$")
    preferred_location_id: uuid.UUID | None = None
    lifecycle: str | None = Field(default=None, pattern="^(lead|active|lapsed|archived)$")
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("name", "preferred_name")
    @classmethod
    def trimmed(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("must contain a non-whitespace character")
        return _no_control_characters(cleaned)


class FieldDefinitionIn(BaseModel):
    entity_type: str = Field(default="client", pattern="^client$")
    key: str = Field(min_length=1, max_length=64, pattern="^[a-z_][a-z0-9_]*$")
    label: str = Field(min_length=1, max_length=120)
    type: str = Field(pattern="^(text|number|date|boolean|enum)$")
    validation: dict = Field(default_factory=dict)
    purpose: str = Field(min_length=1, max_length=240)
    sensitivity: str = Field(default="standard", pattern="^(standard|private|restricted)$")

    @field_validator("key")
    @classmethod
    def no_controls(cls, value: str) -> str:
        return _no_control_characters(value)

    @field_validator("validation")
    @classmethod
    def validate_validation(cls, value: dict, info) -> dict:
        # Cross-field validation: validation rules depend on type
        # Type is not available here; validation happens at value write time.
        return value


class FieldDefinitionPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    validation: dict | None = None
    purpose: str | None = Field(default=None, min_length=1, max_length=240)
    sensitivity: str | None = Field(default=None, pattern="^(standard|private|restricted)$")
    expected_version: int | None = Field(default=None, ge=1)


class FieldValueIn(BaseModel):
    definition_id: uuid.UUID
    entity_type: str = Field(default="client", pattern="^client$")
    entity_id: uuid.UUID | None = None
    # Exactly one of these is provided based on definition type
    value_text: str | None = None
    value_number: float | None = None
    value_date: str | None = None  # ISO date string
    value_boolean: bool | None = None


class FieldValuePatch(BaseModel):
    # Only value fields are allowed in updates; definition/entity are in URL path
    value_text: str | None = None
    value_number: float | None = None
    value_date: str | None = None
    value_boolean: bool | None = None
    expected_version: int | None = Field(default=None, ge=1)


class VisitNoteIn(BaseModel):
    appointment_id: uuid.UUID | None = None
    class_: str = Field(default="general", alias="class", pattern="^(general|clinical|allergy|preference)$")
    visibility: str = Field(default="standard", pattern="^(standard|private)$")
    body: str = Field(min_length=1, max_length=8000)

    @field_validator("body")
    @classmethod
    def no_controls(cls, value: str) -> str:
        return _no_control_characters(value)


class VisitNotePatch(BaseModel):
    class_: str | None = Field(default=None, alias="class", pattern="^(general|clinical|allergy|preference)$")
    visibility: str | None = Field(default=None, pattern="^(standard|private)$")
    body: str | None = Field(default=None, min_length=1, max_length=8000)
    expected_version: int | None = Field(default=None, ge=1)

    @field_validator("body")
    @classmethod
    def no_controls(cls, value: str | None) -> str | None:
        return _no_control_characters(value) if value is not None else value


class PreferenceIn(BaseModel):
    preference_type: str = Field(pattern="^(colour_formula|stylist_preference|product|rebooking_interval|product_allergy|sensitivity_note|communication_preference)$")
    service_id: uuid.UUID | None = None
    staff_id: uuid.UUID | None = None
    location_id: uuid.UUID | None = None
    value_json: dict
    source: str = Field(default="staff", pattern="^(staff|client|import)$")


class PreferencePatch(BaseModel):
    value_json: dict | None = None
    source: str | None = Field(default=None, pattern="^(staff|client|import)$")
    expected_version: int | None = Field(default=None, ge=1)


def _validate_field_value(definition: dict, payload: FieldValueIn) -> dict:
    """Validate a field value against its definition. Returns dict with typed value column."""
    ftype = definition["type"]
    validation = definition.get("validation", {})
    provided = [name for name in ("value_text", "value_number", "value_date", "value_boolean")
                if getattr(payload, name) is not None]
    expected = "value_text" if ftype == "enum" else f"value_{ftype}"
    if provided != [expected]:
        raise HTTPException(status_code=422, detail={"code": "field_value_type_mismatch"})
    if ftype == "text":
        if payload.value_text is None:
            raise HTTPException(status_code=422, detail={"code": "field_value_required", "field": "value_text"})
        max_len = validation.get("max_length")
        if max_len and len(payload.value_text) > max_len:
            raise HTTPException(status_code=422, detail={"code": "field_value_too_long", "max_length": max_len})
        return {"value_text": payload.value_text}
    elif ftype == "number":
        if payload.value_number is None:
            raise HTTPException(status_code=422, detail={"code": "field_value_required", "field": "value_number"})
        v = payload.value_number
        if not math.isfinite(v):
            raise HTTPException(status_code=422, detail={"code": "field_value_not_finite"})
        if "min" in validation and v < validation["min"]:
            raise HTTPException(status_code=422, detail={"code": "field_value_below_min", "min": validation["min"]})
        if "max" in validation and v > validation["max"]:
            raise HTTPException(status_code=422, detail={"code": "field_value_above_max", "max": validation["max"]})
        return {"value_number": v}
    elif ftype == "date":
        if payload.value_date is None:
            raise HTTPException(status_code=422, detail={"code": "field_value_required", "field": "value_date"})
        # Validate ISO date format
        try:
            from datetime import date
            date.fromisoformat(payload.value_date)
        except ValueError:
            raise HTTPException(status_code=422, detail={"code": "field_value_invalid_date"})
        return {"value_date": payload.value_date}
    elif ftype == "boolean":
        if payload.value_boolean is None:
            raise HTTPException(status_code=422, detail={"code": "field_value_required", "field": "value_boolean"})
        return {"value_boolean": payload.value_boolean}
    elif ftype == "enum":
        if payload.value_text is None:
            raise HTTPException(status_code=422, detail={"code": "field_value_required", "field": "value_text"})
        allowed = validation.get("values", [])
        if payload.value_text not in allowed:
            raise HTTPException(status_code=422, detail={"code": "field_value_not_in_enum", "allowed": allowed})
        return {"value_text": payload.value_text}
    else:
        raise HTTPException(status_code=422, detail={"code": "unknown_field_type", "type": ftype})


def _validate_definition_schema(field_type: str, validation: dict) -> None:
    valid = isinstance(validation, dict)
    if field_type == "text":
        valid = valid and set(validation) <= {"max_length"} and (
            "max_length" not in validation or
            type(validation["max_length"]) is int and 1 <= validation["max_length"] <= 8000
        )
    elif field_type == "number":
        valid = valid and set(validation) <= {"min", "max"} and all(
            type(value) is int or (type(value) is float and math.isfinite(value))
            for value in validation.values())
        valid = valid and not ("min" in validation and "max" in validation and validation["min"] > validation["max"])
    elif field_type == "enum":
        values = validation.get("values") if valid and set(validation) == {"values"} else None
        valid = isinstance(values, list) and 1 <= len(values) <= 100 and all(
            isinstance(item, str) and item.strip() and len(item) <= 120 for item in values
        ) and len(set(values)) == len(values)
    elif field_type in ("date", "boolean"):
        valid = valid and not validation
    if not valid:
        raise HTTPException(status_code=422, detail={"code": "invalid_field_validation"})


def _require_field_sensitivity(role: str, sensitivity: str) -> None:
    if sensitivity == "private" and role not in ("owner", "manager"):
        raise HTTPException(status_code=403, detail={"code": "sensitive_field_forbidden"})
    if sensitivity == "restricted" and role != "owner":
        raise HTTPException(status_code=403, detail={"code": "restricted_field_forbidden"})


def _validate_preference_value(preference_type: str, value: dict) -> None:
    if not isinstance(value, dict):
        raise HTTPException(status_code=422, detail={"code": "invalid_preference_value"})
    schemas = {
        "colour_formula": {"formula", "developer", "processing_time"},
        "stylist_preference": {"stylist_name", "avoid", "notes"},
        "product": {"brand", "products", "notes"},
        "rebooking_interval": {"days", "service"},
        "communication_preference": {"channels"},
    }
    fields = schemas.get(preference_type)
    if fields is None:
        raise HTTPException(status_code=422, detail={"code": "sensitive_preference_type_disabled"})
    valid = isinstance(value, dict) and set(value) <= fields
    if preference_type == "colour_formula":
        valid = valid and isinstance(value.get("formula"), str) and value["formula"].strip() and len(value["formula"]) <= 240
        valid = valid and ("developer" not in value or (isinstance(value["developer"], str) and len(value["developer"]) <= 80))
        valid = valid and ("processing_time" not in value or (type(value["processing_time"]) is int and 1 <= value["processing_time"] <= 240))
    elif preference_type in ("stylist_preference", "product"):
        text_keys = {"stylist_name", "brand", "notes"} & value.keys()
        list_keys = {"avoid", "products"} & value.keys()
        required_key = "stylist_name" if preference_type == "stylist_preference" else "brand"
        alternate_key = "avoid" if preference_type == "stylist_preference" else "products"
        valid = valid and (required_key in value or alternate_key in value)
        valid = valid and all(isinstance(value[key], str) and value[key].strip() and len(value[key]) <= 240
                              for key in text_keys)
        valid = valid and all(isinstance(value[key], list) and len(value[key]) <= 50 and
                              all(isinstance(item, str) and item.strip() and len(item) <= 120 for item in value[key])
                              for key in list_keys)
    elif preference_type == "rebooking_interval":
        valid = valid and type(value.get("days")) is int and 1 <= value["days"] <= 365
        valid = valid and ("service" not in value or (isinstance(value["service"], str) and len(value["service"]) <= 120))
    elif preference_type == "communication_preference":
        channels = value.get("channels")
        valid = valid and isinstance(channels, list) and 1 <= len(channels) <= 3
        valid = valid and all(isinstance(channel, str) for channel in channels)
        valid = valid and set(channels) <= {"email", "sms", "whatsapp"} and len(set(channels)) == len(channels)
    if not valid:
        raise HTTPException(status_code=422, detail={"code": "invalid_preference_value"})


def _serialize_field_value(row: dict) -> dict:
    """Convert stored value columns to a single value for API response."""
    for key in ("value_text", "value_number", "value_date", "value_boolean"):
        if row.get(key) is not None:
            if key == "value_date" and row[key] is not None:
                return {key: row[key].isoformat()}
            if key == "value_number":
                return {key: float(row[key])}
            return {key: row[key]}
    return {}


def _row_to_dict(row: dict) -> dict:
    """Convert SQLAlchemy row mapping to dict with UUIDs as strings."""
    result = {}
    for k, v in row.items():
        if isinstance(v, uuid.UUID):
            result[k] = str(v)
        elif hasattr(v, 'isoformat'):  # datetime
            result[k] = v.isoformat()
        else:
            result[k] = v
    return result


def _check_field_sensitivity(definition: dict, role: str, extra_permissions: frozenset) -> bool:
    """Return True if role may see this field based on sensitivity."""
    sens = definition.get("sensitivity", "standard")
    if sens == "standard":
        return True
    if sens == "private":
        return "clients.sensitive.read" in extra_permissions
    if sens == "restricted":
        return role == "owner"
    return False


def _check_note_visibility(note: dict, role: str, has_sensitive: bool) -> bool:
    """Return True if role may see this visit note."""
    if note["visibility"] == "standard":
        return True
    if note["visibility"] == "private":
        return has_sensitive
    return False


def _build_field_value_update(definition: dict, validated: dict) -> tuple[str, dict]:
    """Build SET clause for field value update."""
    ftype = definition["type"]
    col_map = {
        "text": "value_text",
        "number": "value_number",
        "date": "value_date",
        "boolean": "value_boolean",
        "enum": "value_text",
    }
    col = col_map[ftype]
    val = validated[col]
    return col, val


def _validated_contact(payload: ContactIn) -> tuple[str, str]:
    try:
        normalized = (normalize_email(payload.value) if payload.kind == "email"
                      else normalize_phone(payload.value, payload.country))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"code": "contact_not_normalizable",
                                                    "field": payload.kind}) from exc
    return normalized, payload.value.strip()


def _detect_duplicate_candidates(connection, tenant_id: uuid.UUID, client_id: uuid.UUID,
                                 contact_id: uuid.UUID, kind: str, normalized: str) -> int:
    """Record who else in this tenant shares this value. Never merges - see FR-015."""
    # Scope is the tenant only (FR-018): a matching value in another salon is a different
    # person and must never be discoverable from here.
    rows = connection.execute(
        text("""SELECT DISTINCT c.client_id FROM client_contacts c
                JOIN clients k ON k.tenant_id=c.tenant_id AND k.id=c.client_id
                WHERE c.tenant_id=:tenant AND c.kind=:kind AND c.normalized_value=:value
                  AND c.client_id<>:client AND k.deleted_at IS NULL"""),
        {"tenant": str(tenant_id), "kind": kind, "value": normalized, "client": str(client_id)},
    ).scalars().all()
    for other in rows:
        connection.execute(
            text("""INSERT INTO client_duplicate_candidates
                      (tenant_id,client_id,contact_id,other_client_id,match_basis,matched_value)
                    VALUES (:tenant,:client,:contact,:other,:basis,:value)
                    ON CONFLICT (tenant_id,client_id,contact_id,other_client_id) DO NOTHING"""),
            {"tenant": str(tenant_id), "client": str(client_id), "contact": str(contact_id),
             "other": str(other), "basis": kind, "value": normalized},
        )
    return len(rows)


def register_client_routes(
    app: FastAPI,
    *,
    require_identity: Callable,
    membership_scope: Callable,
    require_permission: Callable[[str, str], None],
    insert_audit: Callable,
    project_fields: Callable[..., dict],
) -> None:
    def client_or_404(connection, tenant_id: uuid.UUID, client_id: uuid.UUID,
                       role: str = "owner", assigned_locations: list[uuid.UUID] | None = None):
        scope = ""
        params = {"tenant": str(tenant_id), "client": str(client_id)}
        if role != "owner":
            locations = [str(value) for value in (assigned_locations or [])]
            if not locations:
                raise HTTPException(status_code=404, detail={"code": "client_not_found"})
            scope = """ AND (preferred_location_id=ANY(:locations) OR EXISTS (
                SELECT 1 FROM appointments a WHERE a.tenant_id=clients.tenant_id
                  AND a.client_id=clients.id AND a.location_id=ANY(:locations)))"""
            params["locations"] = locations
        row = connection.execute(
            text("""SELECT id::text AS id, name, preferred_name, locale, lifecycle,
                           preferred_location_id::text AS preferred_location_id, source,
                           version, created_at
                    FROM clients WHERE tenant_id=:tenant AND id=:client AND deleted_at IS NULL""" + scope),
            params,
        ).mappings().one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "client_not_found"})
        return row

    def contact_rows(connection, tenant_id: uuid.UUID, client_id: uuid.UUID) -> list[dict]:
        rows = connection.execute(
            text("""SELECT id::text AS id, kind, display_value, country, verification,
                           verified_at, deliverability, is_primary, source, version
                    FROM client_contacts WHERE tenant_id=:tenant AND client_id=:client
                    ORDER BY created_at, id"""),
            {"tenant": str(tenant_id), "client": str(client_id)},
        ).mappings().all()
        return [{**dict(row),
                 "verified_at": row["verified_at"].isoformat() if row["verified_at"] else None}
                for row in rows]

    def read_client(connection, tenant_id: uuid.UUID, client_id: uuid.UUID, role: str,
                    assigned_locations: list[uuid.UUID] | None = None) -> dict:
        row = client_or_404(connection, tenant_id, client_id, role, assigned_locations)
        # The field policy governs descriptive profile data. The resource's own identity
        # and version are structural - a caller must be able to address and version-check
        # the record it was just handed - so they are never projected away.
        # `clients.name` (data-model.md:55) is offered to the policy under the key it
        # already uses, and mapped back, so TASK-004's allowlist stays the single
        # definition of who may see which client field.
        policy_input = {**dict(row), "display_name": row["name"]}
        visible = project_fields(role, policy_input)
        profile = {key: value for key, value in visible.items() if key != "display_name"}
        if "display_name" in visible:
            profile["name"] = visible["display_name"]
        # Contact detail is gated by the same policy that declares a role may see a
        # contact summary, rather than by a second rule living here. The capability is
        # asked of the policy directly: a role that may see contacts has none on a
        # client with none, so it cannot be inferred from the filtered output.
        may_see_contacts = "contact_summary" in project_fields(role, {"contact_summary": None})
        return {
            "id": row["id"],
            "version": row["version"],
            "created_at": row["created_at"].isoformat(),
            "source": row["source"],
            # A reference the caller already reaches through their own membership, so it
            # is structural rather than descriptive.
            "preferred_location_id": row["preferred_location_id"],
            **profile,
            "contacts": contact_rows(connection, tenant_id, client_id) if may_see_contacts else None,
        }

    @app.get("/v1/tenants/{tenant_id}/clients/search", tags=["crm"])
    def search_clients(tenant_id: uuid.UUID, request: Request,
                       identity=Depends(require_identity),
                       q: str | None = None,
                       lifecycle: str | None = None,
                       location_id: uuid.UUID | None = None,
                       source: str | None = None,
                       limit: int = 20,
                       offset: int = 0) -> dict:
        engine = app.state.engine_factory()
        if q is not None and len(q) > 120:
            raise HTTPException(status_code=422, detail={"code": "search_query_too_long"})
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail={"code": "invalid_limit"})
        if offset < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_offset"})
        if lifecycle is not None and lifecycle not in ("lead", "active", "lapsed", "archived"):
            raise HTTPException(status_code=422, detail={"code": "invalid_lifecycle"})
        if source is not None and source not in ("staff", "public_booking", "import"):
            raise HTTPException(status_code=422, detail={"code": "invalid_source"})
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.read")
                visible_location_ids = {str(value) for value in assigned}
                may_see_contacts = "contact_summary" in project_fields(role, {"contact_summary": None})
                pat = f"%{q.replace('%', '\\%').replace('_', '\\_')}%" if q else None
                params = {
                    "tenant": str(tenant_id),
                    "pat": pat,
                    "lifecycle": lifecycle,
                    "location": str(location_id) if location_id else None,
                    "source": source,
                    "limit": limit,
                    "offset": offset,
                    "may_see_contacts": may_see_contacts,
                }
                base_sql = """
                    SELECT id::text AS id, name, preferred_name, locale, lifecycle,
                           preferred_location_id::text AS preferred_location_id, source,
                           version, created_at
                    FROM clients
                    WHERE tenant_id=:tenant AND deleted_at IS NULL
                """
                where_parts = []
                if pat:
                    if may_see_contacts:
                        where_parts.append("""(
                            name ILIKE :pat ESCAPE '\\' OR preferred_name ILIKE :pat ESCAPE '\\'
                            OR id IN (
                                SELECT client_id FROM client_contacts
                                WHERE tenant_id=:tenant
                                  AND (display_value ILIKE :pat ESCAPE '\\' OR normalized_value ILIKE :pat ESCAPE '\\')
                            )
                        )""")
                    else:
                        where_parts.append("""(
                            name ILIKE :pat ESCAPE '\\' OR preferred_name ILIKE :pat ESCAPE '\\'
                        )""")
                if lifecycle is not None:
                    where_parts.append("lifecycle = :lifecycle")
                if location_id is not None:
                    where_parts.append("""(preferred_location_id = :location OR EXISTS (
                        SELECT 1 FROM appointments a WHERE a.tenant_id=clients.tenant_id
                          AND a.client_id=clients.id AND a.location_id=:location
                    ))""")
                if source is not None:
                    where_parts.append("source = :source")
                if role != "owner":
                    params["assigned"] = [str(value) for value in assigned]
                    where_parts.append("""(preferred_location_id=ANY(:assigned) OR EXISTS (
                        SELECT 1 FROM appointments a WHERE a.tenant_id=clients.tenant_id
                          AND a.client_id=clients.id AND a.location_id=ANY(:assigned)))""")
                where_sql = " AND ".join(where_parts) if where_parts else "TRUE"
                items = connection.execute(
                    text(f"{base_sql} AND {where_sql} ORDER BY created_at DESC, id LIMIT :limit OFFSET :offset"),
                    params
                ).mappings().all()
                total = connection.execute(
                    text(f"SELECT count(*) FROM ({base_sql} AND {where_sql}) t"),
                    {k: v for k, v in params.items() if k not in ("limit", "offset")}
                ).scalar_one()
                results = []
                for row in items:
                    policy_input = {**dict(row), "display_name": row["name"]}
                    visible = project_fields(role, policy_input)
                    profile = {key: value for key, value in visible.items() if key != "display_name"}
                    if "display_name" in visible:
                        profile["name"] = visible["display_name"]
                    results.append({
                        "id": row["id"],
                        "version": row["version"],
                        "created_at": row["created_at"].isoformat(),
                        "source": row["source"],
                        **profile,
                    })
                    if role == "owner" or (row["preferred_location_id"] is not None and
                                            str(row["preferred_location_id"]) in visible_location_ids):
                        results[-1]["preferred_location_id"] = row["preferred_location_id"]
            return {"items": results, "total": total, "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/clients/{client_id}/timeline", tags=["crm"])
    def get_client_timeline(tenant_id: uuid.UUID, client_id: uuid.UUID, request: Request,
                            identity=Depends(require_identity),
                            limit: int = 20,
                            offset: int = 0) -> dict:
        engine = app.state.engine_factory()
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail={"code": "invalid_limit"})
        if offset < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_offset"})
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.read")
                client_or_404(connection, tenant_id, client_id, role, assigned)
                assigned_ids = [str(value) for value in assigned]
                parameters = {"tenant": str(tenant_id), "client": str(client_id),
                              "assigned": assigned_ids, "limit": limit, "offset": offset,
                              "show_private_notes": role in ("owner", "manager")}
                timeline_sql = """
                    WITH timeline AS (
                        SELECT e.occurred_at, e.id AS sort_id, e.action,
                               e.resource_type AS source_type, e.resource_id AS source_id,
                               e.actor_issuer, e.actor_subject, e.reason,
                               e.changed_fields,
                               jsonb_build_object() AS details
                        FROM tenant_audit_events e
                        WHERE e.tenant_id=:tenant AND (
                            (e.resource_type='client' AND e.resource_id=:client) OR
                            (e.resource_type='client_contact' AND e.resource_id IN (
                                SELECT id FROM client_contacts WHERE tenant_id=:tenant AND client_id=:client
                            ))
                        ) AND (:owner OR EXISTS (
                            SELECT 1 FROM clients c WHERE c.tenant_id=:tenant AND c.id=:client
                              AND c.preferred_location_id=ANY(:assigned)
                        ))
                        UNION ALL
                        SELECT t.occurred_at, t.id, 'appointment.status_changed', 'appointment',
                               t.appointment_id, t.actor_issuer, t.actor_subject, NULL::text,
                               ARRAY[]::text[], jsonb_build_object('from_status', t.from_status,
                                                                   'to_status', t.to_status)
                        FROM appointment_transitions t
                        JOIN appointments a ON a.tenant_id=t.tenant_id AND a.id=t.appointment_id
                        WHERE t.tenant_id=:tenant AND a.client_id=:client
                          AND (:owner OR a.location_id=ANY(:assigned))
                        UNION ALL
                        SELECT v.completed_at, v.id, 'visit.completed', 'visit', v.id,
                               NULL::text, v.staff_id::text, NULL::text, ARRAY[]::text[],
                               jsonb_build_object('services', COALESCE((
                                   SELECT jsonb_agg(vs.service_name ORDER BY vs.sequence)
                                   FROM visit_services vs WHERE vs.tenant_id=v.tenant_id AND vs.visit_id=v.id
                               ), '[]'::jsonb))
                        FROM visits v
                        WHERE v.tenant_id=:tenant AND v.client_id=:client
                          AND (:owner OR v.location_id=ANY(:assigned))
                        UNION ALL
                        SELECT n.created_at, n.id, 'visit_note.created', 'visit_note', n.id,
                               n.author_issuer, n.author_subject, NULL::text, ARRAY[]::text[],
                               jsonb_build_object('class', n.class, 'visibility', n.visibility, 'body', n.body)
                        FROM visit_notes n
                        WHERE n.tenant_id=:tenant AND n.client_id=:client
                          AND (n.visibility <> 'private' OR :show_private_notes)
                          AND (:owner OR (
                              (n.appointment_id IS NULL AND EXISTS (
                                  SELECT 1 FROM clients c WHERE c.tenant_id=:tenant AND c.id=:client
                                    AND c.preferred_location_id=ANY(:assigned)
                              )) OR n.appointment_id IN (
                                  SELECT a.id FROM appointments a WHERE a.tenant_id=:tenant
                                    AND a.client_id=:client AND a.location_id=ANY(:assigned)
                              )
                          ))
                        UNION ALL
                        SELECT r.posted_at, r.id, 'receipt.recorded', 'salon_receipt', r.id,
                               NULL::text, NULL::text, NULL::text, ARRAY[]::text[],
                               jsonb_build_object('kind', r.kind, 'amount_minor', r.amount_minor,
                                                  'currency', r.currency, 'method', r.method)
                        FROM salon_receipt_entries r
                        WHERE r.tenant_id=:tenant AND r.client_id=:client
                          AND (:owner OR r.appointment_id IN (
                              SELECT a.id FROM appointments a WHERE a.tenant_id=:tenant
                                AND a.client_id=:client AND a.location_id=ANY(:assigned)
                          ) OR EXISTS (SELECT 1 FROM clients c WHERE c.tenant_id=:tenant
                                       AND c.id=:client AND c.preferred_location_id=ANY(:assigned)))
                        UNION ALL
                        SELECT c.created_at, c.id, 'communication.' || c.state, 'communication', c.id,
                               NULL::text, NULL::text, NULL::text, ARRAY[]::text[],
                               jsonb_build_object('channel', c.channel, 'purpose', c.purpose,
                                                  'state', c.state, 'scheduled_at', c.scheduled_at,
                                                  'dispatched_at', c.dispatched_at)
                        FROM communications c
                        WHERE c.tenant_id=:tenant AND c.client_id=:client
                          AND (:owner OR c.source_id IN (
                              SELECT a.id FROM appointments a WHERE a.tenant_id=:tenant
                                AND a.client_id=:client AND a.location_id=ANY(:assigned)
                          ) OR EXISTS (SELECT 1 FROM clients cl WHERE cl.tenant_id=:tenant
                                       AND cl.id=:client AND cl.preferred_location_id=ANY(:assigned)))
                    )
                """
                items = connection.execute(
                    text(timeline_sql + """
                        SELECT occurred_at, action, source_type, source_id::text AS source_id,
                               actor_issuer, actor_subject, reason, changed_fields, details
                        FROM timeline ORDER BY occurred_at DESC, sort_id DESC
                        LIMIT :limit OFFSET :offset
                    """),
                    {**parameters, "owner": role == "owner"}
                ).mappings().all()
                total = connection.execute(
                    text(timeline_sql + "SELECT count(*) FROM timeline"),
                    {**parameters, "owner": role == "owner"}
                ).scalar_one()
                results = [{
                    "occurred_at": row["occurred_at"].isoformat(),
                    "action": row["action"],
                    "source": {"type": row["source_type"], "id": row["source_id"]},
                    "actor": {"issuer": row["actor_issuer"], "subject": row["actor_subject"]},
                    "reason": row["reason"],
                    "changed_fields": list(row["changed_fields"]),
                    "details": row["details"],
                } for row in items]
            return {"items": results, "total": total, "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/clients", tags=["crm"], status_code=201)
    def create_client(tenant_id: uuid.UUID, payload: ClientIn, request: Request,
                      identity=Depends(require_identity)) -> JSONResponse:
        engine = app.state.engine_factory()
        client_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                prepared = [_validated_contact(item) for item in payload.contacts]
                preferred_location_id = payload.preferred_location_id
                if role != "owner":
                    if preferred_location_id is None:
                        if len(assigned) == 1:
                            preferred_location_id = assigned[0]
                        else:
                            raise HTTPException(status_code=422, detail={"code": "client_location_required"})
                    if preferred_location_id not in assigned:
                        raise HTTPException(status_code=404, detail={"code": "location_not_found"})
                if preferred_location_id is not None:
                    owned = connection.execute(
                        text("SELECT 1 FROM locations WHERE tenant_id=:tenant AND id=:location"),
                        {"tenant": str(tenant_id), "location": str(preferred_location_id)}).scalar_one_or_none()
                    if owned is None:
                        raise HTTPException(status_code=404, detail={"code": "location_not_found"})
                connection.execute(
                    text("""INSERT INTO clients (tenant_id,id,name,preferred_name,locale,
                                                 lifecycle,preferred_location_id,source)
                            VALUES (:tenant,:id,:name,:preferred,:locale,
                                    :lifecycle,:location,:source)"""),
                    {"tenant": str(tenant_id), "id": str(client_id), "name": payload.name,
                     "preferred": payload.preferred_name, "locale": payload.locale,
                     "lifecycle": payload.lifecycle,
                     "location": str(preferred_location_id) if preferred_location_id else None,
                     "source": payload.source})
                duplicates = 0
                # Exactly one contact is primary: the first the caller marked, or the
                # first one supplied when none was marked.
                primary_index = next((i for i, item in enumerate(payload.contacts) if item.is_primary), 0)
                for index, (item, (normalized, _display)) in enumerate(zip(payload.contacts, prepared)):
                    contact_id = uuid.uuid4()
                    connection.execute(
                        text("""INSERT INTO client_contacts
                                (tenant_id,id,client_id,kind,normalized_value,display_value,
                                 country,is_primary,source)
                                VALUES (:tenant,:id,:client,:kind,:normalized,:display,
                                        :country,:primary,:source)"""),
                        {"tenant": str(tenant_id), "id": str(contact_id), "client": str(client_id),
                         "kind": item.kind, "normalized": normalized, "display": item.value.strip(),
                         "country": item.country, "primary": index == primary_index,
                         "source": payload.source})
                    duplicates += _detect_duplicate_candidates(
                        connection, tenant_id, client_id, contact_id, item.kind, normalized)
                insert_audit(connection, tenant_id, identity, action="client.created",
                             resource_type="client", resource_id=client_id,
                             reason=f"client created via {payload.source}",
                             request_id=request.state.request_id,
                             changed_fields=(["name", "locale", "source", "lifecycle"]
                                             + (["preferred_name"] if payload.preferred_name else [])
                                             + (["preferred_location_id"] if preferred_location_id else [])
                                             + (["contacts"] if payload.contacts else [])))
                body = read_client(connection, tenant_id, client_id, role, assigned)
            return JSONResponse(status_code=201,
                                content={**body, "duplicate_candidates": duplicates},
                                headers={"X-Request-ID": str(request.state.request_id)})
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/clients/{client_id}", tags=["crm"])
    def get_client(tenant_id: uuid.UUID, client_id: uuid.UUID, identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.read")
                return read_client(connection, tenant_id, client_id, role, assigned)
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/clients/{client_id}", tags=["crm"])
    def update_client(tenant_id: uuid.UUID, client_id: uuid.UUID, payload: ClientPatch, request: Request,
                      identity=Depends(require_identity)) -> dict:
        changes = payload.model_dump(exclude_unset=True)
        expected = changes.pop("expected_version", None)
        if expected is None:
            # api-contracts.md:13: an edit carries the version it read. Without it two
            # concurrent writers both succeed and the later one silently wins.
            raise HTTPException(status_code=428, detail={"code": "expected_version_required"})
        # Nullable-by-design fields may be cleared; required fields may not. Clearing a
        # required one would violate a NOT NULL constraint and surface as a 500.
        NON_NULLABLE = {"name", "locale"}
        if cleared := [key for key, value in changes.items() if value is None]:
            if illegal := NON_NULLABLE.intersection(cleared):
                raise HTTPException(status_code=422, detail={
                    "code": "client_field_not_clearable",
                    "fields": sorted(illegal),
                })
        changes = {key: value for key, value in changes.items() if value is not None}
        if not changes and not cleared:
            raise HTTPException(status_code=422, detail={"code": "client_change_required"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                if role != "owner" and "preferred_location_id" in changes:
                    if changes["preferred_location_id"] is None or uuid.UUID(str(changes["preferred_location_id"])) not in assigned:
                        raise HTTPException(status_code=404, detail={"code": "location_not_found"})
                current = connection.execute(
                    text("SELECT version FROM clients WHERE tenant_id=:tenant AND id=:client AND deleted_at IS NULL "
                         + ("" if role == "owner" else "AND (preferred_location_id=ANY(:assigned) OR EXISTS "
                            "(SELECT 1 FROM appointments a WHERE a.tenant_id=clients.tenant_id AND a.client_id=clients.id "
                            "AND a.location_id=ANY(:assigned))) ") + "FOR UPDATE"),
                    {"tenant": str(tenant_id), "client": str(client_id),
                     **({"assigned": [str(value) for value in assigned]} if role != "owner" else {})}).scalar_one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "client_not_found"})
                # Optimistic concurrency (api-contracts.md:13).
                if expected != current:
                    raise HTTPException(status_code=409, detail={"code": "client_version_conflict"})
                if "preferred_location_id" in changes:
                    owned = connection.execute(
                        text("SELECT 1 FROM locations WHERE tenant_id=:tenant AND id=:location"),
                        {"tenant": str(tenant_id), "location": str(changes["preferred_location_id"])}).scalar_one_or_none()
                    if owned is None:
                        raise HTTPException(status_code=404, detail={"code": "location_not_found"})
                # Every cleared key is written back as NULL. Handling only some of them
                # produced an empty assignment list and a 500.
                merged = {**changes, **{key: None for key in cleared}}
                assignments = ", ".join(f"{key}=:{key}" for key in merged)
                params = {"tenant": str(tenant_id), "client": str(client_id), **merged}
                updated = connection.execute(
                    text(f"""UPDATE clients SET {assignments}, version=version+1
                             WHERE tenant_id=:tenant AND id=:client RETURNING version"""),
                    params).mappings().one()
                insert_audit(connection, tenant_id, identity, action="client.updated",
                             resource_type="client", resource_id=client_id,
                             reason="client profile updated",
                             request_id=request.state.request_id,
                             changed_fields=sorted(set(changes) | set(cleared)))
                body = read_client(connection, tenant_id, client_id, role, assigned)
            return {**body, "version": updated["version"]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/clients/{client_id}/contacts", tags=["crm"], status_code=201)
    def add_contact(tenant_id: uuid.UUID, client_id: uuid.UUID, payload: ContactIn, request: Request,
                    identity=Depends(require_identity)) -> JSONResponse:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                client_or_404(connection, tenant_id, client_id, role, assigned)
                normalized, display = _validated_contact(payload)
                contact_id = uuid.uuid4()
                connection.execute(
                    text("""INSERT INTO client_contacts
                            (tenant_id,id,client_id,kind,normalized_value,display_value,country,is_primary,source)
                            VALUES (:tenant,:id,:client,:kind,:normalized,:display,:country,:primary,'staff')"""),
                    {"tenant": str(tenant_id), "id": str(contact_id), "client": str(client_id),
                     "kind": payload.kind, "normalized": normalized, "display": display,
                     "country": payload.country, "primary": payload.is_primary})
                if payload.is_primary:
                    connection.execute(
                        text("UPDATE client_contacts SET is_primary=false WHERE client_id=:client AND id<>:contact"),
                        {"client": str(client_id), "contact": str(contact_id)})
                duplicates = _detect_duplicate_candidates(
                    connection, tenant_id, client_id, contact_id, payload.kind, normalized)
                insert_audit(connection, tenant_id, identity, action="client.contact_added",
                             resource_type="client_contact", resource_id=contact_id,
                             reason=f"{payload.kind} contact added",
                             request_id=request.state.request_id,
                             changed_fields=["contact_kind", "contact_source"])
                body = read_client(connection, tenant_id, client_id, role, assigned)
            return JSONResponse(status_code=201,
                                content={**body, "duplicate_candidates": duplicates},
                                headers={"X-Request-ID": str(request.state.request_id)})
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/clients/{client_id}/contacts/{contact_id}", tags=["crm"])
    def update_contact(tenant_id: uuid.UUID, client_id: uuid.UUID, contact_id: uuid.UUID,
                       payload: ContactStatusIn, request: Request,
                       identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                client_or_404(connection, tenant_id, client_id, role, assigned)
                current = connection.execute(
                    text("SELECT verification FROM client_contacts WHERE tenant_id=:tenant AND client_id=:client AND id=:contact FOR UPDATE"),
                    {"tenant": str(tenant_id), "client": str(client_id), "contact": str(contact_id)}).scalar_one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "contact_not_found"})
                # A failed verification is terminal: it is never quietly retried into
                # "verified" by a later status write.
                if current == "failed" and payload.verification == "verified":
                    raise HTTPException(status_code=409, detail={"code": "contact_verification_failed"})
                connection.execute(
                    text("""UPDATE client_contacts
                            SET verification=:verification,
                                verified_at=CASE WHEN :verification='verified' THEN now() ELSE NULL END,
                                deliverability=:deliverability,
                                version=version+1
                            WHERE tenant_id=:tenant AND id=:contact"""),
                    {"tenant": str(tenant_id), "contact": str(contact_id),
                     "verification": payload.verification,
                     "deliverability": payload.deliverability or "unknown"})
                insert_audit(connection, tenant_id, identity, action="client.contact_status_updated",
                             resource_type="client_contact", resource_id=contact_id,
                             reason=f"contact verification set to {payload.verification}",
                             request_id=request.state.request_id,
                             changed_fields=["contact_verification", "contact_deliverability"])
                return read_client(connection, tenant_id, client_id, role, assigned)
        finally:
            engine.dispose()

    # Field Definitions
    @app.post("/v1/tenants/{tenant_id}/field-definitions", tags=["crm"], status_code=201)
    def create_field_definition(tenant_id: uuid.UUID, payload: FieldDefinitionIn, request: Request,
                                identity=Depends(require_identity)) -> JSONResponse:
        engine = app.state.engine_factory()
        def_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                _validate_definition_schema(payload.type, payload.validation)
                _require_field_sensitivity(role, payload.sensitivity)
                connection.execute(
                    text("""INSERT INTO client_field_definitions
                            (tenant_id, id, entity_type, key, label, type, validation, purpose, sensitivity)
                            VALUES (:tenant, :id, :entity_type, :key, :label, :type, :validation, :purpose, :sensitivity)"""),
                    {"tenant": str(tenant_id), "id": str(def_id), "entity_type": payload.entity_type,
                     "key": payload.key, "label": payload.label, "type": payload.type,
                     "validation": json.dumps(payload.validation), "purpose": payload.purpose,
                     "sensitivity": payload.sensitivity})
                insert_audit(connection, tenant_id, identity, action="field_definition.created",
                             resource_type="field_definition", resource_id=def_id,
                             reason=f"field definition {payload.key} created",
                             request_id=request.state.request_id,
                             changed_fields=["key", "label", "type", "purpose", "sensitivity"])
                row = connection.execute(
                    text("SELECT * FROM client_field_definitions WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(def_id)}).mappings().one()
            return JSONResponse(status_code=201, content=_row_to_dict(row), headers={"X-Request-ID": str(request.state.request_id)})
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/field-definitions", tags=["crm"])
    def list_field_definitions(tenant_id: uuid.UUID, request: Request,
                               identity=Depends(require_identity),
                               entity_type: str | None = None,
                               limit: int = 20,
                               offset: int = 0) -> dict:
        engine = app.state.engine_factory()
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail={"code": "invalid_limit"})
        if offset < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_offset"})
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.read")
                where = "tenant_id=:tenant"
                params = {"tenant": str(tenant_id)}
                if entity_type:
                    where += " AND entity_type=:entity_type"
                    params["entity_type"] = entity_type
                # Definitions carry no per-row visibility policy, so the page is the whole slice.
                total = connection.execute(
                    text(f"SELECT count(*) FROM client_field_definitions WHERE {where}"),
                    params).scalar_one()
                rows = connection.execute(
                    text(f"SELECT * FROM client_field_definitions WHERE {where} "
                         "ORDER BY created_at LIMIT :limit OFFSET :offset"),
                    {**params, "limit": limit, "offset": offset}).mappings().all()
            return {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/field-definitions/{def_id}", tags=["crm"])
    def update_field_definition(tenant_id: uuid.UUID, def_id: uuid.UUID, payload: FieldDefinitionPatch, request: Request,
                                identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                current = connection.execute(
                    text("SELECT * FROM client_field_definitions WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                    {"tenant": str(tenant_id), "id": str(def_id)}).mappings().one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "field_definition_not_found"})
                if payload.expected_version is None:
                    raise HTTPException(status_code=428, detail={"code": "expected_version_required"})
                if payload.expected_version is not None and payload.expected_version != current["version"]:
                    raise HTTPException(status_code=409, detail={"code": "field_definition_version_conflict"})
                changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
                if not changes:
                    raise HTTPException(status_code=422, detail={"code": "field_definition_change_required"})
                next_sensitivity = changes.get("sensitivity", current["sensitivity"])
                _require_field_sensitivity(role, next_sensitivity)
                if "validation" in changes:
                    _validate_definition_schema(current["type"], changes["validation"])
                    value_count = connection.execute(
                        text("SELECT count(*) FROM client_field_values WHERE tenant_id=:tenant AND definition_id=:id"),
                        {"tenant": str(tenant_id), "id": str(def_id)}).scalar_one()
                    if value_count:
                        raise HTTPException(status_code=409, detail={"code": "field_definition_in_use"})
                merged = {**changes, **{k: None for k, v in changes.items() if v is None}}
                assignments = ", ".join(f"{k}=:{k}" for k in merged)
                params = {"tenant": str(tenant_id), "id": str(def_id), **merged}
                updated = connection.execute(
                    text(f"UPDATE client_field_definitions SET {assignments}, version=version+1, updated_at=now() WHERE tenant_id=:tenant AND id=:id RETURNING *"),
                    params).mappings().one()
                insert_audit(connection, tenant_id, identity, action="field_definition.updated",
                             resource_type="field_definition", resource_id=def_id,
                             reason="field definition updated",
                             request_id=request.state.request_id,
                             changed_fields=list(changes.keys()))
            return dict(updated)
        finally:
            engine.dispose()

    # Field Values
    @app.post("/v1/tenants/{tenant_id}/clients/{client_id}/field-values", tags=["crm"], status_code=201)
    def create_field_value(tenant_id: uuid.UUID, client_id: uuid.UUID, payload: FieldValueIn, request: Request,
                           identity=Depends(require_identity)) -> JSONResponse:
        engine = app.state.engine_factory()
        val_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                client_or_404(connection, tenant_id, client_id, role, assigned)
                definition = connection.execute(
                    text("SELECT * FROM client_field_definitions WHERE tenant_id=:tenant AND id=:id FOR SHARE"),
                    {"tenant": str(tenant_id), "id": str(payload.definition_id)}).mappings().one_or_none()
                if definition is None:
                    raise HTTPException(status_code=404, detail={"code": "field_definition_not_found"})
                if definition["entity_type"] != payload.entity_type:
                    raise HTTPException(status_code=422, detail={"code": "entity_type_mismatch"})
                if payload.entity_type == "client" and payload.entity_id != client_id:
                    raise HTTPException(status_code=422, detail={"code": "entity_id_mismatch"})
                validated = _validate_field_value(dict(definition), payload)
                cols = ["tenant_id", "id", "definition_id", "entity_type", "entity_id"] + list(validated.keys())
                vals = [":tenant", ":id", ":def_id", ":entity_type", ":entity_id"] + [f":{k}" for k in validated.keys()]
                params = {"tenant": str(tenant_id), "id": str(val_id), "def_id": str(payload.definition_id),
                          "entity_type": payload.entity_type, "entity_id": str(client_id), **validated}
                connection.execute(
                    text(f"INSERT INTO client_field_values ({', '.join(cols)}) VALUES ({', '.join(vals)})"),
                    params)
                insert_audit(connection, tenant_id, identity, action="field_value.created",
                             resource_type="field_value", resource_id=val_id,
                             reason=f"field value for {definition['key']} set",
                             request_id=request.state.request_id,
                             changed_fields=["value"])
                row = connection.execute(
                    text("SELECT * FROM client_field_values WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(val_id)}).mappings().one()
            return JSONResponse(status_code=201, content={**_row_to_dict(row), **_serialize_field_value(_row_to_dict(row))},
                                headers={"X-Request-ID": str(request.state.request_id)})
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/clients/{client_id}/field-values", tags=["crm"])
    def list_field_values(tenant_id: uuid.UUID, client_id: uuid.UUID, request: Request,
                          identity=Depends(require_identity),
                          limit: int = 20,
                          offset: int = 0) -> dict:
        engine = app.state.engine_factory()
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail={"code": "invalid_limit"})
        if offset < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_offset"})
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.read")
                client_or_404(connection, tenant_id, client_id, role, assigned)
                rows = connection.execute(
                    text("""SELECT v.*, d.key, d.type, d.sensitivity, d.validation
                            FROM client_field_values v
                            JOIN client_field_definitions d ON d.tenant_id=v.tenant_id AND d.id=v.definition_id
                            WHERE v.tenant_id=:tenant AND v.entity_type='client' AND v.entity_id=:client
                            ORDER BY v.updated_at DESC"""),
                    {"tenant": str(tenant_id), "client": str(client_id)}).mappings().all()
                # Owner and manager have clients.sensitive.read per ROLE_PERMISSIONS
                extra_perms = frozenset({"clients.sensitive.read"}) if role in ("owner", "manager") else frozenset()
                visible = [{**dict(row), **_serialize_field_value(dict(row))}
                           for row in rows if _check_field_sensitivity(dict(row), role, extra_perms)]
            # total is the visible count: the pager must never advertise rows the caller cannot see.
            return {"items": visible[offset:offset + limit], "total": len(visible), "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/clients/{client_id}/field-values/{value_id}", tags=["crm"])
    def update_field_value(tenant_id: uuid.UUID, client_id: uuid.UUID, value_id: uuid.UUID,
                           payload: FieldValuePatch, request: Request,
                           identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                client_or_404(connection, tenant_id, client_id, role, assigned)
                current = connection.execute(
                    text("""SELECT v.*, d.type, d.validation FROM client_field_values v
                            JOIN client_field_definitions d ON d.tenant_id=v.tenant_id AND d.id=v.definition_id
                            WHERE v.tenant_id=:tenant AND v.id=:id AND v.entity_type='client'
                              AND v.entity_id=:client FOR UPDATE"""),
                    {"tenant": str(tenant_id), "id": str(value_id), "client": str(client_id)}).mappings().one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "field_value_not_found"})
                if payload.expected_version is None:
                    raise HTTPException(status_code=428, detail={"code": "expected_version_required"})
                if payload.expected_version != current["version"]:
                    raise HTTPException(status_code=409, detail={"code": "field_value_version_conflict"})
                # URL path provides client_id; no entity_type/entity_id in patch payload
                validated = _validate_field_value(dict(current), payload)
                col, val = _build_field_value_update(dict(current), validated)
                updated = connection.execute(
                    text(f"UPDATE client_field_values SET {col}=:val, version=version+1, updated_at=now() WHERE tenant_id=:tenant AND id=:id RETURNING *"),
                    {"tenant": str(tenant_id), "id": str(value_id), "val": val}).mappings().one()
                insert_audit(connection, tenant_id, identity, action="field_value.updated",
                             resource_type="field_value", resource_id=value_id,
                             reason="field value updated",
                             request_id=request.state.request_id,
                             changed_fields=["value"])
            return {**dict(updated), **_serialize_field_value(dict(updated))}
        finally:
            engine.dispose()

    # Visit Notes
    @app.post("/v1/tenants/{tenant_id}/clients/{client_id}/visit-notes", tags=["crm"], status_code=201)
    def create_visit_note(tenant_id: uuid.UUID, client_id: uuid.UUID, payload: VisitNoteIn, request: Request,
                          identity=Depends(require_identity)) -> JSONResponse:
        engine = app.state.engine_factory()
        note_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                client_or_404(connection, tenant_id, client_id, role, assigned)
                if payload.class_ in ("clinical", "allergy") and payload.visibility != "private":
                    raise HTTPException(status_code=422, detail={"code": "sensitive_note_must_be_private"})
                if payload.visibility == "private":
                    _require_field_sensitivity(role, "private")
                author_issuer = getattr(identity, 'issuer', 'unknown')
                author_subject = getattr(identity, 'subject', 'unknown')
                connection.execute(
                    text("""INSERT INTO visit_notes
                            (tenant_id, id, client_id, appointment_id, author_issuer, author_subject, class, visibility, body)
                            VALUES (:tenant, :id, :client, :appointment, :issuer, :subject, :class, :visibility, :body)"""),
                    {"tenant": str(tenant_id), "id": str(note_id), "client": str(client_id),
                     "appointment": str(payload.appointment_id) if payload.appointment_id else None,
                     "issuer": author_issuer, "subject": author_subject,
                     "class": payload.class_, "visibility": payload.visibility, "body": payload.body})
                insert_audit(connection, tenant_id, identity, action="visit_note.created",
                             resource_type="visit_note", resource_id=note_id,
                             reason=f"visit note ({payload.class_}) added",
                             request_id=request.state.request_id,
                             changed_fields=["class", "visibility", "body"])
                row = connection.execute(
                    text("SELECT * FROM visit_notes WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(note_id)}).mappings().one()
            return JSONResponse(status_code=201, content=_row_to_dict(row), headers={"X-Request-ID": str(request.state.request_id)})
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/clients/{client_id}/visit-notes", tags=["crm"])
    def list_visit_notes(tenant_id: uuid.UUID, client_id: uuid.UUID, request: Request,
                         identity=Depends(require_identity),
                         limit: int = 20,
                         offset: int = 0) -> dict:
        engine = app.state.engine_factory()
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail={"code": "invalid_limit"})
        if offset < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_offset"})
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.read")
                client_or_404(connection, tenant_id, client_id, role, assigned)
                # Owner and manager have clients.sensitive.read per ROLE_PERMISSIONS
                has_sensitive = role in ("owner", "manager")
                rows = connection.execute(
                    text("SELECT * FROM visit_notes WHERE tenant_id=:tenant AND client_id=:client ORDER BY created_at DESC"),
                    {"tenant": str(tenant_id), "client": str(client_id)}).mappings().all()
                # Filter before paging so a private note can never shift the page a caller sees.
                visible = [dict(r) for r in rows if _check_note_visibility(dict(r), role, has_sensitive)]
            return {"items": visible[offset:offset + limit], "total": len(visible), "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/clients/{client_id}/visit-notes/{note_id}", tags=["crm"])
    def update_visit_note(tenant_id: uuid.UUID, client_id: uuid.UUID, note_id: uuid.UUID,
                          payload: VisitNotePatch, request: Request,
                          identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                client_or_404(connection, tenant_id, client_id, role, assigned)
                current = connection.execute(
                    text("SELECT * FROM visit_notes WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                    {"tenant": str(tenant_id), "id": str(note_id)}).mappings().one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "visit_note_not_found"})
                if str(current["client_id"]) != str(client_id):
                    raise HTTPException(status_code=422, detail={"code": "client_id_mismatch"})
                if payload.expected_version is None:
                    raise HTTPException(status_code=428, detail={"code": "expected_version_required"})
                # Optimistic concurrency
                expected = payload.model_dump(exclude_unset=True).get("expected_version")
                if expected is not None and expected != current["version"]:
                    raise HTTPException(status_code=409, detail={"code": "visit_note_version_conflict"})
                changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
                if not changes:
                    raise HTTPException(status_code=422, detail={"code": "visit_note_change_required"})
                next_class = changes.get("class_", current["class"])
                next_visibility = changes.get("visibility", current["visibility"])
                if next_class in ("clinical", "allergy") and next_visibility != "private":
                    raise HTTPException(status_code=422, detail={"code": "sensitive_note_must_be_private"})
                if next_visibility == "private":
                    _require_field_sensitivity(role, "private")
                merged = {**changes, **{k: None for k, v in changes.items() if v is None}}
                if "class_" in merged:
                    merged["class"] = merged.pop("class_")
                assignments = ", ".join(f"{k}=:{k}" for k in merged)
                params = {"tenant": str(tenant_id), "id": str(note_id), **merged}
                updated = connection.execute(
                    text(f"UPDATE visit_notes SET {assignments}, version=version+1, updated_at=now() WHERE tenant_id=:tenant AND id=:id RETURNING *"),
                    params).mappings().one()
                insert_audit(connection, tenant_id, identity, action="visit_note.updated",
                             resource_type="visit_note", resource_id=note_id,
                             reason="visit note updated",
                             request_id=request.state.request_id,
                             changed_fields=list(changes.keys()))
            return dict(updated)
        finally:
            engine.dispose()

    # Client Preferences
    @app.post("/v1/tenants/{tenant_id}/clients/{client_id}/preferences", tags=["crm"], status_code=201)
    def create_preference(tenant_id: uuid.UUID, client_id: uuid.UUID, payload: PreferenceIn, request: Request,
                          identity=Depends(require_identity)) -> JSONResponse:
        engine = app.state.engine_factory()
        pref_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                client_or_404(connection, tenant_id, client_id, role, assigned)
                _validate_preference_value(payload.preference_type, payload.value_json)
                connection.execute(
                    text("""INSERT INTO client_preferences
                            (tenant_id, id, client_id, preference_type, service_id, staff_id, location_id, value_json, source)
                            VALUES (:tenant, :id, :client, :ptype, :service, :staff, :location, :value, :source)"""),
                    {"tenant": str(tenant_id), "id": str(pref_id), "client": str(client_id),
                     "ptype": payload.preference_type, "service": str(payload.service_id) if payload.service_id else None,
                     "staff": str(payload.staff_id) if payload.staff_id else None,
                     "location": str(payload.location_id) if payload.location_id else None,
                     "value": json.dumps(payload.value_json), "source": payload.source})
                insert_audit(connection, tenant_id, identity, action="preference.created",
                             resource_type="preference", resource_id=pref_id,
                             reason=f"preference {payload.preference_type} added",
                             request_id=request.state.request_id,
                             changed_fields=["preference_type", "value_json", "source"])
                row = connection.execute(
                    text("SELECT * FROM client_preferences WHERE tenant_id=:tenant AND id=:id"),
                    {"tenant": str(tenant_id), "id": str(pref_id)}).mappings().one()
            return JSONResponse(status_code=201, content=_row_to_dict(row), headers={"X-Request-ID": str(request.state.request_id)})
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/clients/{client_id}/preferences", tags=["crm"])
    def list_preferences(tenant_id: uuid.UUID, client_id: uuid.UUID, request: Request,
                         identity=Depends(require_identity),
                         limit: int = 20,
                         offset: int = 0) -> dict:
        engine = app.state.engine_factory()
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail={"code": "invalid_limit"})
        if offset < 0:
            raise HTTPException(status_code=422, detail={"code": "invalid_offset"})
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.read")
                client_or_404(connection, tenant_id, client_id, role, assigned)
                total = connection.execute(
                    text("""SELECT count(*) FROM client_preferences WHERE tenant_id=:tenant AND client_id=:client
                          AND preference_type NOT IN ('product_allergy', 'sensitivity_note')"""),
                    {"tenant": str(tenant_id), "client": str(client_id)}).scalar_one()
                rows = connection.execute(
                    text("SELECT * FROM client_preferences WHERE tenant_id=:tenant AND client_id=:client "
                         "AND preference_type NOT IN ('product_allergy', 'sensitivity_note') "
                         "ORDER BY preference_type, updated_at DESC LIMIT :limit OFFSET :offset"),
                    {"tenant": str(tenant_id), "client": str(client_id), "limit": limit, "offset": offset}).mappings().all()
            return {"items": [dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/clients/{client_id}/preferences/{pref_id}", tags=["crm"])
    def update_preference(tenant_id: uuid.UUID, client_id: uuid.UUID, pref_id: uuid.UUID,
                          payload: PreferencePatch, request: Request,
                          identity=Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, assigned = membership_scope(connection, tenant_id, identity)
                require_permission(role, "clients.operational.write")
                client_or_404(connection, tenant_id, client_id, role, assigned)
                current = connection.execute(
                    text("SELECT * FROM client_preferences WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                    {"tenant": str(tenant_id), "id": str(pref_id)}).mappings().one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "preference_not_found"})
                if str(current["client_id"]) != str(client_id):
                    raise HTTPException(status_code=422, detail={"code": "client_id_mismatch"})
                if payload.expected_version is None:
                    raise HTTPException(status_code=428, detail={"code": "expected_version_required"})
                if payload.expected_version != current["version"]:
                    raise HTTPException(status_code=409, detail={"code": "preference_version_conflict"})
                changes = payload.model_dump(exclude_unset=True, exclude={"expected_version"})
                if not changes:
                    raise HTTPException(status_code=422, detail={"code": "preference_change_required"})
                merged = {**changes, **{k: None for k, v in changes.items() if v is None}}
                if "value_json" in merged:
                    _validate_preference_value(current["preference_type"], merged["value_json"])
                    merged["value_json"] = json.dumps(merged["value_json"])
                assignments = ", ".join(f"{k}=:{k}" for k in merged)
                params = {"tenant": str(tenant_id), "id": str(pref_id), **merged}
                updated = connection.execute(
                    text(f"UPDATE client_preferences SET {assignments}, version=version+1, updated_at=now() WHERE tenant_id=:tenant AND id=:id RETURNING *"),
                    params).mappings().one()
                insert_audit(connection, tenant_id, identity, action="preference.updated",
                             resource_type="preference", resource_id=pref_id,
                             reason="preference updated",
                             request_id=request.state.request_id,
                             changed_fields=list(changes.keys()))
            return dict(updated)
        finally:
            engine.dispose()

