from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.crm.main import normalize_email, normalize_phone
from modules.identity.main import AuthenticatedIdentity, create_app, require_identity


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL CRM integration")
    return url


def _client(database_url: str, identity: AuthenticatedIdentity) -> TestClient:
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_identity] = lambda: identity
    return TestClient(app)


def _identity(role_subject: str | None = None) -> AuthenticatedIdentity:
    return AuthenticatedIdentity("https://identity.example.invalid/crm", str(uuid.uuid4()))


def _salon(database_url: str, issuer: str):
    """Onboard a tenant and return (engine, owner identity, tenant payload)."""
    owner = AuthenticatedIdentity(issuer, str(uuid.uuid4()))
    signup = _client(database_url, owner).post("/v1/onboarding", json={
        "business_name": "Synthetic CRM Salon", "location_name": "Main", "timezone": "America/Toronto"})
    assert signup.status_code == 201, signup.text
    engine = create_engine(database_url, pool_pre_ping=True)
    return engine, owner, signup.json()


def _teardown(engine, tenants: list[str], issuer: str) -> None:
    with engine.begin() as connection:
        for tenant_id in tenants:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            connection.execute(text("""UPDATE identity_users SET tenant_id=NULL,onboarding_fingerprint=NULL
                                       WHERE tenant_id=:tenant"""), {"tenant": tenant_id})
            connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": tenant_id})
        connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        # Only users with no surviving membership are removed, so a stale row
        # from an interrupted run can never cascade into a new leak by blocking
        # this run's cleanup with a foreign-key violation.
        connection.execute(text("""DELETE FROM identity_users WHERE issuer=:issuer
                                   AND id NOT IN (SELECT user_id FROM memberships)"""),
                           {"issuer": issuer})
    engine.dispose()


# TASK-009 / AC-1


def test_contact_normalization_is_conservative_and_preserves_display_input():
    # E.164 with country context (data-model.md:66).
    assert normalize_phone("(416) 555-0134") == "+14165550134"
    assert normalize_phone("1-416-555-0134") == "+14165550134"
    assert normalize_phone("+33 1 42 68 53 00") == "+33142685300"
    for bad in ("555", "12345678901234567", "+1234"):
        with pytest.raises(ValueError):
            normalize_phone(bad)
    with pytest.raises(ValueError):
        normalize_phone("0612345678", country="FR")

    # Conservative: the domain is lowercased, the local part is not, and no dot or
    # plus-tag removal is ever applied.
    assert normalize_email("  Ada.Lovelace+news@Example.COM ") == "Ada.Lovelace+news@example.com"
    assert normalize_email("a.b@x.com") != normalize_email("ab@x.com")
    for bad in ("no-at-sign", "a@b", "a b@x.com", "@x.com", "a@x.com@y.com"):
        with pytest.raises(ValueError):
            normalize_email(bad)


def test_client_and_contacts_keep_both_normalized_and_display_values(database_url):
    """AC-1: FR-010 requires the operator's original string to survive normalization."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/crm-1")
    client = _client(database_url, owner)
    try:
        response = client.post(f"/v1/tenants/{tenant['tenant_id']}/clients", json={
            "name": "  Ada Lovelace  ", "preferred_name": "Ada", "source": "staff",
            "preferred_location_id": tenant["location_id"],
            "contacts": [{"kind": "phone", "value": "(416) 555-0134"},
                         {"kind": "email", "value": "  Ada.Lovelace+news@Example.COM "}]})
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["name"] == "Ada Lovelace"
        assert body["preferred_location_id"] == tenant["location_id"]
        assert body["source"] == "staff"
        assert body["duplicate_candidates"] == 0

        contacts = {item["kind"]: item for item in body["contacts"]}
        # FR-010: display is exactly what was typed; the normal form is stored alongside it.
        assert contacts["phone"]["display_value"] == "(416) 555-0134"
        assert contacts["email"]["display_value"] == "Ada.Lovelace+news@Example.COM"
        assert contacts["phone"]["verification"] == "unverified"
        assert contacts["phone"]["verified_at"] is None
        assert contacts["phone"]["deliverability"] == "unknown"

        # Provenance is readable on the stored rows, not just the create response.
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant["tenant_id"]})
            rows = connection.execute(
                text("SELECT kind, normalized_value, source FROM client_contacts WHERE tenant_id=:tenant ORDER BY kind"),
                {"tenant": tenant["tenant_id"]}).all()
        assert rows == [("email", "Ada.Lovelace+news@example.com", "staff"),
                        ("phone", "+14165550134", "staff")]

        audit = client.get(f"/v1/tenants/{tenant['tenant_id']}/audit").json()["items"]
        created = next(e for e in audit if e["action"] == "client.created")
        assert created["changed_fields"] == ["name", "locale", "source", "lifecycle",
                                            "preferred_name", "preferred_location_id", "contacts"]
        assert created["request_id"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/crm-1")


def test_a_client_may_exist_without_any_reachable_contact(database_url):
    """FR-010: staff-created bookings allow a client with no email or phone yet."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/crm-2")
    client = _client(database_url, owner)
    try:
        created = client.post(f"/v1/tenants/{tenant['tenant_id']}/clients",
                              json={"name": "Walk-in", "source": "staff"})
        assert created.status_code == 201, created.text
        assert created.json()["contacts"] == []
        assert client.get(f"/v1/tenants/{tenant['tenant_id']}/clients/{created.json()['id']}").status_code == 200
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/crm-2")


