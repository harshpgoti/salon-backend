from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.identity.main import SETUP_STEPS, AuthenticatedIdentity, create_app, require_identity


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL setup integration")
    return url


def _client(database_url: str, identity: AuthenticatedIdentity) -> TestClient:
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_identity] = lambda: identity
    return TestClient(app)


@pytest.fixture()
def salon(database_url):
    """A fresh tenant per test, with its owner and booking slug."""
    engine = create_engine(database_url, pool_pre_ping=True)
    issuer = "https://identity.example.invalid/setup-test"
    owner = AuthenticatedIdentity(issuer, str(uuid.uuid4()), email="owner@setup.invalid",
                                  email_verified=True, auth_time=time.time())
    signup = _client(database_url, owner).post("/v1/onboarding", json={
        "business_name": "Synthetic Setup Salon", "location_name": "Downtown", "timezone": "America/Toronto"})
    assert signup.status_code == 201, signup.text
    tenant = signup.json()

    yield {"engine": engine, "issuer": issuer, "owner": owner,
           "tenant_id": tenant["tenant_id"], "location_id": tenant["location_id"],
           "booking_slug": tenant["booking_slug"]}

    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant["tenant_id"]})
        connection.execute(text("""UPDATE identity_users SET tenant_id=NULL,onboarding_fingerprint=NULL
                                   WHERE tenant_id=:tenant"""), {"tenant": tenant["tenant_id"]})
        connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": tenant["tenant_id"]})
        connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        connection.execute(text("DELETE FROM identity_users WHERE issuer=:issuer"), {"issuer": issuer})
    engine.dispose()


def _states(client: TestClient, tenant_id: str) -> dict[str, str]:
    return {item["step"]: item["state"] for item in client.get(f"/v1/tenants/{tenant_id}/setup").json()["steps"]}


# TASK-008 / AC-1


def test_fresh_tenant_starts_unready_and_reports_every_step(salon):
    """AC-1: a new salon reports all eight steps and does not yet accept bookings."""
    owner = _client(os.environ["DATABASE_URL"], salon["owner"])

    setup = owner.get(f"/v1/tenants/{salon['tenant_id']}/setup")
    assert setup.status_code == 200, setup.text
    body = setup.json()
    assert [item["step"] for item in body["steps"]] == [
        "business_identity", "location", "hours", "service", "qualified_staff",
        "public_link", "communication_setup", "import"]
    # TASK-002 already created a location, so only that derived step is complete.
    states = {item["step"]: item["state"] for item in body["steps"]}
    assert states["location"] == "complete"
    assert states["qualified_staff"] == "pending"
    assert states["hours"] == "pending"
    assert body["accepting_bookings"] is False
    assert set(body["missing_setup"]) == {"hours", "service", "public_link"}


def test_checklist_progress_persists_and_resumes_in_a_new_session(salon):
    """AC-1: progress survives the request, the process and the session that recorded it."""
    database_url = os.environ["DATABASE_URL"]
    tenant_id, location_id = salon["tenant_id"], salon["location_id"]

    first = _client(database_url, salon["owner"])
    for step in ("business_identity", "hours", "service"):
        updated = first.patch(f"/v1/tenants/{tenant_id}/setup/{step}", json={"state": "complete"})
        assert updated.status_code == 200, updated.text

    # A brand new app, engine and client: nothing is carried in memory.
    resumed = _client(database_url, salon["owner"])
    states = _states(resumed, tenant_id)
    assert {states[s] for s in ("business_identity", "hours", "service")} == {"complete"}
    assert states["public_link"] == "pending"

    # Publishing the booking link is the last blocker; staff is a warning, not a blocker.
    published = resumed.patch(f"/v1/tenants/{tenant_id}/setup/public_link", json={"state": "complete"})
    assert published.status_code == 200
    assert published.json()["accepting_bookings"] is True
    assert published.json()["missing_setup"] == []

    with salon["engine"].begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        members = connection.execute(
            text("SELECT user_id::text AS user_id FROM memberships WHERE tenant_id=:tenant AND role<>'owner'"),
            {"tenant": tenant_id}).scalars().all()
        if not members:
            user_id = connection.execute(
                text("""INSERT INTO identity_users (issuer,subject,email,email_verified,auth_time)
                        VALUES (:issuer,:subject,'staff@setup.invalid',true,to_timestamp(:auth_time)) RETURNING id"""),
                {"issuer": salon["issuer"], "subject": str(uuid.uuid4()), "auth_time": time.time()}).scalar_one()
            connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'stylist')"),
                               {"tenant": tenant_id, "user": str(user_id)})
            connection.execute(text("""INSERT INTO membership_locations (tenant_id,user_id,location_id)
                                       VALUES (:tenant,:user,:location)"""),
                               {"tenant": tenant_id, "user": str(user_id), "location": location_id})
    # qualified_staff is derived, so it flips without anyone writing to the checklist.
    after_staff = _states(resumed, tenant_id)
    assert after_staff["qualified_staff"] == "complete"
    assert resumed.get(f"/v1/tenants/{tenant_id}/setup").json()["accepting_bookings"] is True


