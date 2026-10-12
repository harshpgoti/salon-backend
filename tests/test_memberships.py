from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import modules.identity.main as identity_main
from modules.identity.main import AuthenticatedIdentity, _lock_owners_and_target, create_app, require_identity


@pytest.fixture(scope="session")
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL membership lifecycle integration")
    return url


def _client(database_url: str, identity: AuthenticatedIdentity) -> TestClient:
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_identity] = lambda: identity
    return TestClient(app)


def _verified_user(connection, issuer: str, email: str, subject: str | None = None) -> tuple[uuid.UUID, str]:
    """Create a verified identity and return its (user_id, subject) so tests can authenticate as it."""
    subject = subject or str(uuid.uuid4())
    user_id = connection.execute(
        text("""INSERT INTO identity_users (issuer,subject,email,email_verified,auth_time)
               VALUES (:issuer,:subject,:email,true,to_timestamp(:auth_time)) RETURNING id"""),
        {"issuer": issuer, "subject": subject, "email": email, "auth_time": time.time()},
    ).scalar_one()
    return user_id, subject


@pytest.fixture()
def salon(database_url):
    engine = create_engine(database_url, pool_pre_ping=True)
    issuer = "https://identity.example.invalid/membership-test"
    owner = AuthenticatedIdentity(issuer, str(uuid.uuid4()), email="owner@membership.invalid", email_verified=True, auth_time=time.time())
    owner_app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    owner_app.dependency_overrides[require_identity] = lambda: owner
    signup = TestClient(owner_app).post("/v1/onboarding", json={
        "business_name": "Synthetic Membership Salon", "location_name": "Downtown", "timezone": "America/Toronto"})
    assert signup.status_code == 201, signup.text
    tenant_id = signup.json()["tenant_id"]
    location_id = signup.json()["location_id"]

    def cleanup():
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            connection.execute(text("""UPDATE identity_users SET tenant_id=NULL,onboarding_fingerprint=NULL
                                       WHERE tenant_id=:tenant"""), {"tenant": tenant_id})
            connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": tenant_id})
            connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            connection.execute(text("DELETE FROM auth_sessions WHERE issuer=:issuer"), {"issuer": issuer})
            connection.execute(text("DELETE FROM identity_users WHERE issuer=:issuer"), {"issuer": issuer})
        engine.dispose()

    yield {"engine": engine, "issuer": issuer, "owner": owner, "tenant_id": tenant_id, "location_id": location_id}
    cleanup()


def test_invitation_accept_revoke_and_expire_control_staff_access(salon):
    """AC-1: accepting grants access, revoking and expiry deny it, deactivation revokes permissions."""
    engine, issuer, tenant_id, location_id = salon["engine"], salon["issuer"], salon["tenant_id"], salon["location_id"]
    database_url = os.environ["DATABASE_URL"]
    owner = _client(database_url, salon["owner"])

    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        accepted_id, accepted_subject = _verified_user(connection, issuer, "accepted@membership.invalid")
        _revoked_id, revoked_subject = _verified_user(connection, issuer, "revoked@membership.invalid")
        _expired_id, expired_subject = _verified_user(connection, issuer, "expired@membership.invalid")

    accepted = owner.post(f"/v1/tenants/{tenant_id}/invitations",
                          json={"email": "Accepted@Membership.invalid", "role": "stylist", "location_ids": [location_id]})
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["delivery_status"] == "recorded_only"
    revoked = owner.post(f"/v1/tenants/{tenant_id}/invitations",
                         json={"email": "revoked@membership.invalid", "role": "receptionist", "location_ids": [location_id]})
    expired = owner.post(f"/v1/tenants/{tenant_id}/invitations",
                         json={"email": "expired@membership.invalid", "role": "manager", "location_ids": [location_id]})
    assert revoked.status_code == 201 and expired.status_code == 201

    # A second pending invitation for one email is rejected.
    assert owner.post(f"/v1/tenants/{tenant_id}/invitations",
                      json={"email": "revoked@membership.invalid", "role": "stylist", "location_ids": [location_id]}).status_code == 409

    assert owner.delete(f"/v1/tenants/{tenant_id}/memberships/{accepted_id}",
                        params={"reason": "invitation withdrawn by owner"}).status_code == 404

    revoke = owner.post(f"/v1/tenants/{tenant_id}/invitations/{revoked.json()['invitation_id']}/revoke")
    assert revoke.status_code == 200 and revoke.json()["state"] == "revoked"
    assert owner.post(f"/v1/tenants/{tenant_id}/invitations/{revoked.json()['invitation_id']}/revoke").status_code == 409

    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        # Backdate creation as well, because the table requires expires_at > created_at.
        connection.execute(text("""UPDATE tenant_invitations
                                   SET created_at=now()-interval '8 days', expires_at=now()-interval '1 minute'
                                   WHERE id=:id"""),
                           {"id": expired.json()["invitation_id"]})

    revoked_identity = AuthenticatedIdentity(issuer, revoked_subject, email="revoked@membership.invalid", email_verified=True)
    expired_identity = AuthenticatedIdentity(issuer, expired_subject, email="expired@membership.invalid", email_verified=True)

    # Revoked and expired invitations both refuse acceptance.
    assert _client(database_url, revoked_identity).post(
        f"/v1/tenants/{tenant_id}/invitations/{revoked.json()['invitation_id']}/accept").status_code == 409
    assert _client(database_url, expired_identity).post(
        f"/v1/tenants/{tenant_id}/invitations/{expired.json()['invitation_id']}/accept").status_code == 409

    accepted_identity = AuthenticatedIdentity(issuer, accepted_subject, email="accepted@membership.invalid", email_verified=True)
    accepted_client = _client(database_url, accepted_identity)

    # Unverified invitees cannot accept even with a matching session.
    unverified = AuthenticatedIdentity(issuer, str(uuid.uuid4()), email="accepted@membership.invalid", email_verified=False)
    assert _client(database_url, unverified).post(
        f"/v1/tenants/{tenant_id}/invitations/{accepted.json()['invitation_id']}/accept").status_code == 403

    accept = accepted_client.post(f"/v1/tenants/{tenant_id}/invitations/{accepted.json()['invitation_id']}/accept")
    assert accept.status_code == 201, accept.text
    assert accept.json() == {"tenant_id": tenant_id, "role": "stylist", "state": "accepted"}

    # Accepting grants location-scoped access and appears in the team roster.
    assert accepted_client.get(f"/v1/tenants/{tenant_id}/locations").status_code == 200
    roster = owner.get(f"/v1/tenants/{tenant_id}/memberships")
    assert roster.status_code == 200
    entry = next(item for item in roster.json()["items"] if item["user_id"] == str(accepted_id))
    assert entry["role"] == "stylist" and entry["location_ids"] == [location_id]

    # The invitation cannot be reused.
    assert accepted_client.post(f"/v1/tenants/{tenant_id}/invitations/{accepted.json()['invitation_id']}/accept").status_code == 409

    deactivate = owner.delete(f"/v1/tenants/{tenant_id}/memberships/{accepted_id}",
                              params={"reason": "staff member left the salon"})
    assert deactivate.status_code == 204
    assert accepted_client.get(f"/v1/tenants/{tenant_id}/locations").status_code == 404

    audit = owner.get(f"/v1/tenants/{tenant_id}/audit").json()["items"]
    actions = {event["action"] for event in audit}
    assert {"membership.invited", "membership.invitation_revoked", "membership.invitation_accepted", "membership.deactivated"} <= actions
    assert all(event["request_id"] for event in audit)