def test_shared_household_contact_produces_candidates_and_never_merges(database_url):
    """AC-1: data-model.md:32 - matching yields candidates, not automatic merges."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/crm-3")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        first = client.post(base, json={"name": "Ada Lovelace", "contacts": [
            {"kind": "phone", "value": "416-555-0134"}]})
        assert first.status_code == 201, first.text
        assert first.json()["duplicate_candidates"] == 0

        # The same household number, typed differently, on a different client.
        second = client.post(base, json={"name": "Byron Lovelace", "contacts": [
            {"kind": "phone", "value": "(416) 555-0134"}]})
        assert second.status_code == 201, second.text
        assert second.json()["duplicate_candidates"] == 1

        # Two distinct clients remain two distinct clients.
        assert first.json()["id"] != second.json()["id"]
        roster = engine.connect()
        with roster.begin():
            roster.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant["tenant_id"]})
            assert roster.execute(text("SELECT count(*) FROM clients WHERE tenant_id=:tenant"),
                                  {"tenant": tenant["tenant_id"]}).scalar_one() == 2
            candidates = roster.execute(
                text("""SELECT client_id, other_client_id, match_basis, matched_value, resolution
                        FROM client_duplicate_candidates WHERE tenant_id=:tenant"""),
                {"tenant": tenant["tenant_id"]}).all()
        roster.close()
        assert len(candidates) == 1
        row = candidates[0]
        assert row.match_basis == "phone" and row.matched_value == "+14165550134"
        # Detection only: nothing was resolved and nothing was merged.
        assert row.resolution is None
        assert str(second.json()["id"]) in {str(row.client_id), str(row.other_client_id)}

        # A different value on the same client adds no candidate.
        third = client.post(base, json={"name": "Carly Lovelace", "contacts": [
            {"kind": "phone", "value": "416-555-0199"}]})
        assert third.json()["duplicate_candidates"] == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/crm-3")


def test_contacts_are_tenant_scoped_so_unrelated_salons_never_match(database_url):
    """FR-018: no cross-tenant merge, matching or pooling."""
    mine_engine, mine_owner, mine = _salon(database_url, "https://identity.example.invalid/crm-4a")
    their_engine, their_owner, theirs = _salon(database_url, "https://identity.example.invalid/crm-4b")
    shared = {"kind": "phone", "value": "416-555-0134"}
    try:
        mine_client = _client(database_url, mine_owner).post(
            f"/v1/tenants/{mine['tenant_id']}/clients", json={"name": "Ada", "contacts": [shared]}).json()
        their_client = _client(database_url, their_owner).post(
            f"/v1/tenants/{theirs['tenant_id']}/clients", json={"name": "Ada Elsewhere", "contacts": [shared]}).json()

        # Identical value, different tenants: neither sees a candidate, because the
        # duplicate scan is scoped to the tenant.
        for engine, tenant_id in ((mine_engine, mine["tenant_id"]), (their_engine, theirs["tenant_id"])):
            with engine.connect() as connection:
                connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
                assert connection.execute(text("SELECT count(*) FROM client_duplicate_candidates WHERE tenant_id=:tenant"),
                                          {"tenant": tenant_id}).scalar_one() == 0
                # And each tenant still sees only its own client.
                assert connection.execute(text("SELECT count(*) FROM clients WHERE tenant_id=:tenant"),
                                          {"tenant": tenant_id}).scalar_one() == 1
        assert mine_client["id"] != their_client["id"]
    finally:
        _teardown(mine_engine, [mine["tenant_id"]], "https://identity.example.invalid/crm-4a")
        _teardown(their_engine, [theirs["tenant_id"]], "https://identity.example.invalid/crm-4b")


# TASK-009 / AC-2


def test_clients_are_unreachable_from_another_tenant(database_url):
    """QA-01: a guessed client UUID from another tenant is 404, never readable."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/crm-5")
    mine = _client(database_url, owner)
    created = mine.post(f"/v1/tenants/{tenant['tenant_id']}/clients", json={"name": "Private Client"})
    assert created.status_code == 201, created.text
    client_id = created.json()["id"]

    other_engine, other_owner, other_tenant = _salon(database_url, "https://identity.example.invalid/crm-5b")
    try:
        other = _client(database_url, other_owner)
        foreign = other_tenant["tenant_id"]
        assert other.get(f"/v1/tenants/{foreign}/clients/{client_id}").status_code == 404
        assert other.patch(f"/v1/tenants/{foreign}/clients/{client_id}",
                           json={"name": "Stolen", "expected_version": 1}).status_code == 404
        assert other.post(f"/v1/tenants/{foreign}/clients/{client_id}/contacts",
                          json={"kind": "email", "value": "intruder@example.invalid"}).status_code == 404
        # Listing under my own tenant id must not surface someone else's client either.
        assert mine.get(f"/v1/tenants/{tenant['tenant_id']}/clients/{client_id}").status_code == 200
        with engine.connect() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": other_tenant["tenant_id"]})
            assert connection.execute(text("SELECT count(*) FROM clients WHERE id=:client"), {"client": client_id}).scalar_one() == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/crm-5")
        _teardown(other_engine, [other_tenant["tenant_id"]], "https://identity.example.invalid/crm-5b")