def test_derived_and_unknown_steps_cannot_be_written_by_hand(salon):
    """A step that is a fact about records is not something a client may assert."""
    owner = _client(os.environ["DATABASE_URL"], salon["owner"])
    tenant_id = salon["tenant_id"]

    assert owner.patch(f"/v1/tenants/{tenant_id}/setup/location", json={"state": "complete"}).status_code == 409
    assert owner.patch(f"/v1/tenants/{tenant_id}/setup/qualified_staff", json={"state": "complete"}).status_code == 409
    assert owner.patch(f"/v1/tenants/{tenant_id}/setup/not_a_step", json={"state": "complete"}).status_code == 404
    assert owner.patch(f"/v1/tenants/{tenant_id}/setup/hours", json={"state": "skipped"}).status_code == 422

    states = _states(owner, tenant_id)
    assert states["location"] == "complete" and states["qualified_staff"] == "pending"


def test_setup_changes_are_audited_with_a_request_id(salon):
    """FR-006: every checklist transition is an audited state change."""
    database_url = os.environ["DATABASE_URL"]
    tenant_id = salon["tenant_id"]
    request_id = str(uuid.uuid4())
    owner = _client(database_url, salon["owner"])

    response = owner.patch(f"/v1/tenants/{tenant_id}/setup/hours", json={"state": "complete"},
                           headers={"X-Request-ID": request_id})
    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == request_id

    audit = owner.get(f"/v1/tenants/{tenant_id}/audit").json()["items"]
    event = next(e for e in audit if e["action"] == "setup.step_updated")
    assert event["changed_fields"] == ["setup.hours"]
    assert event["request_id"] == request_id
    assert event["actor_subject"] == salon["owner"].subject


# TASK-008 / AC-2


def test_public_booking_page_explains_missing_setup_without_leaking(salon):
    """AC-2: an unready public page says what is missing, in step names only."""
    database_url = os.environ["DATABASE_URL"]
    tenant_id = salon["tenant_id"]

    public = TestClient(create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True)))
    slug = salon["booking_slug"]
    body = public.get(f"/v1/public/salons/{slug}").json()

    assert body["business_name"] == "Synthetic Setup Salon"
    assert body["location_name"] == "Downtown"
    assert body["timezone"] == "America/Toronto"
    assert body["accepting_bookings"] is False
    assert set(body["missing_setup"]) == {"hours", "service", "public_link"}
    # No tenant id, no internal reason text, no other tenant's data.
    assert tenant_id not in str(body)
    # Every missing entry is a bare step name from the known set - never free text.
    assert set(body["missing_setup"]) <= set(SETUP_STEPS)
    assert all(isinstance(step, str) and " " not in step for step in body["missing_setup"])

    owner = _client(database_url, salon["owner"])
    for step in ("hours", "service", "public_link"):
        owner.patch(f"/v1/tenants/{tenant_id}/setup/{step}", json={"state": "complete"})
    ready = public.get(f"/v1/public/salons/{slug}").json()
    assert ready["accepting_bookings"] is True and ready["missing_setup"] == []

    # An unknown slug is a plain 404 that enumerates nothing.
    missing = public.get("/v1/public/salons/no-such-salon")
    assert missing.status_code == 404
    assert missing.json()["detail"]["code"] == "salon_not_found"


def test_branding_is_versioned_and_scoped_to_its_tenant(salon):
    """AC-2: settings round-trip, the version advances, and branding is tenant-only."""
    database_url = os.environ["DATABASE_URL"]
    tenant_id = salon["tenant_id"]
    owner = _client(database_url, salon["owner"])

    first = owner.get(f"/v1/tenants/{tenant_id}/settings")
    assert first.status_code == 200, first.text
    assert first.json() == {"settings": {}, "settings_version": 1}

    updated = owner.patch(f"/v1/tenants/{tenant_id}/settings", json={
        "brand_name": "Maple & Co", "primary_colour": "#334B39", "accent_colour": "#8c2626",
        "cancellation_policy": "Free cancellation up to 24 hours before the appointment.",
        "notification_language": "en-CA", "marketing_messages": False})
    assert updated.status_code == 200, updated.text
    assert updated.json()["settings_version"] == 2
    assert updated.json()["settings"]["primary_colour"] == "#334b39"

    # A second write merges rather than replaces, and advances the version again.
    merged = owner.patch(f"/v1/tenants/{tenant_id}/settings", json={"brand_name": "Maple & Co Salon"})
    assert merged.status_code == 200
    assert merged.json()["settings_version"] == 3
    assert merged.json()["settings"]["primary_colour"] == "#334b39"

    public = TestClient(create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True)))
    body = public.get(f"/v1/public/salons/{salon['booking_slug']}").json()
    assert body["branding"] == {"brand_name": "Maple & Co Salon", "primary_colour": "#334b39",
                                "accent_colour": "#8c2626"}
    assert body["branding_version"] == 3
    # Cancellation terms and notification preferences are private configuration, not brand.
    assert "cancellation_policy" not in str(body)
    assert "notification_language" not in str(body)
    assert "marketing_messages" not in str(body)


