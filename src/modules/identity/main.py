from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from salon_platform.tenant import list_tenant_locations as list_tenant_location_rows, set_tenant_context
from modules.crm.main import register_client_routes, normalize_email, normalize_phone
from modules.crm.imports import register_import_routes
from modules.crm.merges import register_merge_routes
from modules.crm.privacy import register_privacy_routes
from modules.catalogue.main import register_catalogue_routes
from modules.catalogue.staff import register_staff_routes
from modules.catalogue.hours import register_hours_routes
from modules.catalogue.resources import register_resource_routes
from modules.closure_impacts import register_closure_impacts_routes
from modules.booking.availability import register_availability_routes, resolve_public_location
from modules.booking.holds import register_hold_routes
from modules.booking.calendar import register_calendar_routes
from modules.booking.transitions import register_transition_routes
from modules.booking.receipts import register_receipt_routes
from modules.analytics import register_analytics_routes
from modules.budgets import register_budget_routes
from modules.company import register_company_routes, start_trial
from modules.communications.callbacks import register_callback_routes
from modules.communications.inbox import register_inbox_routes
from modules.communications.senders import register_sender_routes
from modules.retention import register_retention_routes
from modules.segments import register_segment_routes
from modules.support import register_support_routes
from modules.trace import register_trace_routes
from modules.communications.whatsapp import register_wa_sender_routes

DEMO_SALON = {"name": "Maple Demo Salon", "timezone": "America/Toronto"}


class RecordingDeliveryAdapter:
    """Synthetic-only sink: records intents and never sends externally."""

    def __init__(self) -> None:
        self.records: list[dict[str, str]] = []

    def send(
        self, *, channel: Literal["email", "sms", "whatsapp"], recipient: str, template: str,
        sender: str | None = None,
    ) -> dict[str, str]:
        self.records.append({"channel": channel, "recipient": recipient, "template": template,
                             "sender": sender})
        return {"status": "recorded", "id": f"recorded-{len(self.records)}"}


def database_is_reachable() -> bool:
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        return False
    engine = create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 2})
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar_one() == 1
    except SQLAlchemyError:
        return False
    finally:
        engine.dispose()




@dataclass(frozen=True)
class AuthenticatedIdentity:
    issuer: str
    subject: str
    surface: str = "staff"
    email: str | None = None
    email_verified: bool = False
    auth_time: float | datetime | None = None