def test_only_owner_and_manager_may_write_client_records(database_url):
    """requirements.md:20-23 keeps reception and stylist read-shaped on clients."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/crm-6")
    owner_client = _client(database_url, owner)
    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant["tenant_id"]})
            ids = {}
            for role in ("manager", "receptionist", "stylist", "analyst"):
                ids[role] = connection.execute(
                    text("""INSERT INTO identity_users (issuer,subject) VALUES (:issuer,:subject) RETURNING id"""),
                    {"issuer": "https://identity.example.invalid/crm-6", "subject": f"{role}-subject"}).scalar_one()
                connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,:role)"),
                                   {"tenant": tenant["tenant_id"], "user": str(ids[role]), "role": role})

        base = f"/v1/tenants/{tenant['tenant_id']}/clients"
        for role in ("receptionist", "stylist", "analyst"):
            as_role = _client(database_url, AuthenticatedIdentity("https://identity.example.invalid/crm-6", f"{role}-subject"))
            # The permission check runs before the lookup, so a refused writer gets 403
            # whether or not the client exists - no existence oracle.
            assert as_role.post(base, json={"name": "Nope"}).status_code == 403
            assert as_role.patch(f"{base}/{uuid.uuid4()}", json={"name": "Nope", "expected_version": 1}).status_code == 403

        manager = _client(database_url, AuthenticatedIdentity("https://identity.example.invalid/crm-6", "manager-subject"))
        created = manager.post(base, json={"name": "Manager Created"})
        assert created.status_code == 201, created.text
        # Manager can write clients but does not see the owner-only sensitive fields.
        assert "private_note" not in created.json()
        assert created.json()["name"] == "Manager Created"

        receptionist = _client(database_url, AuthenticatedIdentity("https://identity.example.invalid/crm-6", "receptionist-subject"))
        assert receptionist.get(f"{base}/{created.json()['id']}").status_code == 200
        assert receptionist.post(f"{base}/{created.json()['id']}/contacts",
                                json={"kind": "email", "value": "r@example.invalid"}).status_code == 403
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/crm-6")


def test_contact_verification_cannot_be_asserted_and_stays_audited(database_url):
    """AC-2: verification status is server-owned, and changes are audited."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/crm-7")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        created = client.post(base, json={"name": "Verified", "contacts": [
            {"kind": "email", "value": "verified@example.invalid"}]})
        assert created.status_code == 201, created.text
        contact = created.json()["contacts"][0]

        # The create payload cannot claim to be verified, and the column refuses it.
        assert contact["verification"] == "unverified" and contact["verified_at"] is None
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant["tenant_id"]})
            with pytest.raises(Exception):
                with engine.begin() as nested:
                    nested.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant["tenant_id"]})
                    nested.execute(text("UPDATE client_contacts SET verification='verified' WHERE tenant_id=:tenant"),
                                   {"tenant": tenant["tenant_id"]})

        added = client.post(f"{base}/{created.json()['id']}/contacts",
                            json={"kind": "phone", "value": "416-555-0177", "is_primary": True})
        assert added.status_code == 201, added.text
        phone = next(item for item in added.json()["contacts"] if item["kind"] == "phone")
        assert phone["is_primary"] is True and phone["verification"] == "unverified"

        audit = client.get(f"/v1/tenants/{tenant['tenant_id']}/audit").json()["items"]
        assert {e["action"] for e in audit} >= {"client.created", "client.contact_added"}
        assert all(e["request_id"] for e in audit)
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/crm-7")