def test_team_management_is_owner_only(salon):
    """AC-1: only the owner may invite, deactivate or transfer; deactivating needs a reason."""
    database_url = os.environ["DATABASE_URL"]
    tenant_id, location_id = salon["tenant_id"], salon["location_id"]
    owner = _client(database_url, salon["owner"])

    with salon["engine"].begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        manager_id, manager_subject = _verified_user(connection, salon["issuer"], "manager@membership.invalid")
        connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'manager')"),
                           {"tenant": tenant_id, "user": str(manager_id)})

    manager = AuthenticatedIdentity(salon["issuer"], manager_subject, email="manager@membership.invalid", email_verified=True)
    manager_client = _client(database_url, manager)
    body = {"email": "someone@membership.invalid", "role": "stylist", "location_ids": [location_id]}
    assert manager_client.get(f"/v1/tenants/{tenant_id}/memberships").status_code == 403
    assert manager_client.post(f"/v1/tenants/{tenant_id}/invitations", json=body).status_code == 403
    assert manager_client.patch(f"/v1/tenants/{tenant_id}/memberships/{manager_id}",
                                json={"role": "stylist", "reason": "self promotion"}).status_code == 403
    assert manager_client.delete(f"/v1/tenants/{tenant_id}/memberships/{manager_id}",
                                 params={"reason": "self removal"}).status_code == 403
    assert manager_client.post(f"/v1/tenants/{tenant_id}/ownership-transfers",
                               json={"target_email": "someone@membership.invalid"}).status_code == 403

    # Deactivating a membership requires an audit-worthy reason.
    assert owner.delete(f"/v1/tenants/{tenant_id}/memberships/{manager_id}", params={"reason": "ab"}).status_code == 422
    assert owner.delete(f"/v1/tenants/{tenant_id}/memberships/{manager_id}").status_code == 422
    assert owner.delete(f"/v1/tenants/{tenant_id}/memberships/{manager_id}",
                        params={"reason": "manager left the salon"}).status_code == 204