def _as_epoch(value: float | datetime | None) -> float | None:
    """Normalise an auth timestamp to epoch seconds.

    A session resolved from the database carries a `timestamptz`, which psycopg
    hands back as an aware `datetime`; a test or in-process identity may carry a
    float. Comparing either against `time.time()` requires the same units.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        # A session read back from PostgreSQL is timezone-aware. A naive value is
        # treated as UTC rather than host-local, so the freshness window cannot
        # silently shift with the machine's timezone.
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).timestamp()
        return value.timestamp()
    return float(value)


def _cookie_name(surface: str) -> str:
    return "salon_staff_session" if surface == "staff" else "salon_company_session"


def _flow_cookie_name(surface: str) -> str:
    return f"salon_{surface}_oidc_flow"


def _auth_config(surface: str) -> dict[str, str]:
    prefix = "STAFF" if surface == "staff" else "COMPANY"
    names = {
        "issuer": f"{prefix}_OIDC_ISSUER",
        "client_id": f"{prefix}_OIDC_CLIENT_ID",
        "authorization_endpoint": f"{prefix}_OIDC_AUTHORIZATION_ENDPOINT",
        "token_endpoint": f"{prefix}_OIDC_TOKEN_ENDPOINT",
        "jwks_url": f"{prefix}_OIDC_JWKS_URL",
    }
    values = {key: os.getenv(env, "").strip() for key, env in names.items()}
    if not all(values.values()):
        raise HTTPException(status_code=503, detail={"code": "identity_provider_unconfigured"})
    if not values["issuer"].startswith("https://") or not values["authorization_endpoint"].startswith("https://") or not values["token_endpoint"].startswith("https://") or not values["jwks_url"].startswith("https://"):
        raise HTTPException(status_code=503, detail={"code": "identity_provider_configuration_invalid"})
    return values


def _state_key() -> bytes:
    secret = os.getenv("AUTH_STATE_SECRET", "").encode()
    if len(secret) < 32:
        raise HTTPException(status_code=503, detail={"code": "identity_provider_unconfigured"})
    return secret


def _pack_flow(data: dict[str, str | int]) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).rstrip(b"=").decode()
    signature = hmac.new(_state_key(), encoded.encode(), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def _unpack_flow(value: str, surface: str) -> dict[str, str | int]:
    try:
        encoded, signature = value.rsplit(".", 1)
        expected = hmac.new(_state_key(), encoded.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        padding = "=" * (-len(encoded) % 4)
        data = json.loads(base64.urlsafe_b64decode(encoded + padding))
        if data.get("surface") != surface or int(data.get("expires_at", 0)) < int(time.time()):
            raise ValueError("expired")
        return data
    except Exception as exc:
        raise HTTPException(status_code=400, detail={"code": "invalid_login_state"}) from exc


def _validate_id_token(token: str, config: dict[str, str], nonce: str, surface: str) -> dict:
    try:
        jwks = jwt.PyJWKClient(config["jwks_url"], timeout=5)
        key = jwks.get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], audience=config["client_id"], issuer=config["issuer"],
                            options={"require": ["exp", "iat", "sub", "iss", "aud", "nonce", "token_use"]})
        if claims.get("token_use") != "id" or not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
            raise ValueError("token type or nonce")
        if surface == "company" and "mfa" not in claims.get("amr", []):
            raise ValueError("company MFA is required")
        return claims
    except Exception as exc:
        raise HTTPException(status_code=401, detail={"code": "identity_token_rejected"}) from exc


def _session_identity(engine: Engine, token: str, surface: str) -> AuthenticatedIdentity | None:
    session_hash = hashlib.sha256(token.encode()).hexdigest()
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT s.issuer,s.subject,u.email,u.email_verified,u.auth_time FROM auth_sessions s JOIN identity_users u ON u.issuer=s.issuer AND u.subject=s.subject WHERE s.session_hash=:session_hash AND s.surface=:surface AND s.expires_at > now()"),
            {"session_hash": session_hash, "surface": surface},
        ).mappings().one_or_none()
    if row is None:
        return None
    return AuthenticatedIdentity(
        row["issuer"], row["subject"], surface,
        email=row["email"], email_verified=row["email_verified"], auth_time=row["auth_time"],
    )


def require_identity(request: Request) -> AuthenticatedIdentity:
    token = request.cookies.get(_cookie_name("staff"))
    if not token:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    engine = request.app.state.engine_factory()
    try:
        identity = _session_identity(engine, token, "staff")
    finally:
        engine.dispose()
    if identity is None:
        raise HTTPException(status_code=401, detail={"code": "authentication_required"})
    request.state.authenticated_identity = identity
    return identity


def require_company_identity(request: Request) -> AuthenticatedIdentity:
    token = request.cookies.get(_cookie_name("company"))
    if not token:
        raise HTTPException(status_code=401, detail={"code": "company_authentication_required"})
    engine = request.app.state.engine_factory()
    try:
        identity = _session_identity(engine, token, "company")
    finally:
        engine.dispose()
    if identity is None:
        raise HTTPException(status_code=401, detail={"code": "company_authentication_required"})
    return identity


class OnboardingRequest(BaseModel):
    business_name: str = Field(min_length=1, max_length=120)
    location_name: str = Field(min_length=1, max_length=120)
    timezone: str = Field(min_length=1, max_length=64)

    @field_validator("business_name", "location_name")
    @classmethod
    def trim_nonempty(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must contain a non-whitespace character")
        return normalized

    @field_validator("timezone")
    @classmethod
    def valid_iana_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("must be a valid IANA timezone") from exc
        return value



class InviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: Literal["manager", "receptionist", "stylist", "analyst"]
    location_ids: list[uuid.UUID] = Field(min_length=1, max_length=50)

    @field_validator("email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized.count("@") != 1 or any(ch.isspace() for ch in normalized):
            raise ValueError("must be a valid email address")
        local, domain = normalized.split("@")
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("must be a valid email address")
        return normalized


class MembershipPatch(BaseModel):
    role: Literal["manager", "receptionist", "stylist", "analyst"] | None = None
    location_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)
    reason: str = Field(min_length=3, max_length=240)

    @field_validator("reason")
    @classmethod
    def trim_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason is required")
        return value


class OwnershipTransferRequest(BaseModel):
    target_email: str = Field(min_length=3, max_length=254)

    @field_validator("target_email")
    @classmethod
    def normalized_email(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized.count("@") != 1 or any(ch.isspace() for ch in normalized):
            raise ValueError("must be a valid email address")
        return normalized


class DeactivateRequest(BaseModel):
    """Body form of the deactivation audit reason, kept off the query string."""

    reason: str = Field(min_length=3, max_length=240)

    @field_validator("reason")
    @classmethod
    def trim_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("reason is required")
        return value


class SetupStepPatch(BaseModel):
    state: Literal["pending", "complete", "skipped"]

    @field_validator("state")
    @classmethod
    def known_step_state(cls, value: str) -> str:
        if value == "skipped":
            raise ValueError("a setup step cannot be skipped")
        return value


HEX_COLOUR = re.compile(r"^#[0-9a-f]{6}$")


class SettingsPatch(BaseModel):
    """FR-008 branding, cancellation terms and notification preferences.

    Validated here rather than by the JSONB column alone, because data-model.md:117
    restricts JSONB to validated extensible configuration. A field sent explicitly as
    `null` clears that key; a field omitted leaves it alone.
    """

    brand_name: str | None = Field(default=None, min_length=1, max_length=120)
    primary_colour: str | None = Field(default=None, max_length=7)
    accent_colour: str | None = Field(default=None, max_length=7)
    cancellation_policy: str | None = Field(default=None, min_length=1, max_length=2000)
    notification_language: Literal["en-CA", "fr-CA"] | None = None
    appointment_reminders: bool | None = None
    marketing_messages: bool | None = None

    @field_validator("brand_name", "primary_colour", "accent_colour", "cancellation_policy", mode="before")
    @classmethod
    def trim_before_length(cls, value: object) -> object:
        # Runs before the field length constraints, so padded input is tolerated instead
        # of rejected for its padding. An emptied field arrives here as "" and is refused
        # by min_length, which is the intent.
        return value.strip() if isinstance(value, str) else value

    @field_validator("primary_colour", "accent_colour")
    @classmethod
    def hex_colour(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not HEX_COLOUR.match(value.lower()):
            raise ValueError("must be a #rrggbb colour")
        return value.lower()


def _slug_base(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", ascii_name).strip("-")[:40] or "salon"


def _create_tenant(engine: Engine, identity: AuthenticatedIdentity, request: OnboardingRequest, request_id: uuid.UUID) -> tuple[dict[str, str], bool]:
    fingerprint = hashlib.sha256(json.dumps(request.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    with engine.begin() as connection:
        user = connection.execute(
            text("""INSERT INTO identity_users (issuer,subject,email,email_verified,auth_time)
                   VALUES (:issuer,:subject,:email,:email_verified,to_timestamp(:auth_time))
                   ON CONFLICT (issuer,subject) DO UPDATE SET issuer=EXCLUDED.issuer
                   RETURNING id,tenant_id,onboarding_fingerprint"""),
            {"issuer": identity.issuer, "subject": identity.subject, "email": identity.email if identity.email_verified else None,
             "email_verified": identity.email_verified, "auth_time": _as_epoch(identity.auth_time)},
        ).mappings().one()
        if user["tenant_id"] is not None:
            if user["onboarding_fingerprint"] != fingerprint:
                raise HTTPException(status_code=409, detail={"code": "onboarding_already_completed"})
            tenant_id = str(user["tenant_id"])
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": tenant_id})
            result = connection.execute(
                text("""SELECT t.id AS tenant_id, t.display_name AS business_name,
                             l.id AS location_id, l.display_name AS location_name,
                             l.timezone, l.slug, l.booking_slug, m.role
                      FROM tenants t JOIN locations l ON l.tenant_id=t.id
                      JOIN memberships m ON m.tenant_id=t.id
                      WHERE t.id=:tenant_id AND m.user_id=:user_id
                      ORDER BY l.created_at LIMIT 1"""),
                {"tenant_id": tenant_id, "user_id": str(user["id"])},
            ).mappings().one_or_none()
            if result is None:
                raise HTTPException(status_code=409, detail={"code": "onboarding_state_unavailable"})
            return {key: str(value) if isinstance(value, uuid.UUID) else value for key, value in result.items()}, False

        tenant_id, location_id = str(uuid.uuid4()), str(uuid.uuid4())
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": tenant_id})
        connection.execute(
            text("INSERT INTO tenants (id, display_name) VALUES (:id, :name)"),
            {"id": tenant_id, "name": request.business_name},
        )
        slug = _slug_base(request.location_name)
        booking_slug = f"{_slug_base(request.business_name)}-{tenant_id.replace('-', '')[:10]}"
        connection.execute(
            text("""INSERT INTO locations (id, tenant_id, display_name, timezone, slug, booking_slug)
                   VALUES (:id, :tenant_id, :name, :timezone, :slug, :booking_slug)"""),
            {"id": location_id, "tenant_id": tenant_id, "name": request.location_name,
             "timezone": request.timezone, "slug": slug, "booking_slug": booking_slug},
        )
        connection.execute(
            text("INSERT INTO memberships (tenant_id, user_id, role) VALUES (:tenant_id, :user_id, 'owner')"),
            {"tenant_id": tenant_id, "user_id": str(user["id"])},
        )
        # TASK-046 / FR-066: every new salon starts on an Essential trial, in
        # the same transaction as the tenant itself.
        start_trial(connection, tenant_id=tenant_id)
        connection.execute(
            text("UPDATE identity_users SET tenant_id=:tenant_id, onboarding_fingerprint=:fingerprint WHERE id=:user_id"),
            {"tenant_id": tenant_id, "fingerprint": fingerprint, "user_id": str(user["id"])},
        )
        connection.execute(
            text("""INSERT INTO tenant_audit_events
                   (tenant_id, actor_issuer, actor_subject, action, resource_type, resource_id, reason, request_id, changed_fields)
                   VALUES (:tenant_id,:issuer,:subject,'tenant.onboarded','tenant',:resource_id,
                           'owner initiated onboarding',:request_id,:changed_fields)"""),
            {"tenant_id": tenant_id, "issuer": identity.issuer, "subject": identity.subject,
             "resource_id": tenant_id, "request_id": str(request_id),
             "changed_fields": ["display_name", "default_currency", "location", "owner_membership"]},
        )
        return {"tenant_id": tenant_id, "business_name": request.business_name,
                "location_id": location_id, "location_name": request.location_name,
                "timezone": request.timezone, "slug": slug, "booking_slug": booking_slug, "role": "owner"}, True




def _insert_audit(connection, tenant_id: uuid.UUID | str, identity: AuthenticatedIdentity, *, action: str,
                  resource_type: str, resource_id: uuid.UUID | str, reason: str, request_id: uuid.UUID,
                  changed_fields: list[str]) -> None:
    connection.execute(
        text("""INSERT INTO tenant_audit_events
               (tenant_id,actor_issuer,actor_subject,action,resource_type,resource_id,reason,request_id,changed_fields)
               VALUES (:tenant,:issuer,:subject,:action,:resource_type,:resource_id,:reason,:request_id,:fields)"""),
        {"tenant": str(tenant_id), "issuer": identity.issuer, "subject": identity.subject, "action": action,
         "resource_type": resource_type, "resource_id": str(resource_id), "reason": reason,
         "request_id": str(request_id), "fields": changed_fields},
    )


def _require_owner(connection, tenant_id: uuid.UUID, identity: AuthenticatedIdentity) -> uuid.UUID:
    role, user_id, _ = _membership_scope(connection, tenant_id, identity)
    _require_permission(role, "team.manage")
    return user_id


def _validate_location_assignments(connection, tenant_id: uuid.UUID, location_ids: list[uuid.UUID]) -> None:
    if not location_ids:
        raise HTTPException(status_code=422, detail={"code": "at_least_one_location_required"})
    rows = connection.execute(text("SELECT id FROM locations WHERE tenant_id=:tenant AND id=ANY(:locations)"),
                              {"tenant": str(tenant_id), "locations": location_ids}).scalars().all()
    if {str(value) for value in rows} != {str(value) for value in location_ids}:
        raise HTTPException(status_code=404, detail={"code": "location_not_found"})


def _fresh_reauthentication(identity: AuthenticatedIdentity) -> bool:
    authenticated_at = _as_epoch(identity.auth_time)
    if authenticated_at is None:
        return False
    age = time.time() - authenticated_at
    return -60 <= age <= 300


def _lock_owners_and_target(connection, tenant_id: uuid.UUID, target_user_id: uuid.UUID | str) -> dict[str, str]:
    """Lock every owner membership plus one target membership, in one deterministic order.

    Ownership transfer acceptance and owner deactivation both move owner rows, and
    both run concurrently in practice. They must therefore serialise on the *same*
    rows in the *same* order: locking the target first in one path and the owner set
    first in the other both opens a window where the tenant can fall to zero owners,
    and reversing the order instead just trades that for a deadlock.
    """
    rows = connection.execute(
        text("""SELECT user_id::text AS user_id, role FROM memberships
                WHERE tenant_id=:tenant AND (role='owner' OR user_id=:target)
                ORDER BY user_id FOR UPDATE"""),
        {"tenant": str(tenant_id), "target": str(target_user_id)},
    ).mappings().all()
    return {row["user_id"]: row["role"] for row in rows}


# FR-007 (requirements.md:38) in order. `location` and `qualified_staff` are derived, not
# stored: TASK-002 always creates a location and TASK-007 can always be read as "has staff".
SETUP_STEPS: tuple[str, ...] = ("business_identity", "location", "hours", "service",
                                "qualified_staff", "public_link", "communication_setup", "import")
STORED_SETUP_STEPS: frozenset[str] = frozenset({"business_identity", "hours", "service",
                                                "public_link", "communication_setup", "import"})
# Booking readiness is deliberately narrower than the checklist: these are the steps
# without which a public page cannot honestly take a booking (requirements.md:153).
BOOKING_BLOCKING_STEPS: frozenset[str] = frozenset({"location", "hours", "service", "public_link"})


def _setup_state(connection, tenant_id: uuid.UUID) -> dict[str, dict[str, str]]:
    """Merge stored step state with the facts derivable from existing records."""
    stored = {
        row["step"]: row["state"]
        for row in connection.execute(text("SELECT step,state FROM tenant_setup_steps WHERE tenant_id=:tenant"),
                                      {"tenant": str(tenant_id)}).mappings().all()
    }
    has_location = connection.execute(text("SELECT count(*) FROM locations WHERE tenant_id=:tenant"),
                                      {"tenant": str(tenant_id)}).scalar_one() > 0
    has_staff = connection.execute(
        text("SELECT count(*) FROM memberships m WHERE m.tenant_id=:tenant AND m.role<>'owner'"),
        {"tenant": str(tenant_id)}).scalar_one() > 0
    derived = {"location": has_location, "qualified_staff": has_staff}
    return {
        step: "complete" if derived.get(step) else stored.get(step, "pending")
        for step in SETUP_STEPS
    }


def _booking_readiness(state: dict[str, dict[str, str]]) -> dict:
    missing = [step for step in SETUP_STEPS if step in BOOKING_BLOCKING_STEPS and state[step] != "complete"]
    warnings = [step for step in ("qualified_staff",) if state[step] != "complete"]
    # `hours`, `service` and `public_link` are satisfied by the tenant's own declaration
    # until TASK-015 and TASK-017 give them tables to check against. While any of them is
    # in this list, accepting_bookings is the tenant saying so, not evidence that a
    # bookable slot exists. `location` is derived, so it never appears here.
    unverified = [step for step in SETUP_STEPS
                  if step in BOOKING_BLOCKING_STEPS and step in STORED_SETUP_STEPS
                  and state[step] == "complete"]
    return {"accepting_bookings": not missing, "missing_setup": missing,
            "setup_warnings": warnings, "unverified_setup": unverified}


def _public_brand(settings: dict) -> dict:
    """Only the brand fields a public page may render. Never the rest of the settings blob."""
    return {key: settings[key] for key in ("brand_name", "primary_colour", "accent_colour") if key in settings}


def _expire_stale_invitations(connection, tenant_id: uuid.UUID, identity: "AuthenticatedIdentity | None" = None,
                             request_id: uuid.UUID | None = None) -> None:
    """Move timed-out invitations to their declared 'expired' state.

    `pending_invitation_email_unique` and `one_pending_owner_transfer` are both
    partial on `state='pending'`, so an invitation left in 'pending' past its
    deadline would block that email - and a transfer would block the tenant -
    permanently.
    """
    ids = connection.execute(
        text("""SELECT id FROM tenant_invitations
                WHERE tenant_id=:tenant AND state='pending' AND expires_at <= now()
                ORDER BY id FOR UPDATE"""),
        {"tenant": str(tenant_id)},
    ).scalars().all()
    for invitation_id in ids:
        connection.execute(text("UPDATE tenant_invitations SET state='expired' WHERE tenant_id=:tenant AND id=:id"),
                           {"tenant": str(tenant_id), "id": str(invitation_id)})
    if identity is not None and request_id is not None:
        for invitation_id in ids:
            _insert_audit(connection, tenant_id, identity, action="membership.invitation_expired",
                          resource_type="invitation", resource_id=invitation_id,
                          reason="invitation window closed before it was accepted",
                          request_id=request_id, changed_fields=["state"])


def _expire_stale_transfers(connection, tenant_id: uuid.UUID, identity: "AuthenticatedIdentity | None" = None,
                            request_id: uuid.UUID | None = None) -> None:
    ids = connection.execute(
        text("""SELECT id FROM ownership_transfers
                WHERE tenant_id=:tenant AND state='pending' AND expires_at <= now()
                ORDER BY id FOR UPDATE"""),
        {"tenant": str(tenant_id)},
    ).scalars().all()
    for transfer_id in ids:
        connection.execute(text("UPDATE ownership_transfers SET state='expired' WHERE tenant_id=:tenant AND id=:id"),
                           {"tenant": str(tenant_id), "id": str(transfer_id)})
    if identity is not None and request_id is not None:
        for transfer_id in ids:
            _insert_audit(connection, tenant_id, identity, action="ownership.transfer_expired",
                          resource_type="ownership_transfer", resource_id=transfer_id,
                          reason="ownership transfer window closed before it was accepted",
                          request_id=request_id, changed_fields=["state"])

ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset({"*"}),
    "manager": frozenset({"tenant.summary.read", "locations.read", "appointments.read", "appointments.manage",
                           "clients.operational.read", "clients.operational.write", "clients.sensitive.read",
                           "clients.merge", "clients.privacy.manage", "services.read", "services.manage",
                           "staff.read", "staff.manage", "hours.read", "hours.manage", "resources.read", "resources.manage", "reports.read",
                           "closures.read", "closures.manage", "communications.manage", "inbox.read",
                           "retention.read", "retention.manage"}),
    "receptionist": frozenset({"locations.read", "appointments.read", "appointments.manage", "clients.operational.read",
                               "services.read", "staff.read", "hours.read", "resources.read", "closures.read", "inbox.read",
                               "retention.read"}),
    "stylist": frozenset({"locations.read", "appointments.own.read", "clients.assigned.read", "services.read",
                          "staff.read", "hours.read", "resources.read"}),
    "analyst": frozenset({"reports.aggregate.read"}),
}
CLIENT_VISIBLE_FIELDS: dict[str, frozenset[str]] = {
    "owner": frozenset({"display_name", "preferred_name", "locale", "lifecycle", "contact_summary",
                        "preferred_location_name", "preferred_staff_name", "service_preferences"}),
    "manager": frozenset({"display_name", "preferred_name", "locale", "lifecycle", "contact_summary",
                          "preferred_location_name", "preferred_staff_name", "service_preferences"}),
    "receptionist": frozenset({"display_name", "preferred_name", "locale", "lifecycle", "contact_summary",
                               "preferred_location_name", "preferred_staff_name"}),
    "stylist": frozenset({"display_name", "preferred_name", "locale", "preferred_staff_name", "service_preferences"}),
    "analyst": frozenset(),
}


def project_client_fields(role: str, values: dict, extra_permissions: frozenset[str] = frozenset()) -> dict:
    visible = CLIENT_VISIBLE_FIELDS.get(role, frozenset())
    if role in {"owner", "manager"} and "clients.sensitive.read" in extra_permissions:
        visible = visible | {"private_note"}
    return {key: value for key, value in values.items() if key in visible}


def _membership_scope(connection, tenant_id: uuid.UUID, identity: AuthenticatedIdentity) -> tuple[str, uuid.UUID, list[uuid.UUID]]:
    set_tenant_context(connection, tenant_id)
    row = connection.execute(
        text("""SELECT m.user_id, m.role FROM memberships m
               JOIN identity_users u ON u.id=m.user_id
               WHERE m.tenant_id=:tenant_id AND u.issuer=:issuer AND u.subject=:subject"""),
        {"tenant_id": str(tenant_id), "issuer": identity.issuer, "subject": identity.subject},
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail={"code": "tenant_not_found"})
    locations = connection.execute(
        text("SELECT location_id FROM membership_locations WHERE tenant_id=:tenant_id AND user_id=:user_id"),
        {"tenant_id": str(tenant_id), "user_id": str(row["user_id"])},
    ).scalars().all()
    return row["role"], row["user_id"], locations


def _require_permission(role: str, permission: str) -> None:
    grants = ROLE_PERMISSIONS.get(role, frozenset())
    if "*" not in grants and permission not in grants:
        raise HTTPException(status_code=403, detail={"code": "permission_denied"})


def create_app(database_probe: Callable[[], bool] = database_is_reachable, engine_factory: Callable[[], Engine] | None = None) -> FastAPI:
    app = FastAPI(title="Salon Operations API", version="0.1.0")
    app.state.engine_factory = engine_factory or (lambda: create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True, connect_args={"connect_timeout": 2}))
    app.state.delivery_adapter = RecordingDeliveryAdapter()

    @app.middleware("http")
    async def trace_requests(request: Request, call_next):
        # TASK-053 / FR-077: every request carries a trace id end to end. An
        # inbound X-Trace-ID is echoed when it parses as a UUID; otherwise a
        # fresh one is minted. It lands on request.state for handlers and on
        # the response so callers (and tests) can correlate.
        incoming = request.headers.get("x-trace-id", "")
        try:
            trace_id = str(uuid.UUID(incoming)) if incoming else str(uuid.uuid4())
        except (ValueError, AttributeError):
            trace_id = str(uuid.uuid4())
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-ID"] = trace_id
        return response

    @app.middleware("http")
    async def protect_cookie_mutations(request: Request, call_next):
        raw_request_id = request.headers.get("x-request-id", "")
        try:
            request_id = uuid.UUID(raw_request_id) if raw_request_id else uuid.uuid4()
        except (ValueError, AttributeError):
            request_id = uuid.uuid4()
        request.state.request_id = request_id
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and (
            request.cookies.get(_cookie_name("staff")) or request.cookies.get(_cookie_name("company"))
        ):
            configured = [os.getenv("WEB_BASE_URL", "").rstrip("/")]
            configured.extend(value.strip().rstrip("/") for value in os.getenv("CSRF_ALLOWED_ORIGINS", "").split(",") if value.strip())
            if not request.headers.get("origin") or request.headers["origin"].rstrip("/") not in configured:
                rejected = JSONResponse(status_code=403, content={"detail": {"code": "origin_rejected"}})
                rejected.headers["X-Request-ID"] = str(request_id)
                return rejected
        response = await call_next(request)
        response.headers["X-Request-ID"] = str(request_id)
        return response


    @app.get("/v1/tenants/{tenant_id}/locations", tags=["identity"])
    def list_tenant_locations(tenant_id: uuid.UUID, identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, list[dict[str, str]]]:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, user_id, assigned = _membership_scope(connection, tenant_id, identity)
                _require_permission(role, "locations.read")
                if role == "owner":
                    rows = list_tenant_location_rows(connection, tenant_id)
                elif assigned:
                    rows = connection.execute(text("""SELECT id::text AS id, slug, booking_slug, display_name, timezone
                                                      FROM locations WHERE tenant_id=:tenant AND id=ANY(:locations) ORDER BY created_at"""),
                                              {"tenant": str(tenant_id), "locations": assigned}).mappings().all()
                    rows = [dict(row) for row in rows]
                else:
                    rows = []
                return {"items": rows}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/audit", tags=["identity"])
    def tenant_audit(tenant_id: uuid.UUID, identity: AuthenticatedIdentity = Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = _membership_scope(connection, tenant_id, identity)
                _require_permission(role, "audit.read")
                rows = connection.execute(text("""SELECT id::text AS id, actor_issuer, actor_subject, action, resource_type,
                                                       resource_id::text AS resource_id, reason, request_id::text AS request_id,
                                                       changed_fields, occurred_at
                                                FROM tenant_audit_events WHERE tenant_id=:tenant
                                                ORDER BY occurred_at DESC, id LIMIT 100"""),
                                          {"tenant": str(tenant_id)}).mappings().all()
                return {"items": [{**dict(row), "occurred_at": row["occurred_at"].isoformat()} for row in rows]}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/memberships", tags=["identity"])
    def list_memberships(tenant_id: uuid.UUID, identity: AuthenticatedIdentity = Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                _require_owner(connection, tenant_id, identity)
                rows = connection.execute(text("""SELECT u.id::text AS user_id,u.email,u.email_verified,m.role,m.created_at,
                                                       COALESCE(array_agg(ml.location_id::text) FILTER (WHERE ml.location_id IS NOT NULL),ARRAY[]::text[]) AS location_ids
                                                FROM memberships m JOIN identity_users u ON u.id=m.user_id
                                                LEFT JOIN membership_locations ml ON ml.tenant_id=m.tenant_id AND ml.user_id=m.user_id
                                                WHERE m.tenant_id=:tenant GROUP BY u.id,u.email,u.email_verified,m.role,m.created_at
                                                ORDER BY m.created_at,u.id"""), {"tenant": str(tenant_id)}).mappings().all()
                return {"items": [{**dict(row), "created_at": row["created_at"].isoformat()} for row in rows]}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/invitations", tags=["identity"])
    def list_invitations(tenant_id: uuid.UUID, identity: AuthenticatedIdentity = Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                _require_owner(connection, tenant_id, identity)
                # A read must not mutate: report the state the deadline implies
                # rather than writing the flip here. The write happens on the
                # invitation and transfer command paths, where it is audited.
                rows = connection.execute(text("""SELECT id::text AS id,email,role,created_at,expires_at,accepted_at,revoked_at,
                                                        CASE WHEN state='pending' AND expires_at <= now() THEN 'expired' ELSE state END AS state
                                                FROM tenant_invitations WHERE tenant_id=:tenant ORDER BY created_at DESC LIMIT 100"""),
                                          {"tenant": str(tenant_id)}).mappings().all()
                return {"items": [{**dict(row), "created_at": row["created_at"].isoformat(),
                                    "expires_at": row["expires_at"].isoformat(),
                                    "accepted_at": row["accepted_at"].isoformat() if row["accepted_at"] else None,
                                    "revoked_at": row["revoked_at"].isoformat() if row["revoked_at"] else None} for row in rows]}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/invitations", tags=["identity"], status_code=201)
    def create_invitation(tenant_id: uuid.UUID, payload: InviteRequest, request: Request,
                          identity: AuthenticatedIdentity = Depends(require_identity)) -> JSONResponse:
        engine = app.state.engine_factory()
        invitation_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                _require_owner(connection, tenant_id, identity)
                _validate_location_assignments(connection, tenant_id, payload.location_ids)
                existing = connection.execute(text("""SELECT user_id FROM memberships
                            WHERE tenant_id=:tenant AND user_id IN (SELECT id FROM identity_users WHERE email=:email AND email_verified)
                            LIMIT 1"""),
                                              {"tenant": str(tenant_id), "email": payload.email}).scalar_one_or_none()
                if existing is not None:
                    raise HTTPException(status_code=409, detail={"code": "already_a_member"})
                _expire_stale_invitations(connection, tenant_id, identity, request.state.request_id)
                pending = connection.execute(
                    text("""SELECT id FROM tenant_invitations
                            WHERE tenant_id=:tenant AND email=:email AND state='pending'
                            LIMIT 1"""),
                    {"tenant": str(tenant_id), "email": payload.email},
                ).scalar_one_or_none()
                if pending is not None:
                    raise HTTPException(status_code=409, detail={"code": "invitation_already_pending"})
                try:
                    connection.execute(text("""INSERT INTO tenant_invitations
                        (tenant_id,id,email,role,invited_by_issuer,invited_by_subject,state,expires_at)
                        VALUES (:tenant,:id,:email,:role,:issuer,:subject,'pending',now()+interval '7 days')"""),
                        {"tenant": str(tenant_id), "id": str(invitation_id), "email": payload.email, "role": payload.role,
                         "issuer": identity.issuer, "subject": identity.subject})
                except IntegrityError as exc:
                    # A concurrent invite for the same email won the race. The partial
                    # unique index is the invariant; surface it as the same 409.
                    raise HTTPException(status_code=409, detail={"code": "invitation_already_pending"}) from exc
                for location_id in payload.location_ids:
                    connection.execute(text("INSERT INTO invitation_locations (tenant_id,invitation_id,location_id) VALUES (:tenant,:invitation,:location)"),
                                       {"tenant": str(tenant_id), "invitation": str(invitation_id), "location": str(location_id)})
                _insert_audit(connection, tenant_id, identity, action="membership.invited", resource_type="invitation",
                              resource_id=invitation_id, reason="owner invited staff", request_id=request.state.request_id,
                              changed_fields=["email", "role", "location_assignments"])
            app.state.delivery_adapter.send(channel="email", recipient=payload.email, template="staff-invitation")
            return JSONResponse(status_code=201, content={"invitation_id": str(invitation_id), "email": payload.email,
                                 "role": payload.role, "state": "pending", "delivery_status": "recorded_only"},
                                 headers={"X-Request-ID": str(request.state.request_id)})
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/invitations/{invitation_id}/revoke", tags=["identity"])
    def revoke_invitation(tenant_id: uuid.UUID, invitation_id: uuid.UUID, request: Request,
                          identity: AuthenticatedIdentity = Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                _require_owner(connection, tenant_id, identity)
                row = connection.execute(text("SELECT state FROM tenant_invitations WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                                         {"tenant": str(tenant_id), "id": str(invitation_id)}).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={"code": "invitation_not_found"})
                if row["state"] != "pending":
                    raise HTTPException(status_code=409, detail={"code": "invitation_not_pending"})
                connection.execute(text("UPDATE tenant_invitations SET state='revoked',revoked_at=now() WHERE tenant_id=:tenant AND id=:id"),
                                   {"tenant": str(tenant_id), "id": str(invitation_id)})
                _insert_audit(connection, tenant_id, identity, action="membership.invitation_revoked", resource_type="invitation",
                              resource_id=invitation_id, reason="owner revoked invitation", request_id=request.state.request_id,
                              changed_fields=["state"])
            return {"invitation_id": str(invitation_id), "state": "revoked"}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/invitations/{invitation_id}/accept", tags=["identity"])
    def accept_invitation(tenant_id: uuid.UUID, invitation_id: uuid.UUID, request: Request,
                          identity: AuthenticatedIdentity = Depends(require_identity)) -> JSONResponse:
        if not identity.email_verified or not identity.email:
            raise HTTPException(status_code=403, detail={"code": "verified_email_required"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                _expire_stale_invitations(connection, tenant_id, identity, request.state.request_id)
                user = connection.execute(text("SELECT id,email,email_verified FROM identity_users WHERE issuer=:issuer AND subject=:subject"),
                                          {"issuer": identity.issuer, "subject": identity.subject}).mappings().one_or_none()
                if user is None or not user["email_verified"] or user["email"] != identity.email.casefold():
                    raise HTTPException(status_code=403, detail={"code": "verified_email_required"})
                invitation = connection.execute(text("SELECT email,role,state,expires_at FROM tenant_invitations WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                                               {"tenant": str(tenant_id), "id": str(invitation_id)}).mappings().one_or_none()
                if invitation is None:
                    raise HTTPException(status_code=404, detail={"code": "invitation_not_found"})
                if invitation["state"] != "pending" or invitation["expires_at"] <= datetime.now(timezone.utc):
                    raise HTTPException(status_code=409, detail={"code": "invitation_unavailable"})
                if invitation["email"] != identity.email.casefold():
                    raise HTTPException(status_code=404, detail={"code": "invitation_not_found"})
                locations = connection.execute(text("SELECT location_id FROM invitation_locations WHERE tenant_id=:tenant AND invitation_id=:id"),
                                               {"tenant": str(tenant_id), "id": str(invitation_id)}).scalars().all()
                connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,:role)"),
                                   {"tenant": str(tenant_id), "user": str(user["id"]), "role": invitation["role"]})
                for location_id in locations:
                    connection.execute(text("INSERT INTO membership_locations (tenant_id,user_id,location_id) VALUES (:tenant,:user,:location)"),
                                       {"tenant": str(tenant_id), "user": str(user["id"]), "location": str(location_id)})
                connection.execute(text("UPDATE tenant_invitations SET state='accepted',accepted_by_user_id=:user,accepted_at=now() WHERE tenant_id=:tenant AND id=:id"),
                                   {"tenant": str(tenant_id), "user": str(user["id"]), "id": str(invitation_id)})
                _insert_audit(connection, tenant_id, identity, action="membership.invitation_accepted", resource_type="invitation",
                              resource_id=invitation_id, reason="invitee accepted verified invitation", request_id=request.state.request_id,
                              changed_fields=["state", "membership.role", "membership.locations"])
            return JSONResponse(status_code=201, content={"tenant_id": str(tenant_id), "role": invitation["role"],
                                  "state": "accepted"}, headers={"X-Request-ID": str(request.state.request_id)})
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/memberships/{user_id}", tags=["identity"])
    def update_membership(tenant_id: uuid.UUID, user_id: uuid.UUID, payload: MembershipPatch, request: Request,
                          identity: AuthenticatedIdentity = Depends(require_identity)) -> dict:
        if payload.role is None and payload.location_ids is None:
            raise HTTPException(status_code=422, detail={"code": "membership_change_required"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                _require_owner(connection, tenant_id, identity)
                current = connection.execute(text("SELECT role FROM memberships WHERE tenant_id=:tenant AND user_id=:user FOR UPDATE"),
                                             {"tenant": str(tenant_id), "user": str(user_id)}).scalar_one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "membership_not_found"})
                if current == "owner":
                    raise HTTPException(status_code=409, detail={"code": "owner_change_requires_transfer"})
                if payload.location_ids is not None:
                    _validate_location_assignments(connection, tenant_id, payload.location_ids)
                new_role = payload.role or current
                if payload.role:
                    connection.execute(text("UPDATE memberships SET role=:role WHERE tenant_id=:tenant AND user_id=:user"),
                                       {"role": new_role, "tenant": str(tenant_id), "user": str(user_id)})
                if payload.location_ids is not None:
                    connection.execute(text("DELETE FROM membership_locations WHERE tenant_id=:tenant AND user_id=:user"),
                                       {"tenant": str(tenant_id), "user": str(user_id)})
                    for location_id in payload.location_ids:
                        connection.execute(text("INSERT INTO membership_locations (tenant_id,user_id,location_id) VALUES (:tenant,:user,:location)"),
                                           {"tenant": str(tenant_id), "user": str(user_id), "location": str(location_id)})
                fields = (["role"] if payload.role else []) + (["location_assignments"] if payload.location_ids is not None else [])
                _insert_audit(connection, tenant_id, identity, action="membership.updated", resource_type="membership",
                              resource_id=user_id, reason=payload.reason, request_id=request.state.request_id, changed_fields=fields)
                # Report the assignments as stored, not as requested: a role-only
                # patch leaves location rows untouched and must say so.
                assigned = connection.execute(
                    text("SELECT location_id FROM membership_locations WHERE tenant_id=:tenant AND user_id=:user ORDER BY location_id"),
                    {"tenant": str(tenant_id), "user": str(user_id)},
                ).scalars().all()
                return {"user_id": str(user_id), "role": new_role, "location_ids": [str(value) for value in assigned]}
        finally:
            engine.dispose()

    @app.delete("/v1/tenants/{tenant_id}/memberships/{user_id}", tags=["identity"], status_code=204)
    def deactivate_membership(tenant_id: uuid.UUID, user_id: uuid.UUID, request: Request,
                              reason: str | None = Query(default=None, min_length=3, max_length=240),
                              payload: DeactivateRequest | None = None,
                              identity: AuthenticatedIdentity = Depends(require_identity)) -> Response:
        # The body is preferred so operator-supplied audit text never lands in a
        # web-tier access log; the query form stays for direct API callers.
        stated = (payload.reason if payload else reason) or ""
        audited_reason = stated.strip()
        if not audited_reason:
            raise HTTPException(status_code=422, detail={"code": "reason_required"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                _require_owner(connection, tenant_id, identity)
                # A pending invitation addressed to this person is withdrawn only when
                # their verified email is unambiguous, so one account cannot cancel
                # another account's pending access.
                departing_email = connection.execute(
                    text("""SELECT email FROM identity_users u WHERE id=:user AND email_verified
                            AND (SELECT count(*) FROM identity_users o WHERE o.email=u.email AND o.email_verified)=1"""),
                    {"user": str(user_id)},
                ).scalar_one_or_none()
                # Withdraw transfers this person initiated as well as ones aimed at them. An
                # initiated transfer could never be accepted after this (its initiator
                # would no longer be an owner), so leaving it pending only strands it
                # and lets it block the tenant's next transfer.
                # `ownership_transfers` is locked before `memberships`, the same order the
                # transfer-accept path takes, so the two paths cannot deadlock.
                transfers = connection.execute(
                    text("""SELECT id FROM ownership_transfers
                            WHERE tenant_id=:tenant AND state='pending'
                              AND (from_user_id=:user OR target_email = ANY(CAST(:emails AS text[])))
                            ORDER BY id FOR UPDATE"""),
                    {"tenant": str(tenant_id), "user": str(user_id),
                     "emails": [departing_email] if departing_email else []},
                ).scalars().all()
                invitations: list = []
                if departing_email:
                    invitations = connection.execute(
                        text("""SELECT id FROM tenant_invitations
                                WHERE tenant_id=:tenant AND state='pending' AND email=:email
                                ORDER BY id FOR UPDATE"""),
                        {"tenant": str(tenant_id), "email": departing_email},
                    ).scalars().all()
                locked = _lock_owners_and_target(connection, tenant_id, user_id)
                current = locked.get(str(user_id))
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "membership_not_found"})
                if current == "owner":
                    owners = [uid for uid, role in locked.items() if role == "owner"]
                    if len(owners) <= 1:
                        raise HTTPException(status_code=409, detail={"code": "last_owner_required"})
                    raise HTTPException(status_code=409, detail={"code": "owner_transfer_required"})
                connection.execute(text("DELETE FROM memberships WHERE tenant_id=:tenant AND user_id=:user"),
                                   {"tenant": str(tenant_id), "user": str(user_id)})
                withdrawn: list[str] = []
                # Deactivation must be authoritative. Otherwise a person removed for
                # cause could still complete an ownership transfer raised while they
                # were a member and become owner.
                for transfer_id in transfers:
                    connection.execute(
                        text("UPDATE ownership_transfers SET state='revoked' WHERE tenant_id=:tenant AND id=:id"),
                        {"tenant": str(tenant_id), "id": str(transfer_id)},
                    )
                    _insert_audit(connection, tenant_id, identity, action="ownership.transfer_withdrawn",
                                  resource_type="ownership_transfer", resource_id=transfer_id,
                                  reason=f"membership deactivated: {audited_reason}",
                                  request_id=request.state.request_id, changed_fields=["state"])
                    withdrawn.append(str(transfer_id))
                for invitation_id in invitations:
                    connection.execute(
                        text("UPDATE tenant_invitations SET state='revoked',revoked_at=now() WHERE tenant_id=:tenant AND id=:id"),
                        {"tenant": str(tenant_id), "id": str(invitation_id)},
                    )
                    _insert_audit(connection, tenant_id, identity, action="membership.invitation_revoked",
                                  resource_type="invitation", resource_id=invitation_id,
                                  reason=f"membership deactivated: {audited_reason}",
                                  request_id=request.state.request_id, changed_fields=["state"])
                    withdrawn.append(str(invitation_id))
                _insert_audit(connection, tenant_id, identity, action="membership.deactivated", resource_type="membership",
                              resource_id=user_id, reason=audited_reason, request_id=request.state.request_id,
                              changed_fields=["state"] + (["withdrawn_pending_access"] if withdrawn else []))
            return Response(status_code=204)
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/ownership-transfers", tags=["identity"], status_code=201)
    def request_ownership_transfer(tenant_id: uuid.UUID, payload: OwnershipTransferRequest, request: Request,
                                   identity: AuthenticatedIdentity = Depends(require_identity)) -> dict:
        if not identity.email_verified or not identity.email:
            raise HTTPException(status_code=403, detail={"code": "verified_email_required"})
        if not _fresh_reauthentication(identity):
            raise HTTPException(status_code=403, detail={"code": "reauthentication_required",
                                                       "reauth_url": f"{os.getenv('API_BASE_URL','').rstrip('/')}/v1/auth/staff/start?reauth=true"})
        if payload.target_email == identity.email.casefold():
            raise HTTPException(status_code=422, detail={"code": "different_owner_required"})
        engine = app.state.engine_factory()
        transfer_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                from_user_id = _require_owner(connection, tenant_id, identity)
                _expire_stale_transfers(connection, tenant_id, identity, request.state.request_id)
                pending_transfer = connection.execute(
                    text("""SELECT id FROM ownership_transfers
                            WHERE tenant_id=:tenant AND state='pending' LIMIT 1"""),
                    {"tenant": str(tenant_id)},
                ).scalar_one_or_none()
                if pending_transfer is not None:
                    raise HTTPException(status_code=409, detail={"code": "ownership_transfer_already_pending"})
                try:
                    connection.execute(text("""INSERT INTO ownership_transfers (tenant_id,id,from_user_id,target_email,state,expires_at)
                                               VALUES (:tenant,:id,:user,:email,'pending',now()+interval '7 days')"""),
                                       {"tenant": str(tenant_id), "id": str(transfer_id),
                                        "user": str(from_user_id), "email": payload.target_email})
                except IntegrityError as exc:
                    # Concurrent request lost the one-pending-transfer race.
                    raise HTTPException(status_code=409, detail={"code": "ownership_transfer_already_pending"}) from exc
                _insert_audit(connection, tenant_id, identity, action="ownership.transfer_requested", resource_type="ownership_transfer",
                              resource_id=transfer_id, reason="owner requested verified ownership transfer",
                              request_id=request.state.request_id, changed_fields=["pending_owner_email"])
            return {"transfer_id": str(transfer_id), "target_email": payload.target_email, "state": "pending",
                    "delivery_status": "recorded_only"}
        finally:
            engine.dispose()

    @app.post("/v1/tenants/{tenant_id}/ownership-transfers/{transfer_id}/accept", tags=["identity"])
    def accept_ownership_transfer(tenant_id: uuid.UUID, transfer_id: uuid.UUID, request: Request,
                                  identity: AuthenticatedIdentity = Depends(require_identity)) -> dict:
        if not identity.email_verified or not identity.email:
            raise HTTPException(status_code=403, detail={"code": "verified_email_required"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                set_tenant_context(connection, tenant_id)
                transfer = connection.execute(text("SELECT from_user_id,target_email,state,expires_at FROM ownership_transfers WHERE tenant_id=:tenant AND id=:id FOR UPDATE"),
                                              {"tenant": str(tenant_id), "id": str(transfer_id)}).mappings().one_or_none()
                if transfer is None:
                    raise HTTPException(status_code=404, detail={"code": "transfer_not_found"})
                if transfer["target_email"] != identity.email.casefold():
                    raise HTTPException(status_code=404, detail={"code": "transfer_not_found"})
                if transfer["state"] != "pending" or transfer["expires_at"] <= datetime.now(timezone.utc):
                    raise HTTPException(status_code=409, detail={"code": "transfer_unavailable"})
                target = connection.execute(text("SELECT id FROM identity_users WHERE issuer=:issuer AND subject=:subject AND email=:email AND email_verified"),
                                            {"issuer": identity.issuer, "subject": identity.subject, "email": identity.email.casefold()}).scalar_one_or_none()
                if target is None or str(target) == str(transfer["from_user_id"]):
                    raise HTTPException(status_code=403, detail={"code": "verified_transfer_target_required"})
                # Same lock set and order as owner deactivation, so the two paths
                # serialise instead of racing the tenant down to zero owners.
                locked = _lock_owners_and_target(connection, tenant_id, target)
                if locked.get(str(transfer["from_user_id"])) != "owner":
                    raise HTTPException(status_code=409, detail={"code": "transfer_initiator_no_longer_owner"})
                if locked.get(str(target)) is None:
                    connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'owner')"),
                                       {"tenant": str(tenant_id), "user": str(target)})
                else:
                    connection.execute(text("UPDATE memberships SET role='owner' WHERE tenant_id=:tenant AND user_id=:user"),
                                       {"tenant": str(tenant_id), "user": str(target)})
                connection.execute(text("UPDATE memberships SET role='manager' WHERE tenant_id=:tenant AND user_id=:user"),
                                   {"tenant": str(tenant_id), "user": str(transfer["from_user_id"])})
                connection.execute(text("DELETE FROM membership_locations WHERE tenant_id=:tenant AND user_id=:user"),
                                   {"tenant": str(tenant_id), "user": str(transfer["from_user_id"])})
                connection.execute(text("UPDATE ownership_transfers SET state='accepted',accepted_by_user_id=:user,accepted_at=now() WHERE tenant_id=:tenant AND id=:id"),
                                   {"tenant": str(tenant_id), "user": str(target), "id": str(transfer_id)})
                _insert_audit(connection, tenant_id, identity, action="ownership.transferred", resource_type="tenant",
                              resource_id=tenant_id, reason="verified owner accepted ownership transfer",
                              request_id=request.state.request_id, changed_fields=["owner_membership"])
            return {"tenant_id": str(tenant_id), "state": "accepted", "role": "owner"}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/setup", tags=["identity"])
    def tenant_setup(tenant_id: uuid.UUID, identity: AuthenticatedIdentity = Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = _membership_scope(connection, tenant_id, identity)
                _require_permission(role, "tenant.summary.read")
                state = _setup_state(connection, tenant_id)
                return {"steps": [{"step": step, "state": state[step],
                                   "stored": step in STORED_SETUP_STEPS} for step in SETUP_STEPS],
                        **_booking_readiness(state)}
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/setup/{step}", tags=["identity"])
    def update_setup_step(tenant_id: uuid.UUID, step: str, payload: SetupStepPatch, request: Request,
                         identity: AuthenticatedIdentity = Depends(require_identity)) -> dict:
        if step not in SETUP_STEPS:
            raise HTTPException(status_code=404, detail={"code": "setup_step_unknown"})
        if step not in STORED_SETUP_STEPS:
            # Derived steps are a fact about existing records, not something a client asserts.
            raise HTTPException(status_code=409, detail={"code": "setup_step_derived"})
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                _require_owner(connection, tenant_id, identity)
                connection.execute(
                    text("""INSERT INTO tenant_setup_steps (tenant_id,step,state,updated_at)
                            VALUES (:tenant,:step,:state,now())
                            ON CONFLICT (tenant_id,step) DO UPDATE
                              SET state=EXCLUDED.state, updated_at=now()"""),
                    {"tenant": str(tenant_id), "step": step, "state": payload.state})
                _insert_audit(connection, tenant_id, identity, action="setup.step_updated",
                              resource_type="tenant", resource_id=tenant_id,
                              reason=f"setup step {step} set to {payload.state}",
                              request_id=request.state.request_id, changed_fields=[f"setup.{step}"])
                state = _setup_state(connection, tenant_id)
                return {"step": step, "state": state[step], **_booking_readiness(state)}
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/settings", tags=["identity"])
    def tenant_settings(tenant_id: uuid.UUID, identity: AuthenticatedIdentity = Depends(require_identity)) -> dict:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, _, _ = _membership_scope(connection, tenant_id, identity)
                _require_permission(role, "tenant.summary.read")
                row = connection.execute(text("SELECT settings, settings_version FROM tenants WHERE id=:tenant"),
                                         {"tenant": str(tenant_id)}).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={"code": "tenant_not_found"})
                return {"settings": row["settings"] or {}, "settings_version": row["settings_version"]}
        finally:
            engine.dispose()

    @app.patch("/v1/tenants/{tenant_id}/settings", tags=["identity"])
    def update_tenant_settings(tenant_id: uuid.UUID, payload: SettingsPatch, request: Request,
                               identity: AuthenticatedIdentity = Depends(require_identity)) -> dict:
        changes = payload.model_dump(exclude_unset=True)
        if not changes:
            raise HTTPException(status_code=422, detail={"code": "settings_change_required"})
        # A field sent as an explicit null clears it. Omitted fields are left untouched.
        cleared = [key for key, value in changes.items() if value is None]
        changes = {key: value for key, value in changes.items() if value is not None}
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                _require_owner(connection, tenant_id, identity)
                current = connection.execute(text("SELECT settings FROM tenants WHERE id=:tenant FOR UPDATE"),
                                             {"tenant": str(tenant_id)}).scalar_one_or_none()
                if current is None:
                    raise HTTPException(status_code=404, detail={"code": "tenant_not_found"})
                merged = {**(current or {}), **changes}
                for key in cleared:
                    merged.pop(key, None)
                row = connection.execute(
                    text("""UPDATE tenants SET settings=CAST(:settings AS jsonb), settings_version=settings_version+1
                            WHERE id=:tenant RETURNING settings_version"""),
                    {"tenant": str(tenant_id), "settings": json.dumps(merged)}).mappings().one()
                _insert_audit(connection, tenant_id, identity, action="tenant.settings_updated",
                              resource_type="tenant", resource_id=tenant_id,
                              reason="owner updated salon settings and branding",
                              request_id=request.state.request_id, changed_fields=sorted(set(changes) | set(cleared)))
                return {"settings": merged, "settings_version": row["settings_version"]}
        finally:
            engine.dispose()

    @app.get("/v1/public/salons/{slug}", tags=["public"])
    def public_salon(slug: str) -> dict:
        """FR-008 branding and the setup reason a public page is not yet taking bookings.

        Returns brand fields and checklist step names only. No tenant ids, no other
        tenant's data, no internal reason text.
        """
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                resolved = connection.execute(
                    text("SELECT tenant_id::text AS tenant_id FROM public_booking_slug_lookup(:slug)"),
                    {"slug": slug}).mappings().one_or_none()
                if resolved is None:
                    raise HTTPException(status_code=404, detail={"code": "salon_not_found"})
                tenant_id = uuid.UUID(resolved["tenant_id"])
                set_tenant_context(connection, tenant_id)
                location = connection.execute(
                    text("""SELECT l.display_name, l.timezone, t.display_name AS business_name,
                                   t.settings, t.settings_version
                            FROM locations l JOIN tenants t ON t.id=l.tenant_id
                            WHERE l.tenant_id=:tenant AND l.booking_slug=:slug"""),
                    {"tenant": str(tenant_id), "slug": slug}).mappings().one_or_none()
                if location is None:
                    raise HTTPException(status_code=404, detail={"code": "salon_not_found"})
                state = _setup_state(connection, tenant_id)
                return {
                    "business_name": location["business_name"],
                    "location_name": location["display_name"],
                    "timezone": location["timezone"],
                    "branding": _public_brand(location["settings"] or {}),
                    "branding_version": location["settings_version"],
                    **_booking_readiness(state),
                }
        finally:
            engine.dispose()

    @app.get("/v1/tenants/{tenant_id}/summary", tags=["identity"])
    def tenant_summary(tenant_id: uuid.UUID, identity: AuthenticatedIdentity = Depends(require_identity)) -> dict[str, str | int]:
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                role, user_id, assigned = _membership_scope(connection, tenant_id, identity)
                _require_permission(role, "tenant.summary.read")
                row = connection.execute(text("""SELECT t.display_name, t.default_currency
                                                FROM tenants t WHERE t.id=:tenant"""),
                                         {"tenant": str(tenant_id)}).mappings().one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={"code": "tenant_not_found"})
                if role == "owner":
                    count = connection.execute(text("SELECT count(*) FROM locations WHERE tenant_id=:tenant"),
                                               {"tenant": str(tenant_id)}).scalar_one()
                elif assigned:
                    count = connection.execute(text("SELECT count(*) FROM locations WHERE tenant_id=:tenant AND id=ANY(:locations)"),
                                               {"tenant": str(tenant_id), "locations": assigned}).scalar_one()
                else:
                    count = 0
                return {"display_name": row["display_name"], "currency": row["default_currency"].strip(), "location_count": count}
        finally:
            engine.dispose()

    @app.get("/health", tags=["operations"], response_model=dict[str, str])
    def health() -> dict[str, str] | JSONResponse:
        if not database_probe():
            return JSONResponse(status_code=503, content={"status": "degraded", "database": "unavailable"})
        return {"status": "ok", "database": "connected"}

    @app.get("/v1/auth/{surface}/start", tags=["identity"], include_in_schema=False)
    def auth_start(surface: str, reauth: bool = False) -> RedirectResponse:
        if surface not in {"staff", "company"}:
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        config = _auth_config(surface)
        api_base = os.getenv("API_BASE_URL", "").rstrip("/")
        if not api_base:
            raise HTTPException(status_code=503, detail={"code": "identity_provider_unconfigured"})
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        redirect_uri = f"{api_base}/v1/auth/{surface}/callback"
        params = {"response_type": "code", "client_id": config["client_id"], "redirect_uri": redirect_uri,
                  "scope": "openid email", "state": state, "nonce": nonce, "code_challenge": challenge,
                  "code_challenge_method": "S256"}
        if reauth:
            params.update({"prompt": "login", "max_age": "0"})
        response = RedirectResponse(config["authorization_endpoint"] + "?" + urlencode(params), status_code=302)
        response.set_cookie(_flow_cookie_name(surface), _pack_flow({"surface": surface, "state": state, "nonce": nonce,
                            "verifier": verifier, "expires_at": int(time.time()) + 300}), max_age=300, httponly=True,
                            secure=os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true", samesite="lax",
                            path=f"/v1/auth/{surface}", domain=os.getenv("SESSION_COOKIE_DOMAIN") or None)
        return response

    @app.get("/v1/auth/{surface}/callback", tags=["identity"], include_in_schema=False)
    def auth_callback(surface: str, request: Request, code: str, state: str) -> RedirectResponse:
        if surface not in {"staff", "company"}:
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        flow_cookie = request.cookies.get(_flow_cookie_name(surface), "")
        flow = _unpack_flow(flow_cookie, surface)
        if not hmac.compare_digest(str(flow["state"]), state):
            raise HTTPException(status_code=400, detail={"code": "invalid_login_state"})
        config = _auth_config(surface)
        redirect_uri = os.getenv("API_BASE_URL", "").rstrip("/") + f"/v1/auth/{surface}/callback"
        try:
            token_response = httpx.post(config["token_endpoint"], data={
                "grant_type": "authorization_code", "client_id": config["client_id"],
                "code": code, "redirect_uri": redirect_uri, "code_verifier": str(flow["verifier"]),
            }, headers={"Accept": "application/json"}, timeout=8.0)
            token_response.raise_for_status()
            tokens = token_response.json()
            claims = _validate_id_token(tokens["id_token"], config, str(flow["nonce"]), surface)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=401, detail={"code": "identity_login_failed"}) from exc
        if not claims.get("sub"):
            raise HTTPException(status_code=401, detail={"code": "identity_token_rejected"})
        raw_email = claims.get("email")
        verified_email = raw_email.strip().casefold() if isinstance(raw_email, str) and claims.get("email_verified") is True else None
        auth_time = claims.get("auth_time")
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                connection.execute(text("""INSERT INTO identity_users (issuer,subject,email,email_verified,auth_time)
                                           VALUES (:issuer,:subject,:email,:verified,to_timestamp(:auth_time))
                                           ON CONFLICT (issuer,subject) DO UPDATE SET
                                             email=COALESCE(EXCLUDED.email,identity_users.email),
                                             email_verified=identity_users.email_verified OR EXCLUDED.email_verified,
                                             auth_time=COALESCE(EXCLUDED.auth_time,identity_users.auth_time)"""),
                                   {"issuer": claims["iss"], "subject": claims["sub"], "email": verified_email,
                                    "verified": verified_email is not None, "auth_time": auth_time})
        finally:
            engine.dispose()
        session_token = secrets.token_urlsafe(32)
        session_hash = hashlib.sha256(session_token.encode()).hexdigest()
        engine = app.state.engine_factory()
        try:
            with engine.begin() as connection:
                connection.execute(text("""INSERT INTO auth_sessions (session_hash, surface, issuer, subject, expires_at)
                                           VALUES (:hash, :surface, :issuer, :subject, to_timestamp(:expires))"""),
                                   {"hash": session_hash, "surface": surface, "issuer": claims["iss"],
                                    "subject": claims["sub"], "expires": claims["exp"]})
        finally:
            engine.dispose()
        web_base = os.getenv("WEB_BASE_URL", "").rstrip("/")
        if not web_base:
            raise HTTPException(status_code=503, detail={"code": "web_origin_unconfigured"})
        target = RedirectResponse(f"{web_base}/login?surface={surface}&status=signed-in", status_code=303)
        secure = os.getenv("SESSION_COOKIE_SECURE", "true").lower() == "true"
        domain = os.getenv("SESSION_COOKIE_DOMAIN") or None
        target.set_cookie(_cookie_name(surface), session_token, max_age=max(0, int(claims["exp"]) - int(time.time())),
                          httponly=True, secure=secure, samesite="lax", path="/", domain=domain)
        target.delete_cookie(_flow_cookie_name(surface), path=f"/v1/auth/{surface}", domain=domain)
        return target

    @app.post("/v1/auth/{surface}/logout", tags=["identity"], include_in_schema=False)
    def auth_logout(surface: str, request: Request) -> Response:
        if surface not in {"staff", "company"}:
            raise HTTPException(status_code=404, detail={"code": "not_found"})
        token = request.cookies.get(_cookie_name(surface))
        if token:
            engine = app.state.engine_factory()
            try:
                with engine.begin() as connection:
                    connection.execute(text("DELETE FROM auth_sessions WHERE session_hash=:hash AND surface=:surface"),
                                       {"hash": hashlib.sha256(token.encode()).hexdigest(), "surface": surface})
            finally:
                engine.dispose()
        response = Response(status_code=204)
        response.delete_cookie(_cookie_name(surface), path="/", domain=os.getenv("SESSION_COOKIE_DOMAIN") or None)
        return response

    @app.post("/v1/onboarding", tags=["identity"], status_code=201)
    def onboard(payload: OnboardingRequest, request: Request, identity: AuthenticatedIdentity = Depends(require_identity)) -> JSONResponse:
        engine = app.state.engine_factory()
        try:
            result, created = _create_tenant(engine, identity, payload, request.state.request_id)
            return JSONResponse(status_code=201 if created else 200, content=result, headers={"X-Request-ID": str(request.state.request_id)})
        finally:
            engine.dispose()

    register_client_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
        project_fields=project_client_fields,
    )

    register_import_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
        normalize_email=normalize_email,
        normalize_phone=normalize_phone,
    )

    register_merge_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_privacy_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_catalogue_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_staff_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_hours_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_resource_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_availability_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
    )

    register_hold_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
        resolve_public_location=resolve_public_location,
    )

    register_calendar_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_transition_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_receipt_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_sender_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_callback_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_inbox_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_retention_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_analytics_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
    )

    register_company_routes(
        app,
        require_identity=require_identity,
        require_company_identity=require_company_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_budget_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_segment_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_support_routes(
        app,
        require_identity=require_identity,
        require_company_identity=require_company_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_trace_routes(
        app,
        require_company_identity=require_company_identity,
    )

    register_wa_sender_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    register_closure_impacts_routes(
        app,
        require_identity=require_identity,
        membership_scope=_membership_scope,
        require_permission=_require_permission,
        insert_audit=_insert_audit,
    )

    return app


app = create_app()
