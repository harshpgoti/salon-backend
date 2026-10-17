"""Tests for appointment lifecycle transitions and completion visits (TASK-024, FR-031/034).

Builds on the calendar create seam, then walks the audited state machine: a legal
confirm→check_in→in_service→completed path, an illegal jump refused, completion creating
exactly one snapshotted visit, no_show releasing the reservation, a terminal correction
requiring a reason, and a stale version refused.
"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from threading import Barrier

import pytest
from sqlalchemy import text
from test_clients import _salon, _client, _teardown
from test_calendar import _bookable, _next_weekday_slot, _book


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _created(client, base, service, staff, client_id):
    day = _next_weekday_slot()
    created = _book(client, base, service, staff, client_id, f"{day.isoformat()}T14:00:00+00:00")
    assert created.status_code == 201, created.text
    return created.json()["appointment_id"], day


def _transition(client, base, appt_id, to_status, version, reason=None, key=None):
    body = {"to_status": to_status, "expected_version": version,
            "idempotency_key": key or "t-" + uuid.uuid4().hex}
    if reason is not None:
        body["reason"] = reason
    return client.post(f"{base}/appointments/{appt_id}/transitions", json=body)


def _set_rebooking_interval(client, base, service, days=42):
    changed = client.patch(f"{base}/services/{service}", json={
        "rebooking_interval_days": days, "expected_version": 1})
    assert changed.status_code == 200, changed.text


def test_full_lifecycle_walk_completes_and_creates_a_visit(database_url):
    """AC-1/AC-2: confirmed→checked_in→in_service→completed, each audited, one visit."""
    issuer = "https://identity.example.invalid/tr-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        appt_id, _day = _created(client, base, service, staff, client_id)
        assert _transition(client, base, appt_id, "checked_in", 1).status_code == 200
        assert _transition(client, base, appt_id, "in_service", 2).status_code == 200
        done = _transition(client, base, appt_id, "completed", 3)
        assert done.status_code == 200, done.text
        visit_id = done.json()["visit_id"]
        assert visit_id
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            visits = connection.execute(
                text("SELECT count(*) FROM visits WHERE tenant_id=:t AND appointment_id=:a"),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
            snap = connection.execute(
                text("""SELECT service_name, price_minor FROM visit_services
                        WHERE tenant_id=:t AND visit_id=:v"""),
                {"t": tenant["tenant_id"], "v": visit_id}).mappings().all()
            transitions = connection.execute(
                text("SELECT count(*) FROM appointment_transitions WHERE tenant_id=:t AND appointment_id=:a"),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
            audit_count = connection.execute(text(
                "SELECT count(*) FROM tenant_audit_events WHERE tenant_id=:t AND resource_id=:a "
                "AND action IN ('appointment.checked_in','appointment.in_service','appointment.completed')"),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
        assert visits == 1
        assert len(snap) == 1 and snap[0]["service_name"] == "Cut" and snap[0]["price_minor"] == 6000
        # 1 transition from the create (->confirmed) plus the 3 walk steps.
        assert transitions == 4
        assert audit_count == 3
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_illegal_jump_is_refused(database_url):
    """AC-1: confirmed→completed skips a step and is a 409, never a silent write."""
    issuer = "https://identity.example.invalid/tr-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        appt_id, _day = _created(client, base, service, staff, client_id)
        bad = _transition(client, base, appt_id, "completed", 1)
        assert bad.status_code == 409, bad.text
        assert bad.json()["detail"]["code"] == "illegal_transition"
        unknown = _transition(client, base, appt_id, "rewrite_any_status", 1)
        assert unknown.status_code == 422, unknown.text
        # Status unchanged.
        listing = client.get(f"{base}/appointments")
        row = next(i for i in listing.json()["items"] if i["id"] == appt_id)
        assert row["status"] == "confirmed"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_no_show_releases_the_reservation(database_url):
    """AC-1: no_show is a legal terminal move that frees the slot for rebooking."""
    issuer = "https://identity.example.invalid/tr-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        _set_rebooking_interval(client, base, service)
        appt_id, _day = _created(client, base, service, staff, client_id)
        missing_reason = _transition(client, base, appt_id, "no_show", 1)
        assert missing_reason.status_code == 422, missing_reason.text
        assert missing_reason.json()["detail"]["code"] == "appointment_reason_required"
        blank_reason = _transition(client, base, appt_id, "no_show", 1, reason="   ")
        assert blank_reason.status_code == 422, blank_reason.text
        noshow = _transition(client, base, appt_id, "no_show", 1, reason="Client did not arrive")
        assert noshow.status_code == 200, noshow.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            active = connection.execute(
                text("SELECT count(*) FROM reservations WHERE tenant_id=:t AND appointment_id=:a AND state='active'"),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
            visits = connection.execute(
                text("SELECT count(*) FROM visits WHERE tenant_id=:t AND appointment_id=:a"),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
            cycles = connection.execute(
                text("SELECT count(*) FROM rebooking_cycles WHERE tenant_id=:t AND source_appointment_id=:a"),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
        assert active == 0
        assert visits == cycles == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_completion_snapshots_override_due_date_and_supersedes_older_cycle(database_url):
    """AC-1/AC-3: local completion date and current override snapshot each cycle."""
    issuer = "https://identity.example.invalid/tr-rebooking-snapshot"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, _slug, service, staff, client_id = _bookable(client, database_url, tenant)
        _set_rebooking_interval(client, base, service, 42)
        override = client.post(f"/v1/tenants/{tid}/rebooking-overrides", json={
            "client_id": client_id, "service_id": service, "interval_days": 21})
        assert override.status_code == 201, override.text
        first_appt, day = _created(client, base, service, staff, client_id)
        for status, version in (("checked_in", 1), ("in_service", 2), ("completed", 3)):
            result = _transition(client, base, first_appt, status, version)
            assert result.status_code == 200, result.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            first = connection.execute(text("""SELECT r.interval_days, r.due_date,
                    r.service_version, (v.completed_at AT TIME ZONE a.local_timezone)::date AS local_day
                FROM rebooking_cycles r JOIN visits v ON v.tenant_id=r.tenant_id AND v.id=r.visit_id
                JOIN appointments a ON a.tenant_id=v.tenant_id AND a.id=v.appointment_id
                WHERE r.tenant_id=:t AND r.source_appointment_id=:a"""),
                {"t": tid, "a": first_appt}).mappings().one()
        assert first["interval_days"] == 21
        assert first["due_date"] == first["local_day"] + timedelta(days=21)
        # Changing both rules affects only subsequent service completion.
        override = client.post(f"/v1/tenants/{tid}/rebooking-overrides", json={
            "client_id": client_id, "service_id": service, "interval_days": 30})
        assert override.status_code == 201
        later = (day + timedelta(days=7)).isoformat() + "T14:00:00+00:00"
        second_created = _book(client, base, service, staff, client_id, later)
        assert second_created.status_code == 201, second_created.text
        second_appt = second_created.json()["appointment_id"]
        for status, version in (("checked_in", 1), ("in_service", 2), ("completed", 3)):
            result = _transition(client, base, second_appt, status, version)
            assert result.status_code == 200, result.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            cycles = connection.execute(text("""SELECT status, interval_days, due_date,
                    source_appointment_id::text AS appointment_id FROM rebooking_cycles
                WHERE tenant_id=:t ORDER BY created_at, id"""), {"t": tid}).mappings().all()
        assert [(r["status"], r["interval_days"]) for r in cycles] == [
            ("superseded", 21), ("due", 30)]
        assert cycles[0]["due_date"] == first["due_date"]
        assert cycles[1]["appointment_id"] == second_appt
    finally:
        _teardown(engine, [tid], issuer)


def test_concurrent_completions_for_same_client_both_create_cycles(database_url):
    """AC-3: concurrent completions serialize cycle supersession, not fail uniqueness."""
    issuer = "https://identity.example.invalid/tr-rebooking-concurrent"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    other_client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, _slug, service, staff, client_id = _bookable(client, database_url, tenant)
        _set_rebooking_interval(client, base, service)
        first_day = _next_weekday_slot()
        first = _book(client, base, service, staff, client_id,
                      f"{first_day.isoformat()}T14:00:00+00:00")
        second_day = first_day + timedelta(days=7)
        second = _book(client, base, service, staff, client_id,
                       f"{second_day.isoformat()}T14:00:00+00:00")
        assert first.status_code == second.status_code == 201, first.text + second.text
        appointment_ids = [first.json()["appointment_id"], second.json()["appointment_id"]]
        for appointment_id in appointment_ids:
            assert _transition(client, base, appointment_id, "checked_in", 1).status_code == 200
            assert _transition(client, base, appointment_id, "in_service", 2).status_code == 200

        start = Barrier(2)
        def complete(test_client, appointment_id):
            start.wait(timeout=10)
            return _transition(test_client, base, appointment_id, "completed", 3)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(complete, test_client, appointment_id)
                       for test_client, appointment_id in zip(
                           (client, other_client), appointment_ids)]
            results = [future.result(timeout=30) for future in futures]
        assert [result.status_code for result in results] == [200, 200], [r.text for r in results]
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
            cycles = connection.execute(text("""SELECT status FROM rebooking_cycles
                WHERE tenant_id=:t AND client_id=:client AND service_id=:service
                ORDER BY created_at, id"""),
                {"t": tid, "client": client_id, "service": service}).scalars().all()
        assert sorted(cycles) == ["due", "superseded"]
    finally:
        _teardown(engine, [tid], issuer)


def test_rejection_requires_a_reason_and_records_it(database_url):
    """Pending approvals cannot be rejected without a human-readable reason."""
    issuer = "https://identity.example.invalid/tr-reject"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, _slug, service, staff, client_id = _bookable(client, database_url, tenant)
        appt_id, _day = _created(client, base, service, staff, client_id)
        # The optional approval policy has no creation route yet; fixture its state so
        # this test can exercise the otherwise unreachable legal transition.
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            connection.execute(text(
                "UPDATE appointments SET status='pending_approval' WHERE tenant_id=:tenant AND id=:id"),
                {"tenant": tenant["tenant_id"], "id": appt_id})
        missing = _transition(client, base, appt_id, "rejected", 1)
        blank = _transition(client, base, appt_id, "rejected", 1, reason="  ")
        assert missing.status_code == blank.status_code == 422
        assert missing.json()["detail"]["code"] == blank.json()["detail"]["code"] == "appointment_reason_required"
        rejected = _transition(client, base, appt_id, "rejected", 1, reason="No longer available")
        assert rejected.status_code == 200, rejected.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            transition_reason = connection.execute(text(
                "SELECT reason FROM appointment_transitions WHERE tenant_id=:tenant AND appointment_id=:id "
                "AND to_status='rejected'"), {"tenant": tenant["tenant_id"], "id": appt_id}).scalar_one()
        assert transition_reason == "No longer available"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_terminal_correction_requires_reason_and_owner(database_url):
    """AC-2: terminal corrections need a reason and owner authority."""
    issuer = "https://identity.example.invalid/tr-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        _set_rebooking_interval(client, base, service)
        appt_id, _day = _created(client, base, service, staff, client_id)
        _transition(client, base, appt_id, "checked_in", 1)
        _transition(client, base, appt_id, "in_service", 2)
        _transition(client, base, appt_id, "completed", 3)
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            assert connection.execute(text("SELECT count(*) FROM rebooking_cycles "
                "WHERE tenant_id=:tenant AND source_appointment_id=:id"),
                {"tenant": tenant["tenant_id"], "id": appt_id}).scalar_one() == 1
        # completed -> checked_in is a correction; without a reason it is refused.
        refused = _transition(client, base, appt_id, "checked_in", 4)
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["code"] == "correction_reason_required"
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            user_id = connection.execute(text(
                "SELECT m.user_id::text FROM memberships m JOIN identity_users u ON u.id=m.user_id "
                "WHERE m.tenant_id=:tenant AND u.issuer=:issuer AND u.subject=:subject"),
                {"tenant": tenant["tenant_id"], "issuer": owner.issuer, "subject": owner.subject}).scalar_one()
            connection.execute(text("UPDATE memberships SET role='manager' WHERE tenant_id=:tenant"),
                               {"tenant": tenant["tenant_id"]})
            connection.execute(text(
                "INSERT INTO membership_locations(tenant_id,user_id,location_id) VALUES (:tenant,:user,:location)"),
                {"tenant": tenant["tenant_id"], "user": user_id, "location": tenant["location_id"]})
        denied = _transition(client, base, appt_id, "checked_in", 4, reason="reopen correction")
        assert denied.status_code == 403, denied.text
        assert denied.json()["detail"]["code"] == "owner_correction_required"
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            connection.execute(text("DELETE FROM membership_locations WHERE tenant_id=:tenant AND user_id=:user"),
                               {"tenant": tenant["tenant_id"], "user": user_id})
            connection.execute(text("UPDATE memberships SET role='owner' WHERE tenant_id=:tenant"),
                               {"tenant": tenant["tenant_id"]})
        # With a reason, owner authority, and current version it proceeds.
        fixed = _transition(client, base, appt_id, "checked_in", 4, reason="service not finished, reopened")
        assert fixed.status_code == 200, fixed.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            assert connection.execute(text("SELECT count(*) FROM visits "
                "WHERE tenant_id=:tenant AND appointment_id=:id"),
                {"tenant": tenant["tenant_id"], "id": appt_id}).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM rebooking_cycles "
                "WHERE tenant_id=:tenant AND source_appointment_id=:id"),
                {"tenant": tenant["tenant_id"], "id": appt_id}).scalar_one() == 0
        assert _transition(client, base, appt_id, "in_service", 5).status_code == 200
        recommpleted = _transition(client, base, appt_id, "completed", 6)
        assert recommpleted.status_code == 200, recommpleted.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            assert connection.execute(text("SELECT count(*) FROM rebooking_cycles "
                "WHERE tenant_id=:tenant AND source_appointment_id=:id AND status='due'"),
                {"tenant": tenant["tenant_id"], "id": appt_id}).scalar_one() == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_stale_version_cannot_transition(database_url):
    """AC-1: a stale expected_version is refused so a transition cannot clobber a change."""
    issuer = "https://identity.example.invalid/tr-5"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        appt_id, _day = _created(client, base, service, staff, client_id)
        stale = _transition(client, base, appt_id, "checked_in", 99)
        assert stale.status_code == 409, stale.text
        assert stale.json()["detail"]["code"] == "appointment_version_conflict"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_repeated_completion_returns_the_same_visit(database_url):
    """AC-2: a replayed completion is idempotent — one visit, not two."""
    issuer = "https://identity.example.invalid/tr-6"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        appt_id, _day = _created(client, base, service, staff, client_id)
        _transition(client, base, appt_id, "checked_in", 1)
        _transition(client, base, appt_id, "in_service", 2)
        first = _transition(client, base, appt_id, "completed", 3)
        # Same-target transition is a harmless replay returning the same visit.
        replay = _transition(client, base, appt_id, "completed", 4)
        assert replay.status_code == 200, replay.text
        assert replay.json()["replayed"] is True
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            visits = connection.execute(
                text("SELECT count(*) FROM visits WHERE tenant_id=:t AND appointment_id=:a"),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
        assert visits == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_transition_retry_replays_saved_response_and_binds_key_to_facts(database_url):
    """A lost response can be retried once without duplicating the transition or audit."""
    issuer = "https://identity.example.invalid/tr-retry"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, _slug, service, staff, client_id = _bookable(client, database_url, tenant)
        appt_id, _day = _created(client, base, service, staff, client_id)
        key = "transition-retry-001"
        first = _transition(client, base, appt_id, "checked_in", 1, key=key)
        replay = _transition(client, base, appt_id, "checked_in", 1, key=key)
        assert first.status_code == replay.status_code == 200
        assert first.json()["replayed"] is False
        assert replay.json()["replayed"] is True
        conflict = _transition(client, base, appt_id, "no_show", 1, reason="missed", key=key)
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["detail"]["code"] == "idempotency_conflict"
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            row = connection.execute(text(
                "SELECT status,version FROM appointments WHERE tenant_id=:tenant AND id=:id"),
                {"tenant": tenant["tenant_id"], "id": appt_id}).one()
            count = connection.execute(text(
                "SELECT count(*) FROM appointment_transitions WHERE tenant_id=:tenant AND appointment_id=:id"),
                {"tenant": tenant["tenant_id"], "id": appt_id}).scalar_one()
        assert (row.status, row.version, count) == ("checked_in", 2, 2)
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_cross_tenant_cannot_transition(database_url):
    """AC-1/QA-01: another tenant's operator gets 404 on this tenant's appointment."""
    issuer_a = "https://identity.example.invalid/tr-7a"
    engine_a, owner_a, tenant_a = _salon(database_url, issuer_a)
    client_a = _client(database_url, owner_a)
    issuer_b = "https://identity.example.invalid/tr-7b"
    engine_b, owner_b, tenant_b = _salon(database_url, issuer_b)
    client_b = _client(database_url, owner_b)
    try:
        base_a, slug, service, staff, client_id = _bookable(client_a, database_url, tenant_a)
        appt_id, _day = _created(client_a, base_a, service, staff, client_id)
        denied = client_b.post(f"/v1/tenants/{tenant_a['tenant_id']}/appointments/{appt_id}/transitions",
                               json={"to_status": "checked_in", "expected_version": 1,
                                     "idempotency_key": "t-" + uuid.uuid4().hex})
        assert denied.status_code in (403, 404), denied.text
    finally:
        _teardown(engine_a, [tenant_a["tenant_id"]], issuer_a)
        _teardown(engine_b, [tenant_b["tenant_id"]], issuer_b)
