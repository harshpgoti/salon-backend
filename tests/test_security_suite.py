"""TASK-056 security suite (FR-082 slice, synthetic-only).

Honesty limits: everything here runs against a throwaway Postgres tenant with
synthetic identities. The concurrency tests prove single-winner/idempotency
invariants under thread races, not under production load or adversarial
timing. The load smoke asserts bounded latency (<10s) on a seeded tenant as a
regression tripwire only — it is NOT a capacity claim.

Conventions: unique issuers under https://identity.example.invalid/sec56-*;
every test wraps in try/finally _teardown (helpers reused from test_clients).
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from modules.communications.consent import check_suppressed, record_suppression
from modules.communications.outbox import (
    dispatch_due_communications,
    record_booking_communication,
)
from test_booking_holds import _bookable, _first_slot
from test_clients import _client, _salon, _teardown
from test_communications import (
    FailingAdapter,
    RecordingAdapter,
    _booked_appointment,
    _client_email,
)


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _add_member(engine, tenant_id: str, identity, role: str) -> None:
    """Grant an existing-issuer identity a role on a tenant (test_permissions pattern)."""
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant_id})
        user_id = connection.execute(
            text("INSERT INTO identity_users (issuer,subject) VALUES (:issuer,:subject) RETURNING id"),
            {"issuer": identity.issuer, "subject": identity.subject},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:tenant,:user,:role)"),
            {"tenant": tenant_id, "user": str(user_id), "role": role},
        )


# 1. Cross-tenant denial --------------------------------------------------------

def test_sec56_cross_tenant_data_unreachable(database_url):
    """A session from tenant B sees none of tenant A's data (404/empty, never rows)."""
    from modules.identity.main import AuthenticatedIdentity

    issuer_a = "https://identity.example.invalid/sec56-cross-a"
    issuer_b = "https://identity.example.invalid/sec56-cross-b"
    engine_a, owner_a, tenant_a = _salon(database_url, issuer_a)
    engine_b, owner_b, tenant_b = _salon(database_url, issuer_b)
    client_a = _client(database_url, owner_a)
    client_b = _client(database_url, owner_b)
    try:
        tid_a, tid_b = tenant_a["tenant_id"], tenant_b["tenant_id"]
        # Seed tenant A with a client + appointment + communication + suppression.
        base_a = f"/v1/tenants/{tid_a}"
        client_row = client_a.post(f"{base_a}/clients", json={"name": "Tenant A Client"}).json()
        _, _, _, comm_client, appt_a, _ = _booked_appointment(client_a, tenant_a)
        email_a = _client_email(engine_a, tid_a, comm_client)
        with engine_a.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid_a})
            record_booking_communication(
                connection=conn, tenant_id=tid_a, appointment_id=appt_a,
                client_id=comm_client, contact_value=email_a, channel="email",
                purpose="booking_confirmation", entity_version=1)
            record_suppression(
                conn, tenant_id=tid_a, client_id=comm_client,
                contact_value=email_a, channel="email", reason="stop")

        # HTTP surfaces: B's owner probing A's tenant gets 404 tenant_not_found.
        probes = [
            client_b.get(f"/v1/tenants/{tid_a}/clients/{client_row['id']}"),
            client_b.get(f"/v1/tenants/{tid_a}/appointments", params={"limit": 5}),
            client_b.get(f"/v1/tenants/{tid_a}/locations"),
            client_b.get(f"/v1/tenants/{tid_a}/reports/operational",
                         params={"date_from": date.today().isoformat(),
                                 "date_to": date.today().isoformat()}),
        ]
        for resp in probes:
            assert resp.status_code == 404, resp.text
            assert resp.json()["detail"]["code"] == "tenant_not_found", resp.text

        # RLS level: scoped as B, A's comms/consents/suppressions rows are invisible.
        with engine_b.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid_b})
            for table in ("communications", "suppressions", "consents"):
                leaked = conn.execute(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id=:other"),
                    {"other": tid_a}).scalar_one()
                assert leaked == 0, f"{table} leaked across tenants"
            own_comms = conn.execute(text("SELECT count(*) FROM communications")).scalar_one()
            assert own_comms == 0

        # And A's data is intact under its own scope.
        with engine_a.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid_a})
            own_booking_comms = conn.execute(text("""SELECT count(*) FROM communications
                WHERE tenant_id=:t AND source_type='appointment' AND source_id=:appointment"""),
                {"t": tid_a, "appointment": appt_a}).scalar_one()
            assert own_booking_comms >= 1
            assert conn.execute(text("SELECT count(*) FROM suppressions")).scalar_one() == 1
    finally:
        _teardown(engine_a, [tenant_a["tenant_id"]], issuer_a)
        _teardown(engine_b, [tenant_b["tenant_id"]], issuer_b)


