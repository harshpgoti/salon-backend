"""TASK-056 published load invariants (FR-082, synthetic-only, CI subset).

This module is the measurable slice of qa-plan.md's load section that hosted CI
can honestly run. Everything is synthetic, against a throwaway tenant set.

Honesty limits, stated the same way the plan states them:

- The hard invariants (single winner, zero double bookings, no tenant
  starvation, no 5xx) are asserted, not sampled.
- The latency bounds below are **CI smoke tripwires** on a small dataset and
  shared runner. They are published so a regression fails the build; they are
  NOT capacity claims. The full qa-plan scenario (250 tenants, 50 RPS mixed
  reads, 10 RPS booking mutations, campaign fairness, production p95 targets)
  requires reviewed staging capacity and remains owner-held.
- Machine sizing, data volume and every measured value are written to the
  hosted CI artifact by `scripts/qa_evidence.py`.

Published CI-subset thresholds (keep in sync with plan/qa-plan.md):
- one-slot hold race p95 < HOLD_RACE_P95_MS (5000 ms)
- mixed-load booking mutation p95 < MUTATION_P95_MS (5000 ms)
- mixed-load read p95 < READ_P95_MS (3000 ms)
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from test_booking_holds import _bookable, _first_slot  # noqa: E402
from test_clients import _client, _salon, _teardown  # noqa: E402

HOLD_RACE_CONTENDERS = 100
HOLD_RACE_P95_MS = 5000
MUTATION_P95_MS = 5000
READ_P95_MS = 3000


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((percent / 100) * (len(ordered) - 1))))
    return round(ordered[index], 1)


def _evidence(metric: str, payload: dict) -> None:
    print("[qa-evidence] " + json.dumps({"metric": metric, **payload}, sort_keys=True), flush=True)


# QA-04: published 100-contender single-slot invariant.

def test_qa04_one_hundred_contenders_produce_exactly_one_winner(database_url):
    """100 simultaneous public hold requests for one slot: one 201, 99 conflicts."""
    issuer = "https://identity.example.invalid/load-qa04"
    engine, owner, tenant = _salon(database_url, issuer)
    seeder = _client(database_url, owner)
    try:
        _, slug, service, staff, _ = _bookable(seeder, database_url, tenant)
        start = _first_slot(seeder, slug, service, date.today() + timedelta(days=1))
        clients = [_client(database_url, owner) for _ in range(HOLD_RACE_CONTENDERS)]
        results: list[tuple[int, float]] = []
        lock = threading.Lock()
        barrier = threading.Barrier(HOLD_RACE_CONTENDERS)

        def attempt(index: int) -> None:
            try:
                barrier.wait(timeout=120)
            except Exception:  # noqa: BLE001 - a broken barrier is a recorded loss
                with lock:
                    results.append((0, 0.0))
                return
            started = time.perf_counter()
            try:
                resp = clients[index].post(
                    f"/v1/public/salons/{slug}/holds",
                    json={"service_id": service, "staff_id": staff, "starts_at": start,
                          "idempotency_key": f"qa04-{uuid.uuid4().hex}"})
                code = resp.status_code
            except Exception:  # noqa: BLE001 - a transport failure is a recorded loss
                code = 0
            elapsed_ms = (time.perf_counter() - started) * 1000
            with lock:
                results.append((code, elapsed_ms))

        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(HOLD_RACE_CONTENDERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=180)

        codes = [code for code, _ in results]
        latencies = [ms for _, ms in results]
        winners = codes.count(201)
        conflicts = codes.count(409)
        server_errors = [code for code in codes if code >= 500]
        transport_losses = [code for code in codes if code == 0]

        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                               {"t": tenant["tenant_id"]})
            hold_rows = connection.execute(
                text("SELECT count(*) FROM booking_holds WHERE tenant_id=:t"),
                {"t": tenant["tenant_id"]}).scalar_one()

        p95 = _percentile(latencies, 95)
        _evidence("qa04.hold_race", {
            "contenders": HOLD_RACE_CONTENDERS, "responses": len(results),
            "winners": winners, "conflicts": conflicts,
            "server_errors": len(server_errors), "transport_losses": len(transport_losses),
            "hold_rows": hold_rows,
            "p50_ms": _percentile(latencies, 50), "p95_ms": p95,
            "threshold_p95_ms": HOLD_RACE_P95_MS,
            "winner_ok": winners == 1, "single_row_ok": hold_rows == 1,
            "latency_ok": p95 < HOLD_RACE_P95_MS,
        })

        assert len(results) == HOLD_RACE_CONTENDERS, f"only {len(results)} of 100 attempts returned"
        assert transport_losses == [] and server_errors == [], codes
        assert winners == 1, f"expected exactly one winner, got {winners}: {sorted(codes)}"
        assert conflicts == HOLD_RACE_CONTENDERS - 1, sorted(codes)
        assert hold_rows == 1
        assert p95 < HOLD_RACE_P95_MS, f"hold race p95 {p95}ms exceeds {HOLD_RACE_P95_MS}ms"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


# Mixed multi-tenant load: bookings + holds + reads together, no starvation.

def test_mixed_load_four_tenants_book_hold_read_without_starvation(database_url):
    """4 tenants x (booking + hold + reads) concurrently: all succeed, none starved."""
    tenants = []
    seeders = []
    fixtures = []
    try:
        for index in range(4):
            issuer = f"https://identity.example.invalid/load-mixed-{index}"
            engine, owner, tenant = _salon(database_url, issuer)
            seeder = _client(database_url, owner)
            _, slug, service, staff, client_row = _bookable(seeder, database_url, tenant)
            slots = seeder.get(f"/v1/public/salons/{slug}/availability", params={
                "service_id": service,
                "date_from": (date.today() + timedelta(days=1)).isoformat(),
                "date_to": (date.today() + timedelta(days=1)).isoformat()}).json()["slots"]
            # slots[0] and slots[4] are four grid steps apart, so a 60-minute
            # service booked at slots[0] never overlaps a hold at slots[4]
            # (same pairing as test_booking_holds.py's management-scope test).
            assert len(slots) >= 5, "fixture must expose at least five slots"
            tenants.append((issuer, engine, owner, tenant))
            seeders.append(seeder)
            fixtures.append({"slug": slug, "service": service, "staff": staff,
                             "client": client_row, "slots": slots,
                             "base": f"/v1/tenants/{tenant['tenant_id']}"})

        results: list[dict] = []
        lock = threading.Lock()
        barrier = threading.Barrier(len(tenants) * 3)

        def timed(tenant_index: int, kind: str, call):
            client = None
            started = time.perf_counter()
            try:
                client = _client(database_url, tenants[tenant_index][2])
                barrier.wait(timeout=120)
                started = time.perf_counter()
                status = call(client, tenant_index)
            except Exception:  # noqa: BLE001 - recorded as a loss
                status = 0
            elapsed_ms = (time.perf_counter() - started) * 1000
            with lock:
                results.append({"tenant": tenant_index, "kind": kind,
                                "status": status, "ms": elapsed_ms})

        def do_booking(client, index):
            fixture = fixtures[index]
            resp = client.post(f"{fixture['base']}/appointments", json={
                "client_id": fixture["client"], "staff_id": fixture["staff"],
                "service_id": fixture["service"], "starts_at": fixture["slots"][0]["starts_at"],
                "source": "staff", "idempotency_key": f"mixed-book-{uuid.uuid4().hex}"})
            return resp.status_code

        def do_hold(client, index):
            fixture = fixtures[index]
            resp = client.post(f"/v1/public/salons/{fixture['slug']}/holds", json={
                "service_id": fixture["service"], "staff_id": fixture["staff"],
                "starts_at": fixture["slots"][4]["starts_at"],
                "idempotency_key": f"mixed-hold-{uuid.uuid4().hex}"})
            return resp.status_code

        def do_reads(client, index):
            fixture = fixtures[index]
            day = (date.today() + timedelta(days=1)).isoformat()
            availability = client.get(f"/v1/public/salons/{fixture['slug']}/availability", params={
                "service_id": fixture["service"], "date_from": day, "date_to": day})
            today = client.get(f"{fixture['base']}/reports/today", params={"day": day})
            return availability.status_code if availability.status_code != 200 else today.status_code

        threads = []
        for index in range(len(tenants)):
            threads.append(threading.Thread(target=timed, args=(index, "booking", do_booking)))
            threads.append(threading.Thread(target=timed, args=(index, "hold", do_hold)))
            threads.append(threading.Thread(target=timed, args=(index, "read", do_reads)))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=180)

        mutation_ms = [row["ms"] for row in results if row["kind"] in ("booking", "hold")]
        read_ms = [row["ms"] for row in results if row["kind"] == "read"]
        mutation_p95 = _percentile(mutation_ms, 95)
        read_p95 = _percentile(read_ms, 95)
        per_tenant = {
            str(index): {
                "booking": next((r["status"] for r in results
                                 if r["tenant"] == index and r["kind"] == "booking"), None),
                "hold": next((r["status"] for r in results
                              if r["tenant"] == index and r["kind"] == "hold"), None),
                "read": next((r["status"] for r in results
                              if r["tenant"] == index and r["kind"] == "read"), None),
            } for index in range(len(tenants))
        }
        starved = [tenant_index for tenant_index, outcome in per_tenant.items()
                   if outcome["booking"] != 201 or outcome["hold"] != 201 or outcome["read"] != 200]
        server_errors = [row for row in results if row["status"] == 0 or row["status"] >= 500]

        _evidence("qa_mixed.fairness", {
            "tenants": len(tenants), "threads": len(threads), "responses": len(results),
            "data_volume": {"tenants": len(tenants), "services_per_tenant": 1,
                            "staff_per_tenant": 1, "slots_reserved_per_tenant": 2},
            "starved_tenants": starved, "server_errors": len(server_errors),
            "mutation_p95_ms": mutation_p95, "threshold_mutation_p95_ms": MUTATION_P95_MS,
            "read_p95_ms": read_p95, "threshold_read_p95_ms": READ_P95_MS,
            "per_tenant": per_tenant,
            "fairness_ok": starved == [],
            "latency_ok": mutation_p95 < MUTATION_P95_MS and read_p95 < READ_P95_MS,
        })

        assert len(results) == len(threads), f"{len(results)} of {len(threads)} threads returned"
        assert server_errors == [], server_errors
        assert starved == [], f"starved tenants: {starved}"
        assert mutation_p95 < MUTATION_P95_MS, f"mutation p95 {mutation_p95}ms"
        assert read_p95 < READ_P95_MS, f"read p95 {read_p95}ms"
    finally:
        for issuer, engine, _owner, tenant in tenants:
            _teardown(engine, [tenant["tenant_id"]], issuer)