def test_last_owner_is_protected_and_transfer_requires_verified_acceptance(salon):
    """AC-2: the last owner cannot be removed or demoted, and ownership moves only by verified acceptance."""
    database_url = os.environ["DATABASE_URL"]
    engine, issuer, tenant_id = salon["engine"], salon["issuer"], salon["tenant_id"]
    owner = _client(database_url, salon["owner"])

    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        owner_user_id = connection.execute(text("SELECT user_id FROM memberships WHERE tenant_id=:tenant AND role='owner'"),
                                            {"tenant": tenant_id}).scalar_one()

    # The last owner is protected from demotion and removal.
    assert owner.patch(f"/v1/tenants/{tenant_id}/memberships/{owner_user_id}",
                       json={"role": "manager", "reason": "stepping back"}).status_code == 409
    assert owner.delete(f"/v1/tenants/{tenant_id}/memberships/{owner_user_id}",
                        params={"reason": "closing the salon"}).status_code == 409

    stale = AuthenticatedIdentity(issuer, salon["owner"].subject, email=salon["owner"].email,
                                  email_verified=True, auth_time=time.time() - 3600)
    assert _client(database_url, stale).post(f"/v1/tenants/{tenant_id}/ownership-transfers",
                                             json={"target_email": "successor@membership.invalid"}).status_code == 403

    unverified = AuthenticatedIdentity(issuer, salon["owner"].subject, email=salon["owner"].email,
                                       email_verified=False, auth_time=time.time())
    assert _client(database_url, unverified).post(f"/v1/tenants/{tenant_id}/ownership-transfers",
                                                  json={"target_email": "successor@membership.invalid"}).status_code == 403

    assert owner.post(f"/v1/tenants/{tenant_id}/ownership-transfers",
                      json={"target_email": salon["owner"].email}).status_code == 422

    transfer = owner.post(f"/v1/tenants/{tenant_id}/ownership-transfers",
                          json={"target_email": "Successor@Membership.invalid"})
    assert transfer.status_code == 201, transfer.text
    transfer_id = transfer.json()["transfer_id"]

    # Only one pending transfer exists at a time.
    assert owner.post(f"/v1/tenants/{tenant_id}/ownership-transfers",
                      json={"target_email": "other@membership.invalid"}).status_code == 409

    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        successor_id, successor_subject = _verified_user(connection, issuer, "successor@membership.invalid")
        connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'manager')"),
                           {"tenant": tenant_id, "user": str(successor_id)})

    # A verified but wrong recipient cannot accept the transfer.
    stranger = AuthenticatedIdentity(issuer, str(uuid.uuid4()), email="stranger@membership.invalid", email_verified=True)
    assert _client(database_url, stranger).post(
        f"/v1/tenants/{tenant_id}/ownership-transfers/{transfer_id}/accept").status_code == 404

    successor = AuthenticatedIdentity(issuer, successor_subject, email="successor@membership.invalid", email_verified=True)
    accept = _client(database_url, successor).post(f"/v1/tenants/{tenant_id}/ownership-transfers/{transfer_id}/accept")
    assert accept.status_code == 200, accept.text
    assert accept.json() == {"tenant_id": tenant_id, "state": "accepted", "role": "owner"}

    # Ownership has moved, so the roster must be read as the new owner.
    successor_client = _client(database_url, successor)
    roster_response = successor_client.get(f"/v1/tenants/{tenant_id}/memberships")
    assert roster_response.status_code == 200, roster_response.text
    roster = {item["user_id"]: item["role"] for item in roster_response.json()["items"]}
    assert roster[str(successor_id)] == "owner" and roster[str(owner_user_id)] == "manager"

    # The new owner can manage the team; the demoted previous owner cannot.
    assert successor_client.get(f"/v1/tenants/{tenant_id}/memberships").status_code == 200
    demoted = _client(database_url, salon["owner"])
    assert demoted.get(f"/v1/tenants/{tenant_id}/memberships").status_code == 403

    # The single remaining owner is still protected.
    assert successor_client.delete(f"/v1/tenants/{tenant_id}/memberships/{successor_id}",
                                    params={"reason": "attempt to leave"}).status_code == 409


def test_deactivation_takes_the_shared_owner_lock_before_it_deletes(salon, monkeypatch):
    """The lock test proves `_lock_owners_and_target` locks; this proves deactivation uses it.

    Without this, a future edit could drop the call and the owner guard would silently
    return to an unlocked read-then-delete while both tests still passed.
    """
    database_url = os.environ["DATABASE_URL"]
    tenant_id, location_id = salon["tenant_id"], salon["location_id"]
    owner = _client(database_url, salon["owner"])

    with salon["engine"].begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        member_id, _ = _verified_user(connection, salon["issuer"], "locktarget@membership.invalid")
        connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'manager')"),
                           {"tenant": tenant_id, "user": str(member_id)})

    calls: list = []
    real_lock = identity_main._lock_owners_and_target

    def spy(connection, scope_tenant_id, target_user_id):
        calls.append((str(scope_tenant_id), str(target_user_id)))
        return real_lock(connection, scope_tenant_id, target_user_id)

    monkeypatch.setattr(identity_main, "_lock_owners_and_target", spy)
    assert owner.request("DELETE", f"/v1/tenants/{tenant_id}/memberships/{member_id}",
                         json={"reason": "checking the guard is wired"}).status_code == 204
    assert calls == [(tenant_id, str(member_id))]

    # The member is really gone, and the tenant still has its owner.
    roster = owner.get(f"/v1/tenants/{tenant_id}/memberships").json()["items"]
    assert str(member_id) not in {item["user_id"] for item in roster}
    assert [item["role"] for item in roster].count("owner") == 1