# 2. Role denial matrix ---------------------------------------------------------

def test_sec56_role_denial_matrix(database_url):
    """Analyst/stylist cannot reach owner/manager endpoints (403/404)."""
    from modules.identity.main import AuthenticatedIdentity

    issuer = "https://identity.example.invalid/sec56-roles"
    engine, owner, tenant = _salon(database_url, issuer)
    owner_client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    base = f"/v1/tenants/{tid}"
    try:
        analyst = AuthenticatedIdentity(issuer, str(uuid.uuid4()))
        stylist = AuthenticatedIdentity(issuer, str(uuid.uuid4()))
        _add_member(engine, tid, analyst, "analyst")
        _add_member(engine, tid, stylist, "stylist")
        analyst_client = _client(database_url, analyst)
        stylist_client = _client(database_url, stylist)

        # Owner/manager-only surfaces: budget (team.manage), services.manage,
        # reports.read. Analyst has only reports.aggregate.read; stylist none.
        denied = [
            analyst_client.put(f"{base}/billing/budget", json={"spend_ceiling_minor": 5}),
            stylist_client.put(f"{base}/billing/budget", json={"spend_ceiling_minor": 5}),
            analyst_client.get(f"{base}/billing/budget"),
            analyst_client.post(f"{base}/services",
                                json={"name": "Cut", "duration_minutes": 30, "price_minor": 1000}),
            stylist_client.post(f"{base}/services",
                                json={"name": "Cut", "duration_minutes": 30, "price_minor": 1000}),
            analyst_client.get(f"{base}/reports/operational",
                               params={"date_from": date.today().isoformat(),
                                       "date_to": date.today().isoformat()}),
            stylist_client.get(f"{base}/reports/operational",
                               params={"date_from": date.today().isoformat(),
                                       "date_to": date.today().isoformat()}),
            stylist_client.get(f"{base}/reports/today",
                               params={"day": date.today().isoformat()}),
        ]
        for resp in denied:
            assert resp.status_code in (403, 404), resp.text
        assert any(r.status_code == 403 for r in denied), "expected at least one 403 denial"
        # Owner still passes everywhere probed.
        assert owner_client.get(f"{base}/billing/budget").status_code == 200
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_sec56_outsider_denied_on_surfaces(database_url):
    """An identity with no membership gets 404 tenant_not_found on 6 surfaces."""
    from test_clients import _identity

    issuer = "https://identity.example.invalid/sec56-outsider"
    engine, owner, tenant = _salon(database_url, issuer)
    owner_client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    base = f"/v1/tenants/{tid}"
    try:
        client_id = owner_client.post(f"{base}/clients", json={"name": "Outsider Probe"}).json()["id"]
        outsider = _client(database_url, _identity())
        probes = [
            outsider.get(f"{base}/locations"),
            outsider.get(f"{base}/clients/{client_id}"),
            outsider.get(f"{base}/appointments", params={"limit": 5}),
            outsider.get(f"{base}/reports/operational",
                         params={"date_from": date.today().isoformat(),
                                 "date_to": date.today().isoformat()}),
            outsider.get(f"{base}/reports/today"),
            outsider.get(f"{base}/billing/budget"),
        ]
        for resp in probes:
            assert resp.status_code == 404, resp.text
            assert resp.json()["detail"]["code"] == "tenant_not_found", resp.text
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


