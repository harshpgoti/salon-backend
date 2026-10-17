"""Actual calendar command retry receipts and atomic time facts (TASK-077)."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from test_clients import AuthenticatedIdentity, _client, _salon, _teardown
from test_memberships import _verified_user
from test_public_availability import _bookable_salon


@pytest.fixture
def calendar():
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL required for command regressions")
    issuer = "https://identity.example.invalid/calendar-command/" + uuid.uuid4().hex
    engine, owner, tenant = _salon(url, issuer)
    client = TestClient(_client(url, owner).app, raise_server_exceptions=False)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        day = date.today() + timedelta(days=1)
        _, service, staff = _bookable_salon(
            client, url, tenant, weekdays=range(7), shift_day=day.weekday())
        person = client.post(f"{base}/clients", json={"name": "Synthetic Retry Client"}).json()["id"]
        starts = datetime.combine(day, time(10), ZoneInfo("America/Toronto"))
        body = {"location_id": tenant["location_id"], "service_id": service, "staff_id": staff,
                "client_id": person, "starts_at": starts.isoformat(), "source": "staff",
                "idempotency_key": uuid.uuid4().hex}
        yield {"client": client, "base": base, "body": body, "engine": engine,
               "tenant": tenant, "starts": starts, "owner": owner, "url": url}
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_changed_manual_start_with_saved_key_is_conflict(calendar):
    client, base, body = calendar["client"], calendar["base"], calendar["body"]
    first = client.post(f"{base}/appointments", json=body)
    assert first.status_code == 201, first.text
    changed = {**body, "starts_at": (calendar["starts"] + timedelta(hours=2)).isoformat()}
    rejected = client.post(f"{base}/appointments", json=changed)
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["detail"]["code"] == "idempotency_conflict"
    listed = client.get(f"{base}/appointments").json()["items"]
    assert len(listed) == 1 and listed[0]["id"] == first.json()["appointment_id"]


def _created(calendar, **changes):
    body = {**calendar["body"], **changes}
    booked = calendar["client"].post(f"{calendar['base']}/appointments", json=body)
    assert booked.status_code == 201, booked.text
    return booked.json()["appointment_id"]


def test_cancel_retry_uses_original_saved_version_receipt(calendar):
    appointment = _created(calendar)
    path = f"{calendar['base']}/appointments/{appointment}/cancel"
    body = {"expected_version": 1, "reason": "Synthetic cancellation", "idempotency_key": uuid.uuid4().hex}
    first = calendar["client"].post(path, json=body)
    assert first.status_code == 200, first.text
    again = calendar["client"].post(path, json=body)
    assert again.status_code == 200, again.text
    assert again.json() == {**first.json(), "replayed": True}
    row = calendar["client"].get(f"{calendar['base']}/appointments").json()["items"][0]
    assert row["status"] == "cancelled" and row["version"] == 2


def test_cancel_changed_request_with_saved_key_is_conflict(calendar):
    appointment = _created(calendar)
    path = f"{calendar['base']}/appointments/{appointment}/cancel"
    body = {"expected_version": 1, "reason": "First reason", "idempotency_key": uuid.uuid4().hex}
    assert calendar["client"].post(path, json=body).status_code == 200
    changed = calendar["client"].post(path, json={**body, "expected_version": 2, "reason": "Changed reason"})
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["code"] == "idempotency_conflict"


def test_reschedule_retry_returns_saved_result_after_later_cancel(calendar):
    appointment = _created(calendar)
    path = f"{calendar['base']}/appointments/{appointment}/reschedule"
    body = {"starts_at": (calendar["starts"] + timedelta(hours=2)).isoformat(),
            "expected_version": 1, "idempotency_key": uuid.uuid4().hex}
    first = calendar["client"].post(path, json=body)
    assert first.status_code == 200, first.text
    cancelled = calendar["client"].post(f"{calendar['base']}/appointments/{appointment}/cancel", json={
        "expected_version": 2, "reason": "Later cancellation", "idempotency_key": uuid.uuid4().hex})
    assert cancelled.status_code == 200, cancelled.text
    replay = calendar["client"].post(path, json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json() == {**first.json(), "replayed": True}
    row = calendar["client"].get(f"{calendar['base']}/appointments").json()["items"][0]
    assert row["status"] == "cancelled" and row["version"] == 3


def test_moved_frozen_service_times_and_reservations_are_aligned(calendar):
    service_path = f"{calendar['base']}/services/{calendar['body']['service_id']}"
    patch = calendar["client"].patch(service_path, json={
        "expected_version": 1, "pre_buffer_minutes": 5, "post_buffer_minutes": 10})
    assert patch.status_code == 200, patch.text
    appointment = _created(calendar)
    edited = calendar["client"].patch(service_path, json={
        "expected_version": 2, "name": "Changed catalogue", "duration_minutes": 45,
        "price_minor": 7500, "pre_buffer_minutes": 0, "post_buffer_minutes": 0})
    assert edited.status_code == 200, edited.text
    new_start = calendar["starts"] + timedelta(hours=2)
    moved = calendar["client"].post(f"{calendar['base']}/appointments/{appointment}/reschedule", json={
        "starts_at": new_start.isoformat(), "expected_version": 1, "idempotency_key": uuid.uuid4().hex})
    assert moved.status_code == 200, moved.text
    row = calendar["client"].get(f"{calendar['base']}/appointments").json()["items"][0]
    expected_start = new_start.astimezone(timezone.utc)
    assert datetime.fromisoformat(row["starts_at"]) == expected_start
    assert datetime.fromisoformat(row["ends_at"]) == expected_start + timedelta(hours=1)
    assert (row["service_name"], row["price_minor"], row["service_version"]) == ("Cut", 6000, 2)
    with calendar["engine"].begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                           {"t": calendar["tenant"]["tenant_id"]})
        line = connection.execute(text("""SELECT starts_at,ends_at,duration_minutes,pre_buffer_minutes,post_buffer_minutes
            FROM appointment_services WHERE tenant_id=:t AND appointment_id=:a"""),
            {"t": calendar["tenant"]["tenant_id"], "a": appointment}).mappings().one()
        reservation = connection.execute(text("""SELECT lower(occupied_range),upper(occupied_range)
            FROM reservations WHERE tenant_id=:t AND appointment_id=:a AND state='active'"""),
            {"t": calendar["tenant"]["tenant_id"], "a": appointment}).one()
    assert line["starts_at"] == expected_start
    assert line["ends_at"] == expected_start + timedelta(hours=1)
    assert (line["duration_minutes"], line["pre_buffer_minutes"], line["post_buffer_minutes"]) == (60, 5, 10)
    assert reservation == (expected_start - timedelta(minutes=5), expected_start + timedelta(minutes=70))


@pytest.mark.parametrize("action,state", [
    ("cancel", "in_service"), ("cancel", "completed"), ("cancel", "no_show"),
    ("reschedule", "in_service"),
])
def test_calendar_commands_cannot_bypass_lifecycle(calendar, action, state):
    appointment = _created(calendar)
    client, base = calendar["client"], calendar["base"]
    path = f"{base}/appointments/{appointment}"
    version = 1
    steps = ["no_show"] if state == "no_show" else ["checked_in", "in_service"]
    if state == "completed":
        steps.append("completed")
    for target in steps:
        body = {"to_status": target, "expected_version": version,
                "idempotency_key": uuid.uuid4().hex}
        if target in {"rejected", "cancelled", "no_show"}:
            body["reason"] = "Synthetic lifecycle regression"
        changed = client.post(path + "/transitions", json=body)
        assert changed.status_code == 200, changed.text
        version += 1
    body = {"expected_version": version, "idempotency_key": uuid.uuid4().hex}
    if action == "reschedule":
        body["starts_at"] = (calendar["starts"] + timedelta(hours=2)).isoformat()
    else:
        body["reason"] = "Cannot bypass lifecycle"
    refused = client.post(path + "/" + action, json=body)
    assert refused.status_code == 409, refused.text
    row = client.get(f"{base}/appointments").json()["items"][0]
    assert row["status"] == state and row["version"] == version


@pytest.mark.parametrize("field,value", [
    ("source", "walk_in"), ("client_id", None), ("service_id", None),
    ("staff_id", None), ("location_id", None), ("attribution_token", "changed-synthetic"),
])
def test_each_manual_request_fact_is_bound_to_saved_key(calendar, field, value):
    _created(calendar)
    value = value if value is not None else str(uuid.uuid4())
    changed = calendar["client"].post(f"{calendar['base']}/appointments",
                                    json={**calendar["body"], field: value})
    assert changed.status_code == 409, changed.text
    assert changed.json()["detail"]["code"] == "idempotency_conflict"


@pytest.mark.parametrize("action", ["create", "cancel", "reschedule"])
def test_concurrent_command_retries_commit_one_result_and_audit(calendar, action):
    client, base = calendar["client"], calendar["base"]
    if action == "create":
        path, body, version, audit = f"{base}/appointments", calendar["body"], 1, "appointment.created"
    else:
        appointment = _created(calendar)
        path = f"{base}/appointments/{appointment}/{action}"
        body = {"expected_version": 1, "idempotency_key": uuid.uuid4().hex}
        if action == "reschedule":
            body["starts_at"] = (calendar["starts"] + timedelta(hours=2)).isoformat()
        else:
            body["reason"] = "Synthetic concurrent cancel"
        version, audit = 2, f"appointment.{action}led" if action == "cancel" else "appointment.rescheduled"
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: client.post(path, json=body), range(4)))
    expected_status = 201 if action == "create" else 200
    assert all(r.status_code == expected_status for r in responses), [r.text for r in responses]
    assert len({r.json()["appointment_id"] for r in responses}) == 1
    assert sum(not r.json()["replayed"] for r in responses) == 1
    rows = client.get(f"{base}/appointments").json()["items"]
    assert len(rows) == 1 and rows[0]["version"] == version
    with calendar["engine"].begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                           {"t": calendar["tenant"]["tenant_id"]})
        count = connection.execute(text("""SELECT count(*) FROM tenant_audit_events
            WHERE tenant_id=:t AND action=:action AND resource_id=:a"""),
            {"t": calendar["tenant"]["tenant_id"], "action": audit, "a": rows[0]["id"]}).scalar_one()
    assert count == 1


def test_failed_reschedule_keeps_all_facts_and_key_can_retry_after_release(calendar):
    first = _created(calendar)
    taken_start = calendar["starts"] + timedelta(hours=2)
    second = _created(calendar, starts_at=taken_start.isoformat(), idempotency_key=uuid.uuid4().hex)
    path = f"{calendar['base']}/appointments/{first}/reschedule"
    body = {"starts_at": taken_start.isoformat(), "expected_version": 1, "idempotency_key": uuid.uuid4().hex}
    failed = calendar["client"].post(path, json=body)
    assert failed.status_code == 409, failed.text
    original = next(row for row in calendar["client"].get(f"{calendar['base']}/appointments").json()["items"]
                    if row["id"] == first)
    assert datetime.fromisoformat(original["starts_at"]) == calendar["starts"]
    assert original["version"] == 1
    freed = calendar["client"].post(f"{calendar['base']}/appointments/{second}/cancel", json={
        "expected_version": 1, "reason": "Free test slot", "idempotency_key": uuid.uuid4().hex})
    assert freed.status_code == 200, freed.text
    retried = calendar["client"].post(path, json=body)
    assert retried.status_code == 200 and retried.json()["replayed"] is False, retried.text


def test_key_cannot_be_reused_for_other_action_or_target(calendar):
    first = _created(calendar)
    other = _created(calendar, starts_at=(calendar["starts"] + timedelta(hours=2)).isoformat(),
                     idempotency_key=uuid.uuid4().hex)
    client, base = calendar["client"], calendar["base"]
    wrong_action = client.post(f"{base}/appointments/{first}/cancel", json={
        "expected_version": 1, "idempotency_key": calendar["body"]["idempotency_key"]})
    assert wrong_action.status_code == 409 and wrong_action.json()["detail"]["code"] == "idempotency_conflict"
    body = {"expected_version": 1, "reason": "First target", "idempotency_key": uuid.uuid4().hex}
    assert client.post(f"{base}/appointments/{first}/cancel", json=body).status_code == 200
    wrong_target = client.post(f"{base}/appointments/{other}/cancel", json=body)
    assert wrong_target.status_code == 409 and wrong_target.json()["detail"]["code"] == "idempotency_conflict"


def test_current_authorization_and_original_actor_are_checked_before_replay(calendar):
    _created(calendar)
    with calendar["engine"].begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                           {"t": calendar["tenant"]["tenant_id"]})
        manager_id, subject = _verified_user(connection, calendar["owner"].issuer, "synthetic-manager@example.invalid")
        connection.execute(text("INSERT INTO memberships(tenant_id,user_id,role) VALUES(:t,:u,'manager')"),
                           {"t": calendar["tenant"]["tenant_id"], "u": str(manager_id)})
        connection.execute(text("INSERT INTO membership_locations(tenant_id,user_id,location_id) VALUES(:t,:u,:l)"),
                           {"t": calendar["tenant"]["tenant_id"], "u": str(manager_id),
                            "l": calendar["tenant"]["location_id"]})
    identity = AuthenticatedIdentity(calendar["owner"].issuer, subject,
                                     email="synthetic-manager@example.invalid", email_verified=True)
    manager = _client(calendar["url"], identity)
    path = f"{calendar['base']}/appointments"
    actor_conflict = manager.post(path, json=calendar["body"])
    assert actor_conflict.status_code == 409
    assert actor_conflict.json()["detail"]["code"] == "idempotency_conflict"
    own_body = {**calendar["body"], "starts_at": (calendar["starts"] + timedelta(hours=4)).isoformat(),
                "idempotency_key": uuid.uuid4().hex}
    assert manager.post(path, json=own_body).status_code == 201
    revoked = calendar["client"].delete(f"{calendar['base']}/memberships/{manager_id}",
                                        params={"reason": "Synthetic revocation"})
    assert revoked.status_code == 204, revoked.text
    denied = manager.post(path, json=own_body)
    assert denied.status_code == 404
    assert denied.json()["detail"]["code"] == "tenant_not_found"
    foreign = calendar["client"].post(f"/v1/tenants/{uuid.uuid4()}/appointments", json=calendar["body"])
    assert foreign.status_code in (403, 404)


def test_equivalent_absolute_start_representation_replays_same_receipt(calendar):
    first = _created(calendar)
    body = {**calendar["body"], "starts_at": calendar["starts"].astimezone(timezone.utc).isoformat().replace("+00:00", "Z")}
    replay = calendar["client"].post(f"{calendar['base']}/appointments", json=body)
    assert replay.status_code == 201, replay.text
    assert replay.json() == {"appointment_id": first, "replayed": True}


def test_legacy_key_without_original_request_cannot_invent_a_receipt(calendar):
    appointment = _created(calendar)
    legacy_key = uuid.uuid4().hex
    with calendar["engine"].begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                           {"t": calendar["tenant"]["tenant_id"]})
        connection.execute(text("UPDATE appointments SET idempotency_key=:key WHERE tenant_id=:t AND id=:a"),
                           {"key": legacy_key, "t": calendar["tenant"]["tenant_id"], "a": appointment})
    response = calendar["client"].post(f"{calendar['base']}/appointments",
                                       json={**calendar["body"], "idempotency_key": legacy_key})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "legacy_idempotency_key"


def test_two_authorized_salons_have_independent_key_namespaces(calendar):
    original = _created(calendar)
    other_issuer = "https://identity.example.invalid/calendar-namespace/" + uuid.uuid4().hex
    other_engine, other_owner, other_tenant = _salon(calendar["url"], other_issuer)
    other_client = _client(calendar["url"], other_owner)
    other_base = f"/v1/tenants/{other_tenant['tenant_id']}"
    try:
        _, service, staff = _bookable_salon(other_client, calendar["url"], other_tenant,
            weekdays=range(7), shift_day=calendar["starts"].weekday())
        person = other_client.post(f"{other_base}/clients", json={"name": "Other Synthetic Salon Client"}).json()["id"]
        with other_engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": other_tenant["tenant_id"]})
            user = connection.execute(text("SELECT id FROM identity_users WHERE issuer=:issuer AND subject=:subject"),
                {"issuer": calendar["owner"].issuer, "subject": calendar["owner"].subject}).scalar_one()
            connection.execute(text("INSERT INTO memberships(tenant_id,user_id,role) VALUES(:t,:u,'manager')"),
                               {"t": other_tenant["tenant_id"], "u": user})
            connection.execute(text("INSERT INTO membership_locations(tenant_id,user_id,location_id) VALUES(:t,:u,:l)"),
                               {"t": other_tenant["tenant_id"], "u": user,
                                "l": other_tenant["location_id"]})
        body = {**calendar["body"], "location_id": other_tenant["location_id"],
                "service_id": service, "staff_id": staff, "client_id": person}
        second = calendar["client"].post(f"{other_base}/appointments", json=body)
        assert second.status_code == 201 and second.json()["replayed"] is False, second.text
        assert second.json()["appointment_id"] != original
        first_replay = calendar["client"].post(f"{calendar['base']}/appointments", json=calendar["body"])
        assert first_replay.status_code == 201 and first_replay.json()["appointment_id"] == original
        assert calendar["client"].get(f"{other_base}/appointments").json()["total"] == 1
        assert calendar["client"].get(f"{calendar['base']}/appointments").json()["total"] == 1
    finally:
        _teardown(other_engine, [other_tenant["tenant_id"]], other_issuer)