def test_deactivating_a_previous_owner_keeps_their_transfer_history(database_url):
    """Migration 008: a completed transfer must not make its initiator undeletable."""
    engine = create_engine(database_url, pool_pre_ping=True)
    issuer = "https://identity.example.invalid/history"
    owner = AuthenticatedIdentity(issuer, str(uuid.uuid4()), email="owner@membership.invalid",
                                  email_verified=True, auth_time=time.time())
    heir = AuthenticatedIdentity(issuer, str(uuid.uuid4()), email="heir@membership.invalid", email_verified=True)
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_identity] = lambda: owner
    signup = TestClient(app).post("/v1/onboarding", json={
        "business_name": "Synthetic History Salon", "location_name": "Main", "timezone": "America/Toronto"})
    assert signup.status_code == 201, signup.text
    tenant_id = signup.json()["tenant_id"]

    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            owner_id = str(connection.execute(text("SELECT user_id FROM memberships WHERE tenant_id=:tenant AND role='owner'"),
                                              {"tenant": tenant_id}).scalar_one())
            heir_id = connection.execute(
                text("""INSERT INTO identity_users (issuer,subject,email,email_verified,auth_time)
                        VALUES (:issuer,:subject,:email,true,to_timestamp(:auth_time)) RETURNING id"""),
                {"issuer": issuer, "subject": heir.subject, "email": heir.email, "auth_time": time.time()}).scalar_one()
            connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'manager')"),
                               {"tenant": tenant_id, "user": str(heir_id)})

        transfer = _client(database_url, owner).post(f"/v1/tenants/{tenant_id}/ownership-transfers",
                                                       json={"target_email": heir.email})
        assert transfer.status_code == 201, transfer.text
        assert _client(database_url, heir).post(
            f"/v1/tenants/{tenant_id}/ownership-transfers/{transfer.json()['transfer_id']}/accept").status_code == 200

        # The former owner is now a manager and can be removed; their history survives.
        assert _client(database_url, heir).request(
            "DELETE", f"/v1/tenants/{tenant_id}/memberships/{owner_id}",
            json={"reason": "handed the salon over"}).status_code == 204
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            row = connection.execute(
                text("""SELECT state, from_user_id::text AS from_user_id FROM ownership_transfers
                        WHERE tenant_id=:tenant"""), {"tenant": tenant_id}).one()
        assert row.state == "accepted" and row.from_user_id == owner_id
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            assert connection.execute(text("SELECT count(*) FROM memberships WHERE tenant_id=:tenant AND role='owner'"),
                                      {"tenant": tenant_id}).scalar_one() == 1
    finally:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            connection.execute(text("""UPDATE identity_users SET tenant_id=NULL,onboarding_fingerprint=NULL
                                       WHERE tenant_id=:tenant"""), {"tenant": tenant_id})
            connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": tenant_id})
            connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            connection.execute(text("DELETE FROM auth_sessions WHERE issuer=:issuer"), {"issuer": issuer})
            connection.execute(text("DELETE FROM identity_users WHERE issuer=:issuer"), {"issuer": issuer})
        engine.dispose()


def test_deactivation_role_read_holds_a_row_lock_against_concurrent_promotion(database_url):
    """The last-owner guard must read the role under the same lock the transfer path takes.

    An unlocked read-then-delete is what let a tenant fall to zero owners: deactivation
    reads "manager", a concurrent transfer promotes the same person to owner, and the
    delete then removes the only owner. Assert the deactivation path actually blocks a
    competing writer on that row rather than relying on statement ordering.
    """
    engine = create_engine(database_url, pool_pre_ping=True)
    issuer = "https://identity.example.invalid/owner-lock"
    owner = AuthenticatedIdentity(issuer, str(uuid.uuid4()), email="owner@membership.invalid",
                                  email_verified=True, auth_time=time.time())
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_identity] = lambda: owner
    signup = TestClient(app).post("/v1/onboarding", json={
        "business_name": "Synthetic Owner Lock Salon", "location_name": "Main", "timezone": "America/Toronto"})
    assert signup.status_code == 201, signup.text
    tenant_id = signup.json()["tenant_id"]

    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            heir_id = connection.execute(
                text("""INSERT INTO identity_users (issuer,subject,email,email_verified,auth_time)
                        VALUES (:issuer,:subject,'heir@membership.invalid',true,to_timestamp(:auth_time)) RETURNING id"""),
                {"issuer": issuer, "subject": str(uuid.uuid4()), "auth_time": time.time()}).scalar_one()
            connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'manager')"),
                               {"tenant": tenant_id, "user": str(heir_id)})

        # Hold the deactivation path's locks open in a transaction of our own, then try
        # to write the very row the guard depends on from a second connection.
        holder = engine.connect()
        holder.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        holder.execute(text("BEGIN"))
        locked = _lock_owners_and_target(holder, tenant_id, heir_id)
        assert locked[str(heir_id)] == "manager"

        competing_finished = threading.Event()
        errors: list = []

        def promote():
            try:
                writer = engine.connect()
                writer.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
                writer.execute(text("""UPDATE memberships SET role='owner'
                                       WHERE tenant_id=:tenant AND user_id=:user"""),
                               {"tenant": tenant_id, "user": str(heir_id)})
                writer.commit()
                writer.close()
            except Exception as exc:  # noqa: BLE001 - recorded for the assertion below
                errors.append(exc)
            finally:
                competing_finished.set()

        worker = threading.Thread(target=promote)
        worker.start()
        blocked = not competing_finished.wait(timeout=1.5)
        holder.commit()
        worker.join(timeout=10)
        holder.close()

        assert blocked, "deactivation read the role without holding a lock; a concurrent owner promotion can interleave"
        assert not errors, errors
    finally:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            connection.execute(text("""UPDATE identity_users SET tenant_id=NULL,onboarding_fingerprint=NULL
                                       WHERE tenant_id=:tenant"""), {"tenant": tenant_id})
            connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": tenant_id})
            connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            connection.execute(text("DELETE FROM auth_sessions WHERE issuer=:issuer"), {"issuer": issuer})
            connection.execute(text("DELETE FROM identity_users WHERE issuer=:issuer"), {"issuer": issuer})
        engine.dispose()