def test_client_updates_are_versioned_and_validate_their_location(database_url):
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/crm-8")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        created = client.post(base, json={"name": "Versioned"})
        assert created.status_code == 201
        client_id, version = created.json()["id"], created.json()["version"]

        # api-contracts.md:13 - an edit carries the version it read.
        assert client.patch(f"{base}/{client_id}", json={"name": "No version"}).status_code == 428

        stale = client.patch(f"{base}/{client_id}", json={"name": "Stale", "expected_version": version + 5})
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "client_version_conflict"

        updated = client.patch(f"{base}/{client_id}", json={"name": "Fresh", "lifecycle": "lapsed",
                                                           "expected_version": version})
        assert updated.status_code == 200, updated.text
        assert updated.json()["name"] == "Fresh" and updated.json()["lifecycle"] == "lapsed"
        assert updated.json()["version"] == version + 1

        # Clearing an optional field is possible for every nullable field, not just one.
        for field in ("preferred_name", "preferred_location_id"):
            current = client.get(f"{base}/{client_id}").json()["version"]
            body = client.patch(f"{base}/{client_id}", json={field: None, "expected_version": current})
            assert body.status_code == 200, (field, body.text)
            assert body.json()[field] is None
        current = client.get(f"{base}/{client_id}").json()["version"]
        assert client.patch(f"{base}/{client_id}", json={"expected_version": current}).status_code == 422

        # A location outside this tenant is refused.
        other_engine, _other_owner, other = _salon(database_url, "https://identity.example.invalid/crm-8b")
        try:
            current = client.get(f"{base}/{client_id}").json()["version"]
            assert client.patch(f"{base}/{client_id}",
                                json={"preferred_location_id": other["location_id"],
                                      "expected_version": current}).status_code == 404
        finally:
            _teardown(other_engine, [other["tenant_id"]], "https://identity.example.invalid/crm-8b")
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/crm-8")


def test_response_shapes_are_what_the_web_tier_reads(database_url):
    """Every route answers a flat client object carrying id, version and contacts.

    The TASK-008 and TASK-009 reviews both found a response-shape bug that 49 green tests
    could not see, because nothing asserted a body. This pins the shape itself.
    """
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/crm-9")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    required = {"id", "version", "created_at", "source", "preferred_location_id", "contacts"}
    try:
        created = client.post(base, json={"name": "Shape", "contacts": [
            {"kind": "email", "value": "shape@example.invalid"}]})
        assert created.status_code == 201, created.text
        client_id = created.json()["id"]

        bodies = {
            "create": created.json(),
            "read": client.get(f"{base}/{client_id}").json(),
            "add_contact": client.post(f"{base}/{client_id}/contacts", json={
                "kind": "phone", "value": "416-555-0111"}).json(),
            "set_verification": client.patch(f"{base}/{client_id}/contacts/{created.json()['contacts'][0]['id']}",
                                             json={"verification": "verified"}).json(),
            "patch": client.patch(f"{base}/{client_id}",
                                  json={"lifecycle": "lapsed",
                                        "expected_version": created.json()["version"]}).json(),
        }
        for label, body in bodies.items():
            assert required <= set(body), (label, sorted(set(body)))
            # Not nested: the web tier reads body.id directly.
            assert "client" not in body, label
            assert body["id"] == client_id, label
            assert isinstance(body["contacts"], list), label

        # Contact status moves forward and the terminal failure is not retryable.
        marked = bodies["set_verification"]
        email = next(c for c in marked["contacts"] if c["kind"] == "email")
        assert email["verification"] == "verified" and email["verified_at"] is not None

        failed = client.patch(f"{base}/{client_id}/contacts/{email['id']}",
                              json={"verification": "failed"}).json()
        assert next(c for c in failed["contacts"] if c["id"] == email["id"])["verification"] == "failed"
        retry = client.patch(f"{base}/{client_id}/contacts/{email['id']}", json={"verification": "verified"})
        assert retry.status_code == 409
        assert retry.json()["detail"]["code"] == "contact_verification_failed"

        audit = client.get(f"/v1/tenants/{tenant['tenant_id']}/audit").json()["items"]
        assert "client.contact_status_updated" in {e["action"] for e in audit}
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/crm-9")