def test_invalid_settings_are_refused_and_do_not_advance_the_version(salon):
    owner = _client(os.environ["DATABASE_URL"], salon["owner"])
    tenant_id = salon["tenant_id"]

    for bad in ({"primary_colour": "green"}, {"primary_colour": "#12345"},
                {"notification_language": "de-DE"}, {"brand_name": "   "},
                {"cancellation_policy": ""}):
        response = owner.patch(f"/v1/tenants/{tenant_id}/settings", json=bad)
        assert response.status_code == 422, (bad, response.text)
    assert owner.patch(f"/v1/tenants/{tenant_id}/settings", json={}).status_code == 422
    assert owner.get(f"/v1/tenants/{tenant_id}/settings").json() == {"settings": {}, "settings_version": 1}

    # A rejected batch writes nothing at all - not even its valid half - so a merge that
    # ran before validation would be caught here.
    mixed = owner.patch(f"/v1/tenants/{tenant_id}/settings",
                        json={"brand_name": "Maple & Co", "primary_colour": "chartreuse"})
    assert mixed.status_code == 422
    assert owner.get(f"/v1/tenants/{tenant_id}/settings").json() == {"settings": {}, "settings_version": 1}

    audit = owner.get(f"/v1/tenants/{tenant_id}/audit").json()["items"]
    assert not [e for e in audit if e["action"] == "tenant.settings_updated"]


def test_settings_can_be_cleared_and_padding_is_tolerated(salon):
    """FR-008 configurable means a value can be removed again, not only added."""
    owner = _client(os.environ["DATABASE_URL"], salon["owner"])
    tenant_id = salon["tenant_id"]

    padded = owner.patch(f"/v1/tenants/{tenant_id}/settings",
                        json={"brand_name": "  Maple & Co  ", "primary_colour": "  #ABCDEF  "})
    assert padded.status_code == 200, padded.text
    assert padded.json()["settings"] == {"brand_name": "Maple & Co", "primary_colour": "#abcdef"}

    # An explicit null clears one key and leaves the rest alone.
    cleared = owner.patch(f"/v1/tenants/{tenant_id}/settings", json={"primary_colour": None})
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["settings"] == {"brand_name": "Maple & Co"}
    assert cleared.json()["settings_version"] == 3

    # Omitting a key changes nothing; clearing everything empties the object.
    assert owner.patch(f"/v1/tenants/{tenant_id}/settings", json={"brand_name": None}).json()["settings"] == {}
    owner.patch(f"/v1/tenants/{tenant_id}/settings", json={"notification_language": "fr-CA"})
    assert owner.get(f"/v1/tenants/{tenant_id}/settings").json()["settings"] == {"notification_language": "fr-CA"}
    audit = owner.get(f"/v1/tenants/{tenant_id}/audit").json()["items"]
    assert [e for e in audit if e["action"] == "tenant.settings_updated"]


def test_settings_shape_is_constrained_in_the_database_too(salon, database_url):
    """The public page republishes brand values, so the column enforces them, not just the API."""
    engine = salon["engine"]
    tenant_id = salon["tenant_id"]
    owner = _client(database_url, salon["owner"])
    owner.patch(f"/v1/tenants/{tenant_id}/settings", json={"primary_colour": "#334b39"})

    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            connection.execute(text("UPDATE tenants SET settings=CAST(:settings AS jsonb) WHERE id=:tenant"),
                               {"tenant": tenant_id,
                                "settings": '{"primary_colour":"chartreuse","owner_secret":"x"}'})
    # The rejected write rolled back; the tenant is unchanged.
    assert owner.get(f"/v1/tenants/{tenant_id}/settings").json()["settings"] == {"primary_colour": "#334b39"}