def test_first_run_onboarding_works_through_a_session_cookie(database_url, monkeypatch):
    """Guards the session resolver feeding onboarding: a DB-sourced datetime auth_time
    must not break the tenant INSERT that binds it."""
    monkeypatch.setenv("WEB_BASE_URL", "http://127.0.0.1:3000")
    engine = create_engine(database_url, pool_pre_ping=True)
    issuer = "https://identity.example.invalid/cookie-onboarding"
    subject = str(uuid.uuid4())
    tenants = []

    with engine.begin() as connection:
        # auth_time is a timestamptz column, so the resolved identity carries a
        # datetime exactly as a real session would.
        connection.execute(
            text("""INSERT INTO identity_users (issuer,subject,email,email_verified,auth_time)
                    VALUES (:issuer,:subject,'founder@membership.invalid',true,now())"""),
            {"issuer": issuer, "subject": subject},
        )

    try:
        token = uuid.uuid4().hex + uuid.uuid4().hex
        with engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO auth_sessions (session_hash,surface,issuer,subject,expires_at)
                        VALUES (:hash,'staff',:issuer,:subject,now()+interval '1 hour')"""),
                {"hash": hashlib.sha256(token.encode()).hexdigest(), "issuer": issuer, "subject": subject},
            )
        client = TestClient(create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True)),
                            raise_server_exceptions=False)
        client.cookies.set("salon_staff_session", token)

        response = client.post("/v1/onboarding", json={
            "business_name": "Synthetic Cookie Salon", "location_name": "Main", "timezone": "America/Toronto"},
            headers={"Origin": "http://127.0.0.1:3000"})
        assert response.status_code == 201, response.text
        tenants.append(response.json()["tenant_id"])
        assert response.json()["role"] == "owner"

        # The stored auth_time must remain a usable timestamptz, not be corrupted.
        with engine.connect() as connection:
            assert connection.execute(text("SELECT auth_time FROM identity_users WHERE issuer=:issuer AND subject=:subject"),
                                      {"issuer": issuer, "subject": subject}).scalar_one() is not None
    finally:
        with engine.begin() as connection:
            for tenant_id in tenants:
                connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
                connection.execute(text("""UPDATE identity_users SET tenant_id=NULL,onboarding_fingerprint=NULL
                                           WHERE tenant_id=:tenant"""), {"tenant": tenant_id})
                connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": tenant_id})
            connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            connection.execute(text("DELETE FROM auth_sessions WHERE issuer=:issuer"), {"issuer": issuer})
            connection.execute(text("DELETE FROM identity_users WHERE issuer=:issuer"), {"issuer": issuer})
        engine.dispose()


def test_concurrent_transfer_and_deactivation_never_orphans_the_tenant(database_url):
    """G-FOUNDATION owner guard: racing ownership transfer against removal of the
    outgoing owner must always leave the tenant with at least one owner."""
    engine = create_engine(database_url, pool_pre_ping=True)
    issuer = "https://identity.example.invalid/owner-race"
    owner = AuthenticatedIdentity(issuer, str(uuid.uuid4()), email="owner@membership.invalid",
                                  email_verified=True, auth_time=time.time())
    heir = AuthenticatedIdentity(issuer, str(uuid.uuid4()), email="heir@membership.invalid", email_verified=True)
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_identity] = lambda: owner
    signup = TestClient(app).post("/v1/onboarding", json={
        "business_name": "Synthetic Owner Race Salon", "location_name": "Main", "timezone": "America/Toronto"})
    assert signup.status_code == 201, signup.text
    tenant_id = signup.json()["tenant_id"]
    owner_id = str(uuid.uuid4())

    try:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            owner_id = str(connection.execute(text("SELECT user_id FROM memberships WHERE tenant_id=:tenant AND role='owner'"),
                                              {"tenant": tenant_id}).scalar_one())
            heir_id = connection.execute(
                text("""INSERT INTO identity_users (issuer,subject,email,email_verified,auth_time)
                        VALUES (:issuer,:subject,:email,true,to_timestamp(:auth_time)) RETURNING id"""),
                {"issuer": issuer, "subject": heir.subject, "email": heir.email, "auth_time": time.time()}).scalar_one()
            connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'manager')"),
                               {"tenant": tenant_id, "user": str(heir_id)})

        observed = []
        for _ in range(6):
            transfer = _client(database_url, owner).post(f"/v1/tenants/{tenant_id}/ownership-transfers",
                                                          json={"target_email": heir.email})
            assert transfer.status_code == 201, transfer.text
            transfer_id = transfer.json()["transfer_id"]

            def accept():
                return _client(database_url, heir).post(
                    f"/v1/tenants/{tenant_id}/ownership-transfers/{transfer_id}/accept").status_code

            def deactivate():
                return _client(database_url, owner).request(
                    "DELETE", f"/v1/tenants/{tenant_id}/memberships/{owner_id}",
                    json={"reason": "racing the handover"}).status_code

            with ThreadPoolExecutor(max_workers=2) as pool:
                codes = list(pool.map(lambda fn: fn(), (accept, deactivate)))
            assert 500 not in codes, codes

            with engine.begin() as connection:
                connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
                owners = connection.execute(
                    text("SELECT user_id FROM memberships WHERE tenant_id=:tenant AND role='owner'"),
                    {"tenant": tenant_id}).scalars().all()
            observed.append(len(owners))
            assert owners, f"tenant orphaned: accept={codes[0]} deactivate={codes[1]}"

            # Reset so the next round starts from a single known owner.
            with engine.begin() as connection:
                connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
                connection.execute(text("""DELETE FROM memberships WHERE tenant_id=:tenant
                                          AND user_id<>:owner AND role='owner'"""),
                                   {"tenant": tenant_id, "owner": owner_id})
                # The deactivation may have removed the owner's row outright, so upsert.
                connection.execute(text("""INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'owner')
                                           ON CONFLICT (tenant_id,user_id) DO UPDATE SET role='owner'"""),
                                   {"tenant": tenant_id, "user": owner_id})
                connection.execute(text("""INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'manager')
                                           ON CONFLICT (tenant_id,user_id) DO UPDATE SET role='manager'"""),
                                   {"tenant": tenant_id, "user": str(heir_id)})
        assert min(observed) >= 1
    finally:
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
            connection.execute(text("""UPDATE identity_users SET tenant_id=NULL,onboarding_fingerprint=NULL
                                       WHERE tenant_id=:tenant"""), {"tenant": tenant_id})
            connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": tenant_id})
            connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            connection.execute(text("DELETE FROM auth_sessions WHERE issuer=:issuer"), {"issuer": issuer})
            connection.execute(text("DELETE FROM identity_users WHERE issuer=:issuer"), {"issuer": issuer})
        engine.dispose()


def test_session_cookie_path_resolves_verified_identity_for_every_gated_accept(salon, monkeypatch):
    """The accept verbs must work for a real cookie session, not only a dependency override."""
    database_url = os.environ["DATABASE_URL"]
    engine, issuer, tenant_id = salon["engine"], salon["issuer"], salon["tenant_id"]
    monkeypatch.setenv("WEB_BASE_URL", "http://127.0.0.1:3000")
    origin = {"Origin": "http://127.0.0.1:3000"}

    def session_client(subject: str) -> TestClient:
        """Authenticate with a real auth_sessions row so require_identity runs for real."""
        token = uuid.uuid4().hex + uuid.uuid4().hex
        with engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO auth_sessions (session_hash,surface,issuer,subject,expires_at)
                        VALUES (:hash,'staff',:issuer,:subject,now()+interval '1 hour')"""),
                {"hash": hashlib.sha256(token.encode()).hexdigest(), "issuer": issuer, "subject": subject},
            )
        client = TestClient(create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True)))
        client.cookies.set("salon_staff_session", token)
        return client

    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        owner_user_id = connection.execute(text("SELECT user_id FROM memberships WHERE tenant_id=:tenant AND role='owner'"),
                                           {"tenant": tenant_id}).scalar_one()
        # The owner must exist as a verified identity for a transfer request to pass its gates.
        connection.execute(text("""UPDATE identity_users SET email='owner@membership.invalid',
                                    email_verified=true, auth_time=now() WHERE id=:user"""),
                           {"user": str(owner_user_id)})
        heir_id, heir_subject = _verified_user(connection, issuer, "heir@membership.invalid")
        connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'manager')"),
                           {"tenant": tenant_id, "user": str(heir_id)})

    owner_session = session_client(salon["owner"].subject)
    # Sanity: the session resolver produced a verified identity rather than the bare default.
    assert owner_session.get(f"/v1/tenants/{tenant_id}/memberships").status_code == 200
    transfer = owner_session.post(f"/v1/tenants/{tenant_id}/ownership-transfers",
                                  json={"target_email": "heir@membership.invalid"}, headers=origin)
    assert transfer.status_code == 201, transfer.text

    heir_session = session_client(heir_subject)
    accept = heir_session.post(f"/v1/tenants/{tenant_id}/ownership-transfers/{transfer.json()['transfer_id']}/accept",
                               headers=origin)
    assert accept.status_code == 200, accept.text
    assert accept.json() == {"tenant_id": tenant_id, "state": "accepted", "role": "owner"}


