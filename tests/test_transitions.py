"""Tests for appointment lifecycle transitions and completion visits (TASK-024, FR-031/034).

Builds on the calendar create seam, then walks the audited state machine: a legal
confirm→check_in→in_service→completed path, an illegal jump refused, completion creating
exactly one snapshotted visit, no_show releasing the reservation, a terminal correction
requiring a reason, and a stale version refused.
"""

import os
import uuid
from datetime import date, timedelta

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


def _transition(client, base, appt_id, to_status, version, reason=None):
    body = {"to_status": to_status, "expected_version": version,
            "idempotency_key": "t-" + uuid.uuid4().hex}
    if reason is not None:
        body["reason"] = reason
    return client.post(f"{base}/appointments/{appt_id}/transitions", json=body)


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
        assert visits == 1
        assert len(snap) == 1 and snap[0]["service_name"] == "Cut" and snap[0]["price_minor"] == 6000
        # 1 transition from the create (->confirmed) plus the 3 walk steps.
        assert transitions == 4
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
        appt_id, _day = _created(client, base, service, staff, client_id)
        noshow = _transition(client, base, appt_id, "no_show", 1)
        assert noshow.status_code == 200, noshow.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant["tenant_id"]})
            active = connection.execute(
                text("SELECT count(*) FROM reservations WHERE tenant_id=:t AND appointment_id=:a AND state='active'"),
                {"t": tenant["tenant_id"], "a": appt_id}).scalar_one()
        assert active == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_terminal_correction_requires_a_reason(database_url):
    """AC-2: correcting out of completed without a reason is a 422; with one it proceeds."""
    issuer = "https://identity.example.invalid/tr-4"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        base, slug, service, staff, client_id = _bookable(client, database_url, tenant)
        appt_id, _day = _created(client, base, service, staff, client_id)
        _transition(client, base, appt_id, "checked_in", 1)
        _transition(client, base, appt_id, "in_service", 2)
        _transition(client, base, appt_id, "completed", 3)
        # completed -> checked_in is a correction; without a reason it is refused.
        refused = _transition(client, base, appt_id, "checked_in", 4)
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["code"] == "correction_reason_required"
        # With a reason it is a compensating transition.
        fixed = _transition(client, base, appt_id, "checked_in", 4, reason="service not finished, reopened")
        assert fixed.status_code == 200, fixed.text
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