# 3. Concurrency invariants ------------------------------------------------------

def test_sec56_hold_race_single_winner(database_url):
    """30 threads racing one slot produce exactly one hold (reuses _bookable)."""
    issuer = "https://identity.example.invalid/sec56-holdrace"
    engine, owner, tenant = _salon(database_url, issuer)
    seeder = _client(database_url, owner)
    try:
        _, slug, service, staff, _ = _bookable(seeder, database_url, tenant)
        start = _first_slot(seeder, slug, service, date.today() + timedelta(days=1))
        results, lock, barrier = [], threading.Lock(), threading.Barrier(30)

        def attempt(index: int):
            local = _client(database_url, owner)
            barrier.wait()
            try:
                resp = local.post(
                    f"/v1/public/salons/{slug}/holds",
                    json={"service_id": service, "staff_id": staff, "starts_at": start,
                          "idempotency_key": f"sec56-race-{index}-{uuid.uuid4().hex}"})
                code = resp.status_code
            except Exception:  # noqa: BLE001 — a transport error is a recorded loss, not a hang
                code = "ERR"
            with lock:
                results.append(code)

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert len(results) == 30, results
        assert results.count(201) == 1, f"expected exactly one winner, got {results}"
        assert all(code in (201, 409) for code in results), results
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                               {"t": tenant["tenant_id"]})
            count = connection.execute(
                text("SELECT count(*) FROM booking_holds WHERE tenant_id=:t"),
                {"t": tenant["tenant_id"]}).scalar_one()
        assert count == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_sec56_appointment_idempotency_race_single_row(database_url):
    """10 threads racing one appointment idempotency key produce exactly one row."""
    issuer = "https://identity.example.invalid/sec56-idemrace"
    engine, owner, tenant = _salon(database_url, issuer)
    seeder = _client(database_url, owner)
    try:
        base, _, service, staff, client_row = _bookable(seeder, database_url, tenant)
        day = date.today() + timedelta(days=1)
        starts = datetime.combine(day, datetime.min.time()).replace(
            hour=10, tzinfo=ZoneInfo("America/Toronto")).isoformat()
        key = "sec56-idem-" + uuid.uuid4().hex[:32]
        results, lock, barrier = [], threading.Lock(), threading.Barrier(10)

        def attempt():
            local = _client(database_url, owner)
            barrier.wait()
            try:
                resp = local.post(f"{base}/appointments", json={
                    "client_id": client_row, "staff_id": staff, "service_id": service,
                    "starts_at": starts, "source": "staff", "idempotency_key": key})
                code = resp.status_code
            except Exception:  # noqa: BLE001 — recorded, never a hang
                code = "ERR"
            with lock:
                results.append(code)

        threads = [threading.Thread(target=attempt) for _ in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        assert len(results) == 10, results
        # Losers either replay the key (201) or lose the slot (409); the row
        # count is the invariant, not the per-thread code.
        assert all(code in (201, 409) for code in results), results
        assert 201 in results
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                               {"t": tenant["tenant_id"]})
            count = connection.execute(
                text("""SELECT count(*) FROM appointments
                        WHERE tenant_id=:t AND idempotency_key=:k"""),
                {"t": tenant["tenant_id"], "k": key}).scalar_one()
        assert count == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


# 4. Provider-fault behavior ------------------------------------------------------