def test_readiness_declares_which_steps_are_self_attested(salon):
    """A1: until TASK-015/017 give hours and services tables, readiness is a declaration."""
    database_url = os.environ["DATABASE_URL"]
    tenant_id = salon["tenant_id"]
    owner = _client(database_url, salon["owner"])

    # `location` is derived, so it never appears as unverified.
    fresh = owner.get(f"/v1/tenants/{tenant_id}/setup").json()
    assert fresh["accepting_bookings"] is False
    assert fresh["unverified_setup"] == []

    for step in ("hours", "service", "public_link"):
        owner.patch(f"/v1/tenants/{tenant_id}/setup/{step}", json={"state": "complete"})
    ready = owner.get(f"/v1/tenants/{tenant_id}/setup").json()
    assert ready["accepting_bookings"] is True
    # Ready, but every gate that carries it is the tenant's own word until TASK-015/017
    # give those steps tables. A consumer must not read this as "a slot exists".
    assert ready["unverified_setup"] == ["hours", "service", "public_link"]
    public = TestClient(create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True)))
    assert public.get(f"/v1/public/salons/{salon['booking_slug']}").json()["unverified_setup"] == [
        "hours", "service", "public_link"]


def test_settings_column_rejects_null_and_non_object_documents(salon, database_url):
    """N1/N4: a JSON null must not pass a CHECK via SQL NULL, nor reach the public page."""
    engine = salon["engine"]
    tenant_id = salon["tenant_id"]
    owner = _client(database_url, salon["owner"])
    owner.patch(f"/v1/tenants/{tenant_id}/settings", json={"brand_name": "Maple & Co"})

    from sqlalchemy.exc import IntegrityError
    for bad in ('{"primary_colour": null}', '{"accent_colour": null}', '{"notification_language": null}',
                "null", '"a string"', '{"brand_name": null}', '{"appointment_reminders": "yes"}',
                '{"primary_colour": "chartreuse"}', '{"brand_name": 12345}'):
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
                connection.execute(text("UPDATE tenants SET settings=CAST(:settings AS jsonb) WHERE id=:tenant"),
                                   {"tenant": tenant_id, "settings": bad})
    # Legitimate writes still pass, including false booleans and an empty document.
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        connection.execute(text("UPDATE tenants SET settings=CAST(:settings AS jsonb) WHERE id=:tenant"),
                           {"tenant": tenant_id, "settings": json.dumps(
                               {"appointment_reminders": False, "marketing_messages": False,
                                "brand_name": "A", "primary_colour": "#aabbcc"})})
    assert owner.get(f"/v1/tenants/{tenant_id}/settings").json()["settings"]["marketing_messages"] is False


def test_setup_and_branding_are_isolated_from_another_tenant(salon, database_url):
    """QA-01: a different tenant cannot read or write this tenant's setup or brand."""
    engine = salon["engine"]
    issuer = "https://identity.example.invalid/setup-isolation"
    outsider = AuthenticatedIdentity(issuer, str(uuid.uuid4()), email="outsider@setup.invalid",
                                     email_verified=True, auth_time=time.time())
    theirs = _client(database_url, outsider)
    their_signup = theirs.post("/v1/onboarding", json={
        "business_name": "Synthetic Isolation Salon", "location_name": "Main", "timezone": "America/Toronto"})
    assert their_signup.status_code == 201, their_signup.text
    foreign = their_signup.json()["tenant_id"]
    tenant_id = salon["tenant_id"]

    try:
        assert theirs.get(f"/v1/tenants/{tenant_id}/setup").status_code == 404
        assert theirs.get(f"/v1/tenants/{tenant_id}/settings").status_code == 404
        assert theirs.patch(f"/v1/tenants/{tenant_id}/setup/hours", json={"state": "complete"}).status_code == 404
        assert theirs.patch(f"/v1/tenants/{tenant_id}/settings", json={"brand_name": "Stolen"}).status_code == 404
        # And they cannot reach this tenant through their own tenant id either.
        assert theirs.patch(f"/v1/tenants/{foreign}/setup/hours", json={"state": "complete"}).status_code == 200

        # The rejected writes changed nothing here.
        owner = _client(database_url, salon["owner"])
        assert _states(owner, tenant_id)["hours"] == "pending"
        assert owner.get(f"/v1/tenants/{tenant_id}/settings").json()["settings_version"] == 1
        assert not [e for e in owner.get(f"/v1/tenants/{tenant_id}/audit").json()["items"]
                    if e["action"] == "tenant.settings_updated"]
    finally:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": foreign})
            connection.execute(text("""UPDATE identity_users SET tenant_id=NULL,onboarding_fingerprint=NULL
                                       WHERE tenant_id=:tenant"""), {"tenant": foreign})
            connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": foreign})
            connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            connection.execute(text("DELETE FROM identity_users WHERE issuer=:issuer"), {"issuer": issuer})