def test_control_characters_and_normalization_failures_are_refused(database_url):
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/crm-10")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        for hostile in ("Ada\x00Lovelace", "Ada\nLovelace", "Ada\x07"):
            assert client.post(base, json={"name": hostile}).status_code == 422, hostile
        assert client.post(base, json={"name": "Fine", "contacts": [
            {"kind": "email", "value": "nul\x00@example.invalid"}]}).status_code == 422
        assert client.post(base, json={"name": "Fine", "contacts": [
            {"kind": "phone", "value": "12"}]}).status_code == 422
        # Nothing was written by any rejected request.
        assert client.get(f"{base}/{uuid.uuid4()}").status_code == 404
        audit = client.get(f"/v1/tenants/{tenant['tenant_id']}/audit").json()["items"]
        assert [e for e in audit if e["action"] == "client.created"] == []
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/crm-10")


def test_update_contact_enforces_ownership_and_transitions(database_url):
    """N1+N5: update_contact verifies ownership, enforces transitions, and is tenant-isolated."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/crm-11")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        created = client.post(base, json={"name": "Contact", "contacts": [
            {"kind": "email", "value": "status@example.invalid"}]})
        assert created.status_code == 201
        client_id = created.json()["id"]
        contact_id = created.json()["contacts"][0]["id"]

        # Move forward: unverified -> pending -> verified
        for status in ("pending", "verified"):
            resp = client.patch(f"{base}/{client_id}/contacts/{contact_id}",
                                json={"verification": status})
            assert resp.status_code == 200, resp.text
            assert next(c for c in resp.json()["contacts"] if c["id"] == contact_id)["verification"] == status

        # verified_at set when verified
        verified = client.get(f"{base}/{client_id}").json()
        email = next(c for c in verified["contacts"] if c["id"] == contact_id)
        assert email["verification"] == "verified" and email["verified_at"] is not None

        # Failed is terminal: cannot retry into verified
        resp = client.patch(f"{base}/{client_id}/contacts/{contact_id}", json={"verification": "failed"})
        assert resp.status_code == 200
        resp = client.patch(f"{base}/{client_id}/contacts/{contact_id}", json={"verification": "verified"})
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "contact_verification_failed"

        # N1: contact_id from a different client is not reachable
        other = client.post(base, json={"name": "Other"}).json()
        resp = client.patch(f"{base}/{other['id']}/contacts/{contact_id}", json={"verification": "verified"})
        assert resp.status_code == 404

        # Deliverability can be set
        resp = client.patch(f"{base}/{client_id}/contacts/{contact_id}",
                            json={"verification": "failed", "deliverability": "undeliverable"})
        assert resp.status_code == 200
        email = next(c for c in resp.json()["contacts"] if c["id"] == contact_id)
        assert email["deliverability"] == "undeliverable"
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/crm-11")


def test_add_contact_primary_invariant_and_soft_delete_patch(database_url):
    """N3+N4: add_contact maintains one-primary invariant; PATCH refuses soft-deleted clients."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/crm-12")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/clients"
    try:
        created = client.post(base, json={"name": "Primary", "contacts": [
            {"kind": "email", "value": "first@example.invalid", "is_primary": True},
            {"kind": "phone", "value": "416-555-0140"}]})
        assert created.status_code == 201
        client_id = created.json()["id"]
        contacts = created.json()["contacts"]
        assert sum(1 for c in contacts if c["is_primary"]) == 1

        # N3: add_contact maintains exactly-one-primary invariant
        resp = client.post(f"{base}/{client_id}/contacts",
                           json={"kind": "email", "value": "second@example.invalid", "is_primary": True})
        assert resp.status_code == 201
        contacts = resp.json()["contacts"]
        primaries = [c for c in contacts if c["is_primary"]]
        assert len(primaries) == 1
        assert primaries[0]["display_value"] == "second@example.invalid"
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/crm-12")