def test_sec56_provider_fault_keeps_booking_and_retries(database_url):
    """FailingAdapter dispatch leaves the booking intact and schedules a retry."""
    issuer = "https://identity.example.invalid/sec56-fault"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        email = _client_email(engine, tenant["tenant_id"], client_id)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            queued = conn.execute(text("""SELECT id::text AS id FROM communications
                WHERE tenant_id=:t AND source_type='appointment' AND source_id=:appointment
                  AND purpose='booking_confirmation' AND channel='email'"""),
                {"t": tenant["tenant_id"], "appointment": appt_id}).mappings().one()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=FailingAdapter())
        assert summary["retry_scheduled"] == 1
        assert summary["dispatched"] == 0
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            status = conn.execute(
                text("SELECT status FROM appointments WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": appt_id}).scalar_one()
            comm_state = conn.execute(
                text("SELECT state FROM communications WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": queued["id"]}).scalar_one()
            attempt = conn.execute(
                text("SELECT attempt, state FROM dispatch_attempts WHERE tenant_id=:t AND communication_id=:id"),
                {"t": tenant["tenant_id"], "id": queued["id"]}).mappings().one()
        assert status == "confirmed"
        assert comm_state == "retry_scheduled"
        assert (attempt["attempt"], attempt["state"]) == (1, "queued")
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


# 5. Consent/suppression race ------------------------------------------------------

def test_sec56_suppress_between_record_and_dispatch_blocks_send(database_url):
    """record -> suppress -> dispatch blocks the send; duplicate STOP is idempotent."""
    issuer = "https://identity.example.invalid/sec56-suppress"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    try:
        _, _, _, client_id, appt_id, _ = _booked_appointment(client, tenant)
        email = _client_email(engine, tenant["tenant_id"], client_id)
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            row = conn.execute(text("""SELECT id::text AS id FROM communications
                WHERE tenant_id=:t AND source_type='appointment' AND source_id=:appointment
                  AND purpose='booking_confirmation' AND channel='email'"""),
                {"t": tenant["tenant_id"], "appointment": appt_id}).mappings().one()
            # The STOP arrives after the message was recorded (the race), then
            # arrives AGAIN (provider retries / double tap) — the upsert keeps
            # exactly one suppression row.
            record_suppression(
                conn, tenant_id=tenant["tenant_id"], client_id=client_id,
                contact_value=email, channel="email", reason="stop")
            record_suppression(
                conn, tenant_id=tenant["tenant_id"], client_id=client_id,
                contact_value=email, channel="email", reason="stop")
            assert check_suppressed(
                conn, tenant_id=tenant["tenant_id"],
                contact_value=email, channel="email") is True
            dupes = conn.execute(
                text("""SELECT count(*) FROM suppressions
                        WHERE tenant_id=:t AND channel='email'"""),
                {"t": tenant["tenant_id"]}).scalar_one()
            assert dupes == 1
        adapter = RecordingAdapter()
        with engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            summary = dispatch_due_communications(
                connection=conn, tenant_id=tenant["tenant_id"], adapter=adapter)
        assert summary["suppressed"] == 1
        assert summary["dispatched"] == 0
        assert adapter.records == []
        with engine.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant["tenant_id"]})
            state = conn.execute(
                text("SELECT state FROM communications WHERE tenant_id=:t AND id=:id"),
                {"t": tenant["tenant_id"], "id": row["id"]}).scalar_one()
        assert state == "suppressed"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


# 6. Budget isolation (NOT the ceiling race — that lives in test_budgets.py) --------