def test_invitation_acceptance_works_through_a_session_cookie(salon, monkeypatch):
    """AC-1 acceptance must resolve a verified invitee from its own session cookie."""
    database_url = os.environ["DATABASE_URL"]
    engine, issuer, tenant_id, location_id = salon["engine"], salon["issuer"], salon["tenant_id"], salon["location_id"]
    monkeypatch.setenv("WEB_BASE_URL", "http://127.0.0.1:3000")
    origin = {"Origin": "http://127.0.0.1:3000"}

    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        _user_id, subject = _verified_user(connection, issuer, "cookie-invitee@membership.invalid")

    invitation = _client(database_url, salon["owner"]).post(
        f"/v1/tenants/{tenant_id}/invitations",
        json={"email": "cookie-invitee@membership.invalid", "role": "stylist", "location_ids": [location_id]})
    assert invitation.status_code == 201, invitation.text

    token = uuid.uuid4().hex + uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO auth_sessions (session_hash,surface,issuer,subject,expires_at)
                    VALUES (:hash,'staff',:issuer,:subject,now()+interval '1 hour')"""),
            {"hash": hashlib.sha256(token.encode()).hexdigest(), "issuer": issuer, "subject": subject},
        )
    invitee = TestClient(create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True)))
    invitee.cookies.set("salon_staff_session", token)

    accepted = invitee.post(f"/v1/tenants/{tenant_id}/invitations/{invitation.json()['invitation_id']}/accept",
                            headers=origin)
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["role"] == "stylist"
    assert invitee.get(f"/v1/tenants/{tenant_id}/locations").status_code == 200


def test_deactivation_withdraws_pending_access_aimed_at_that_person(salon):
    """AC-1: deactivation is authoritative - a removed member keeps nothing, including a pending transfer."""
    database_url = os.environ["DATABASE_URL"]
    engine, issuer, tenant_id, location_id = salon["engine"], salon["issuer"], salon["tenant_id"], salon["location_id"]
    owner = _client(database_url, salon["owner"])

    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        heir_id, heir_subject = _verified_user(connection, issuer, "heir2@membership.invalid")

    # Invite first, while the person is not yet a member, then grant the membership
    # directly so both a pending invitation and a pending transfer exist for one person.
    invitation = owner.post(f"/v1/tenants/{tenant_id}/invitations",
                            json={"email": "heir2@membership.invalid", "role": "stylist", "location_ids": [location_id]})
    assert invitation.status_code == 201, invitation.text
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'manager')"),
                           {"tenant": tenant_id, "user": str(heir_id)})

    transfer = owner.post(f"/v1/tenants/{tenant_id}/ownership-transfers",
                          json={"target_email": "heir2@membership.invalid"})
    assert transfer.status_code == 201, transfer.text
    transfer_id = transfer.json()["transfer_id"]

    # Withdraw the membership.
    assert owner.request("DELETE", f"/v1/tenants/{tenant_id}/memberships/{heir_id}",
                         json={"reason": "no longer with the salon"}).status_code == 204

    # The pending transfer aimed at the removed person is withdrawn, not left live.
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        assert connection.execute(text("SELECT state FROM ownership_transfers WHERE tenant_id=:tenant AND id=:id"),
                                  {"tenant": tenant_id, "id": transfer_id}).scalar_one() == "revoked"
        assert connection.execute(text("SELECT state FROM tenant_invitations WHERE tenant_id=:tenant AND id=:id"),
                                  {"tenant": tenant_id, "id": invitation.json()["invitation_id"]}).scalar_one() == "revoked"

    heir = AuthenticatedIdentity(issuer, heir_subject, email="heir2@membership.invalid", email_verified=True)
    assert _client(database_url, heir).post(
        f"/v1/tenants/{tenant_id}/ownership-transfers/{transfer_id}/accept").status_code == 409
    assert _client(database_url, heir).post(
        f"/v1/tenants/{tenant_id}/invitations/{invitation.json()['invitation_id']}/accept").status_code == 409

    # The withdrawal is itself an audited transition, with its own reason.
    audit = owner.get(f"/v1/tenants/{tenant_id}/audit").json()["items"]
    withdrawn = [event for event in audit if event["action"] == "ownership.transfer_withdrawn"]
    assert len(withdrawn) == 1, [event["resource_id"] for event in withdrawn]
    assert withdrawn[0]["resource_id"] == transfer_id
    assert "no longer with the salon" in withdrawn[0]["reason"]
    deactivated = next(event for event in audit if event["action"] == "membership.deactivated")
    assert "withdrawn_pending_access" in deactivated["changed_fields"]

    # The owner is still an owner, and can now start a fresh transfer.
    assert owner.post(f"/v1/tenants/{tenant_id}/ownership-transfers",
                      json={"target_email": "next@membership.invalid"}).status_code == 201


def test_expired_invitation_and_transfer_release_their_single_pending_slot(salon):
    """A lapsed invitation or transfer must not block the owner forever."""
    database_url = os.environ["DATABASE_URL"]
    engine, tenant_id, location_id = salon["engine"], salon["tenant_id"], salon["location_id"]
    owner = _client(database_url, salon["owner"])

    invitation = owner.post(f"/v1/tenants/{tenant_id}/invitations",
                            json={"email": "lapsed@membership.invalid", "role": "stylist", "location_ids": [location_id]})
    assert invitation.status_code == 201
    invitation_id = invitation.json()["invitation_id"]
    transfer = owner.post(f"/v1/tenants/{tenant_id}/ownership-transfers",
                          json={"target_email": "lapsed@membership.invalid"})
    assert transfer.status_code == 201
    transfer_id = transfer.json()["transfer_id"]

    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        for table in ("tenant_invitations", "ownership_transfers"):
            connection.execute(text(f"""UPDATE {table} SET created_at=now()-interval '8 days',
                                                expires_at=now()-interval '1 minute' WHERE id=:id"""),
                               {"id": invitation_id if table == "tenant_invitations" else transfer_id})

    # Listing reports the truth rather than a dead 'pending' invitation with a Revoke button.
    listed = {row["id"]: row["state"] for row in owner.get(f"/v1/tenants/{tenant_id}/invitations").json()["items"]}
    assert listed[invitation_id] == "expired"

    # The email is re-invitable and the tenant can start a new transfer.
    again = owner.post(f"/v1/tenants/{tenant_id}/invitations",
                       json={"email": "lapsed@membership.invalid", "role": "manager", "location_ids": [location_id]})
    assert again.status_code == 201, again.text
    assert owner.post(f"/v1/tenants/{tenant_id}/ownership-transfers",
                      json={"target_email": "other@membership.invalid"}).status_code == 201


def test_concurrent_duplicate_requests_report_conflict_not_server_error(salon):
    """The partial unique indexes are the invariant; a lost race is a 409, not a 500."""
    database_url = os.environ["DATABASE_URL"]
    tenant_id, location_id = salon["tenant_id"], salon["location_id"]
    owner = _client(database_url, salon["owner"])
    body = {"email": "race@membership.invalid", "role": "stylist", "location_ids": [location_id]}

    def invite():
        return _client(database_url, salon["owner"]).post(f"/v1/tenants/{tenant_id}/invitations", json=body).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = sorted(pool.map(lambda _: invite(), range(6)))
    assert codes[0] == 201
    assert set(codes[1:]) == {409}, codes

    transfer_body = {"target_email": "race@membership.invalid"}

    def transfer():
        return _client(database_url, salon["owner"]).post(
            f"/v1/tenants/{tenant_id}/ownership-transfers", json=transfer_body).status_code

    with ThreadPoolExecutor(max_workers=6) as pool:
        codes = sorted(pool.map(lambda _: transfer(), range(6)))
    assert codes[0] == 201
    assert set(codes[1:]) == {409}, codes


def test_membership_routes_are_isolated_from_another_tenant(database_url):
    """Repo rule: tenant-scoped authorization must also be tested, not only implemented."""
    engine = create_engine(database_url, pool_pre_ping=True)
    issuer = "https://identity.example.invalid/isolation"
    outsider = AuthenticatedIdentity(issuer, str(uuid.uuid4()), email="outsider@membership.invalid", email_verified=True)
    # A verified, freshly re-authenticated owner, so the isolation assertions below are
    # not short-circuited by the endpoint's own pre-checks.
    owner = AuthenticatedIdentity(issuer, str(uuid.uuid4()), email="owner@membership.invalid",
                                  email_verified=True, auth_time=time.time())
    tenants = []

    def onboard(identity, name):
        app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
        app.dependency_overrides[require_identity] = lambda: identity
        response = TestClient(app).post("/v1/onboarding", json={
            "business_name": name, "location_name": "Main", "timezone": "America/Toronto"})
        assert response.status_code == 201
        return response.json()

    try:
        mine = onboard(owner, "Synthetic Isolation Mine")
        theirs = onboard(outsider, "Synthetic Isolation Theirs")
        tenants.extend([mine["tenant_id"], theirs["tenant_id"]])

        app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
        app.dependency_overrides[require_identity] = lambda: owner
        client = TestClient(app)
        foreign = theirs["tenant_id"]

        assert client.get(f"/v1/tenants/{foreign}/memberships").status_code == 404
        assert client.get(f"/v1/tenants/{foreign}/invitations").status_code == 404
        assert client.post(f"/v1/tenants/{foreign}/invitations", json={
            "email": "intruder@membership.invalid", "role": "stylist", "location_ids": [theirs["location_id"]]}).status_code == 404
        assert client.patch(f"/v1/tenants/{foreign}/memberships/{theirs['location_id']}",
                            json={"role": "stylist", "reason": "cross tenant write"}).status_code == 404
        assert client.request("DELETE", f"/v1/tenants/{foreign}/memberships/{theirs['location_id']}",
                              json={"reason": "cross tenant delete"}).status_code == 404
        assert client.post(f"/v1/tenants/{foreign}/ownership-transfers",
                           json={"target_email": "intruder@membership.invalid"}).status_code == 404
        assert client.post(f"/v1/tenants/{foreign}/ownership-transfers/{uuid.uuid4()}/accept").status_code == 404
        # Naming another tenant's location inside my own tenant is refused too.
        assert client.post(f"/v1/tenants/{mine['tenant_id']}/invitations", json={
            "email": "intruder@membership.invalid", "role": "stylist",
            "location_ids": [theirs["location_id"]]}).json()["detail"]["code"] == "location_not_found"
    finally:
        with engine.begin() as connection:
            for tenant_id in tenants:
                connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
                connection.execute(text("""UPDATE identity_users SET tenant_id=NULL,onboarding_fingerprint=NULL
                                           WHERE tenant_id=:tenant"""), {"tenant": tenant_id})
                connection.execute(text("DELETE FROM tenants WHERE id=:tenant"), {"tenant": tenant_id})
            connection.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            connection.execute(text("DELETE FROM identity_users WHERE issuer=:issuer"), {"issuer": issuer})
        engine.dispose()


def test_role_patch_reports_stored_locations_and_rejects_blank_reason(salon):
    """A role-only patch must not misreport untouched assignments; a blank reason is refused."""
    database_url = os.environ["DATABASE_URL"]
    engine, tenant_id, location_id = salon["engine"], salon["tenant_id"], salon["location_id"]
    owner = _client(database_url, salon["owner"])

    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"), {"tenant": tenant_id})
        member_id, _ = _verified_user(connection, salon["issuer"], "roles@membership.invalid")
        connection.execute(text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,'stylist')"),
                           {"tenant": tenant_id, "user": str(member_id)})
        connection.execute(text("""INSERT INTO membership_locations (tenant_id,user_id,location_id)
                                   VALUES (:tenant,:user,:location)"""),
                           {"tenant": tenant_id, "user": str(member_id), "location": location_id})

    # Only the role changes; the existing location assignment is untouched and reported truthfully.
    role_only = owner.patch(f"/v1/tenants/{tenant_id}/memberships/{member_id}",
                            json={"role": "receptionist", "reason": "now booking the front desk"})
    assert role_only.status_code == 200, role_only.text
    assert role_only.json() == {"user_id": str(member_id), "role": "receptionist", "location_ids": [location_id]}

    # A reason of only whitespace must be refused, not written as an audit row.
    assert owner.patch(f"/v1/tenants/{tenant_id}/memberships/{member_id}",
                       json={"role": "stylist", "reason": "   "}).status_code == 422
    assert owner.delete(f"/v1/tenants/{tenant_id}/memberships/{member_id}",
                        params={"reason": "   "}).status_code == 422
    assert owner.delete(f"/v1/tenants/{tenant_id}/memberships/{member_id}").status_code == 422

    # The deactivation reason travels in the body, so it stays out of a web-tier access log.
    assert owner.request("DELETE", f"/v1/tenants/{tenant_id}/memberships/{member_id}",
                         json={"reason": "moved to another salon"}).status_code == 204
    audit = owner.get(f"/v1/tenants/{tenant_id}/audit").json()["items"]
    assert next(e for e in audit if e["action"] == "membership.deactivated")["reason"] == "moved to another salon"