def test_sec56_budget_spend_is_per_tenant(database_url):
    """Tenant A's ceiling spend never counts toward tenant B's budget."""
    from modules.communications.senders import register_sender, verify_sender

    issuer_a = "https://identity.example.invalid/sec56-budget-a"
    issuer_b = "https://identity.example.invalid/sec56-budget-b"
    engine_a, owner_a, tenant_a = _salon(database_url, issuer_a)
    engine_b, owner_b, tenant_b = _salon(database_url, issuer_b)
    client_a = _client(database_url, owner_a)
    client_b = _client(database_url, owner_b)
    try:
        tid_a, tid_b = tenant_a["tenant_id"], tenant_b["tenant_id"]
        _, _, _, client_a_id, appt_a, _ = _booked_appointment(client_a, tenant_a)
        _, _, _, client_b_id, appt_b, _ = _booked_appointment(client_b, tenant_b)
        assert client_a.put(f"/v1/tenants/{tid_a}/billing/budget",
                            json={"spend_ceiling_minor": 1}).status_code == 200
        assert client_b.put(f"/v1/tenants/{tid_b}/billing/budget",
                            json={"spend_ceiling_minor": 5}).status_code == 200

        def _send(engine, tid, client_row, appt_id, purpose, sender_number):
            with engine.begin() as conn:
                conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
                created = register_sender(conn, tenant_id=tid, label="Main",
                                          sender_identity=sender_number)
                verify_sender(conn, tenant_id=tid, sender_id=created["id"],
                              code=created["verification_code"])
                record_booking_communication(
                    connection=conn, tenant_id=tid, appointment_id=appt_id,
                    client_id=client_row, contact_value="+14165550101", channel="sms",
                    purpose=purpose, entity_version=1)
                adapter = RecordingAdapter()
                summary = dispatch_due_communications(
                    connection=conn, tenant_id=tid, adapter=adapter)
                return summary, adapter.records

        # Sender numbers are globally unique (never pooled across salons), so
        # each tenant registers its own. Appointment confirmation delivery may
        # already have happened during booking, so assert the SMS itself and
        # the actual per-tenant spend rather than relying on a global message count.
        summary_a, sends_a = _send(engine_a, tid_a, client_a_id, appt_a, "sec56_a_1",
                                   "+14165550111")
        assert summary_a["dispatched"] >= 1
        assert any(send["channel"] == "sms" for send in sends_a)
        assert client_b.get(f"/v1/tenants/{tid_b}/billing/budget").json()["spent_minor"] == 0
        summary_b, sends_b = _send(engine_b, tid_b, client_b_id, appt_b, "sec56_b_1",
                                   "+14165550222")
        assert summary_b["dispatched"] >= 1
        assert any(send["channel"] == "sms" for send in sends_b)
        report_a = client_a.get(f"/v1/tenants/{tid_a}/billing/budget").json()
        report_b = client_b.get(f"/v1/tenants/{tid_b}/billing/budget").json()
        assert (report_a["spent_minor"], report_b["spent_minor"]) == (1, 1)
        with engine_b.connect() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid_b})
            leaked = conn.execute(
                text("""SELECT COALESCE(SUM(cost_minor),0) FROM message_costs
                        WHERE tenant_id=:other"""),
                {"other": tid_a}).scalar_one()
            assert leaked == 0
    finally:
        _teardown(engine_a, [tenant_a["tenant_id"]], issuer_a)
        _teardown(engine_b, [tenant_b["tenant_id"]], issuer_b)


# 7. Load smoke (bounded, honest) ----------------------------------------------------

def test_sec56_load_smoke_report_and_today(database_url):
    """Seeded tenant: operational + today each answer in <10s (smoke-only bound)."""
    from test_operational import _book as _op_book
    from test_operational import _setup as _op_setup

    issuer = "https://identity.example.invalid/sec56-smoke"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        base, _, cut, _, staff, day = _op_setup(client, tenant)
        regular = client.post(f"{base}/clients", json={"name": "Smoke Regular"}).json()["id"]
        for n, hour in enumerate((9, 11, 14)):
            _op_book(client, base, cut, staff, regular, day, hour, f"sec56-smoke-{n}")
        params = {"date_from": date.today().isoformat(), "date_to": day.isoformat()}

        started = time.perf_counter()
        report = client.get(f"/v1/tenants/{tid}/reports/operational", params=params)
        operational_ms = (time.perf_counter() - started) * 1000
        assert report.status_code == 200, report.text

        started = time.perf_counter()
        today = client.get(f"/v1/tenants/{tid}/reports/today",
                           params={"day": day.isoformat()})
        today_ms = (time.perf_counter() - started) * 1000
        assert today.status_code == 200, today.text

        # Smoke-only bound: guards against pathological regressions on a tiny
        # seed, not a capacity claim. Prints survive with -s for the record.
        print(f"\n[sec56-smoke] operational_report_ms={operational_ms:.1f} "
              f"today_ms={today_ms:.1f}")
        assert operational_ms < 10_000, f"operational report took {operational_ms:.1f}ms"
        assert today_ms < 10_000, f"today report took {today_ms:.1f}ms"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
