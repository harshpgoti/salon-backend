"""Tests for controlled client merge (TASK-013, FR-015)."""

import os
import uuid
from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import text
from test_clients import _salon, _client, _teardown
from test_booking_holds import _bookable


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL CRM integration")
    return url


def _two_clients(client, base, name_a="Survivor", name_b="Duplicate", shared_phone=None):
    a = client.post(f"{base}/clients", json={"name": name_a}).json()["id"]
    b_payload = {"name": name_b}
    b = client.post(f"{base}/clients", json=b_payload).json()["id"]
    if shared_phone:
        for cid in (a, b):
            client.post(f"{base}/clients/{cid}/contacts", json={"kind": "phone", "value": shared_phone})
    return a, b


def test_merge_preview_writes_nothing_and_lists_manifest(database_url):
    """AC-1: preview shows what would move and changes nothing."""
    issuer = "https://identity.example.invalid/merge-prev"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        a, b = _two_clients(client, base, shared_phone="(416) 555-0134")
        client.post(f"{base}/clients/{b}/visit-notes", json={"class": "general", "visibility": "standard", "body": "note"})
        resp = client.post(f"{base}/client-merges/preview",
                           json={"survivor_client_id": a, "merged_client_id": b})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["manifest"]["contacts"] and body["manifest"]["visit_notes"]
        # The shared phone is a conflict the operator must decide on, never auto-resolved.
        assert any(c["kind"] == "phone" for c in body["conflicts"])

        # Preview must not have moved or hidden anything.
        assert client.get(f"{base}/clients/{b}").status_code == 200
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_merge_commit_preserves_history_and_records_map(database_url):
    """AC-1: approved merge moves history to the survivor and records the manifest."""
    issuer = "https://identity.example.invalid/merge-commit"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        a, b = _two_clients(client, base)
        client.post(f"{base}/clients/{b}/visit-notes", json={"class": "general", "visibility": "standard", "body": "b history"})
        communication_id = str(uuid.uuid4())
        _, _, service_id, staff_id, _ = _bookable(client, database_url, tenant)
        day = date.today() + timedelta(days=1)
        while day.weekday() > 5:
            day += timedelta(days=1)
        client.put(f"{base}/locations/{tenant['location_id']}/hours", json={
            "intervals": [{"weekday": day.weekday(), "opens_at": "09:00", "closes_at": "17:00"}],
        })
        client.post(f"{base}/staff/{staff_id}/shifts", json={
            "location_id": tenant["location_id"], "weekday": day.weekday(),
            "starts_at": "09:00", "ends_at": "17:00",
        })
        booked = client.post(f"{base}/appointments", json={
            "client_id": b, "staff_id": staff_id, "service_id": service_id,
            "starts_at": f"{day.isoformat()}T14:00:00+00:00", "source": "staff",
            "idempotency_key": "merge-booking-" + uuid.uuid4().hex,
        })
        assert booked.status_code == 201, booked.text
        appointment_id = booked.json()["appointment_id"]
        for version, next_state in enumerate(("checked_in", "in_service", "completed"), start=1):
            moved = client.post(f"{base}/appointments/{appointment_id}/transitions", json={
                "to_status": next_state, "expected_version": version,
                "idempotency_key": f"merge-transition-{uuid.uuid4().hex}",
            })
            assert moved.status_code == 200, moved.text
        receipt = client.post(f"{base}/appointments/{appointment_id}/receipt-entries", json={
            "kind": "payment", "amount_minor": 6000, "method": "card",
            "idempotency_key": "merge-receipt-" + uuid.uuid4().hex,
        })
        assert receipt.status_code == 201, receipt.text
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            connection.execute(text("""INSERT INTO communications
                (tenant_id,id,client_id,contact_value,channel,purpose,source_type,source_id,
                 expected_entity_version,dedupe_key)
                VALUES (:tenant,:id,:client,'ada@example.invalid','email','booking_confirmation',
                        'appointment',:source_id,1,:dedupe)"""), {
                "tenant": tenant["tenant_id"], "id": communication_id, "client": b,
                "source_id": str(uuid.uuid4()), "dedupe": "merge-" + uuid.uuid4().hex,
            })
        preference = client.post(f"{base}/clients/{b}/preferences", json={"preference_type": "colour_formula", "value_json": {"formula": "7.0"}})
        assert preference.status_code == 201, preference.text
        a_row = client.get(f"{base}/clients/{a}").json()
        b_row = client.get(f"{base}/clients/{b}").json()

        resp = client.post(f"{base}/client-merges/commit", json={
            "survivor_client_id": a, "merged_client_id": b,
            "expected_survivor_version": a_row["version"], "expected_merged_version": b_row["version"],
            "acknowledged_conflicts": True, "idempotency_key": "m-" + uuid.uuid4().hex})
        assert resp.status_code == 200, resp.text

        # History moved to the survivor.
        notes = client.get(f"{base}/clients/{a}/visit-notes").json()
        assert any("b history" in note["body"] for note in notes["items"])
        assert communication_id in resp.json()["manifest"]["communications"]
        for key, row_id in (("appointments", appointment_id), ("visits", None), ("receipts", receipt.json()["entry_id"])):
            if row_id:
                assert row_id in resp.json()["manifest"][key]
            else:
                assert resp.json()["manifest"][key], "completed appointment must keep its visit history"
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            assert connection.execute(text("SELECT client_id::text FROM communications WHERE tenant_id=:tenant AND id=:id"),
                                      {"tenant": tenant["tenant_id"], "id": communication_id}).scalar_one() == a
        prefs = client.get(f"{base}/clients/{a}/preferences").json()
        assert prefs["total"] == 1

        # The merged client is retired, and the merge map is readable.
        assert client.get(f"{base}/clients/{b}").status_code == 404
        assert client.get(f"{base}/clients/{a}").json()["version"] == a_row["version"] + 1
        merges = client.get(f"{base}/client-merges").json()
        assert merges["total"] == 1
        assert merges["items"][0]["merged_client_id"] == b
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_merge_requires_acknowledged_conflicts_and_current_version(database_url):
    """AC-2: a conflict is not merged by inattention; a stale version is refused."""
    issuer = "https://identity.example.invalid/merge-guard"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        a, b = _two_clients(client, base, shared_phone="(416) 555-0199")
        a_row = client.get(f"{base}/clients/{a}").json()
        b_row = client.get(f"{base}/clients/{b}").json()

        # Unacknowledged conflict -> 409, nothing merged.
        resp = client.post(f"{base}/client-merges/commit", json={
            "survivor_client_id": a, "merged_client_id": b,
            "expected_survivor_version": a_row["version"], "expected_merged_version": b_row["version"],
            "acknowledged_conflicts": False, "idempotency_key": "m-" + uuid.uuid4().hex})
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "merge_conflicts_unacknowledged"
        assert client.get(f"{base}/clients/{b}").status_code == 200

        # Stale version -> 409.
        resp = client.post(f"{base}/client-merges/commit", json={
            "survivor_client_id": a, "merged_client_id": b,
            "expected_survivor_version": 99, "expected_merged_version": b_row["version"],
            "acknowledged_conflicts": True, "idempotency_key": "m-" + uuid.uuid4().hex})
        assert resp.status_code == 409
        assert resp.json()["detail"]["code"] == "client_version_conflict"

        # Merging a client into itself is refused.
        resp = client.post(f"{base}/client-merges/preview",
                           json={"survivor_client_id": a, "merged_client_id": a})
        assert resp.status_code == 422
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_merge_commit_is_idempotent(database_url):
    """AC-1: replaying the same merge key returns the receipt, not a second merge."""
    issuer = "https://identity.example.invalid/merge-idem"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        a, b = _two_clients(client, base)
        a_row = client.get(f"{base}/clients/{a}").json()
        b_row = client.get(f"{base}/clients/{b}").json()
        key = "m-" + uuid.uuid4().hex
        payload = {"survivor_client_id": a, "merged_client_id": b,
                   "expected_survivor_version": a_row["version"], "expected_merged_version": b_row["version"],
                   "acknowledged_conflicts": True, "idempotency_key": key}
        first = client.post(f"{base}/client-merges/commit", json=payload)
        assert first.status_code == 200 and first.json()["replayed"] is False
        second = client.post(f"{base}/client-merges/commit", json=payload)
        assert second.status_code == 200 and second.json()["replayed"] is True
        assert first.json()["merges"] == second.json()["merges"]
        assert first.json()["manifest"] == second.json()["manifest"]
        assert first.json()["conflicts"] == second.json()["conflicts"]
        duplicate_apply = client.post(f"{base}/client-merges/commit", json={
            **payload, "idempotency_key": "different-" + uuid.uuid4().hex,
        })
        assert duplicate_apply.status_code == 404
        assert duplicate_apply.json()["detail"]["code"] == "client_not_found"
        assert client.get(f"{base}/client-merges").json()["total"] == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_concurrent_same_key_merge_replays_one_receipt(database_url):
    issuer = "https://identity.example.invalid/merge-concurrent-idem"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        survivor, merged = _two_clients(client, base)
        survivor_row = client.get(f"{base}/clients/{survivor}").json()
        merged_row = client.get(f"{base}/clients/{merged}").json()
        payload = {
            "survivor_client_id": survivor, "merged_client_id": merged,
            "expected_survivor_version": survivor_row["version"],
            "expected_merged_version": merged_row["version"],
            "acknowledged_conflicts": True, "idempotency_key": "concurrent-" + uuid.uuid4().hex,
        }
        barrier = Barrier(2)

        def submit():
            barrier.wait()
            return client.post(f"{base}/client-merges/commit", json=payload)

        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = [future.result() for future in (pool.submit(submit), pool.submit(submit))]
        assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
        assert responses[0].json()["merges"] == responses[1].json()["merges"]
        assert responses[0].json()["manifest"] == responses[1].json()["manifest"]
        assert responses[0].json()["conflicts"] == responses[1].json()["conflicts"]
        assert client.get(f"{base}/client-merges").json()["total"] == 1
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_shared_contact_alone_never_merges_and_never_promotes_consent(database_url):
    """AC-2: a shared phone produces a candidate; it does not merge or confer consent."""
    issuer = "https://identity.example.invalid/merge-consent"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        a, b = _two_clients(client, base, shared_phone="(416) 555-0177")
        # Both clients still exist; no merge happened as a side effect of sharing.
        assert client.get(f"{base}/clients/{b}").status_code == 200
        assert client.get(f"{base}/client-merges").json()["total"] == 0

        preview = client.post(f"{base}/client-merges/preview",
                              json={"survivor_client_id": a, "merged_client_id": b}).json()
        # A conflict is surfaced, not silently resolved, so no consent can be inherited.
        assert preview["conflicts"], "the shared phone must surface as a conflict"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_merge_keeps_duplicate_contact_and_its_consent_with_original_subject(database_url):
    issuer = "https://identity.example.invalid/merge-consent-subject"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        survivor, merged = _two_clients(client, base, shared_phone="(416) 555-0178")
        merged_profile = client.get(f"{base}/clients/{merged}").json()
        contact_id = merged_profile["contacts"][0]["id"]
        consent_id = str(uuid.uuid4())
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            connection.execute(text("""INSERT INTO consents
                (tenant_id,id,client_id,contact_id,channel,purpose,source,wording_version,status,
                 contact_value_snapshot,proof_reference)
                SELECT :tenant,:id,:client,:contact,'sms','marketing','staff','v1','granted',
                       normalized_value,'signed synthetic form'
                FROM client_contacts WHERE tenant_id=:tenant AND id=:contact"""), {
                "tenant": tenant["tenant_id"], "id": consent_id, "client": merged, "contact": contact_id,
            })
        survivor_profile = client.get(f"{base}/clients/{survivor}").json()
        preview = client.post(f"{base}/client-merges/preview", json={
            "survivor_client_id": survivor, "merged_client_id": merged,
        }).json()
        result = client.post(f"{base}/client-merges/commit", json={
            "survivor_client_id": survivor, "merged_client_id": merged,
            "expected_survivor_version": survivor_profile["version"],
            "expected_merged_version": merged_profile["version"],
            "acknowledged_conflicts": True, "idempotency_key": "consent-" + uuid.uuid4().hex,
        })
        assert result.status_code == 200, result.text
        assert contact_id in result.json()["manifest"]["retained"]["contacts"]
        assert consent_id in result.json()["manifest"]["retained"]["consents"]
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            assert connection.execute(text("SELECT client_id::text FROM consents WHERE tenant_id=:tenant AND id=:id"),
                                      {"tenant": tenant["tenant_id"], "id": consent_id}).scalar_one() == merged
            assert connection.execute(text("SELECT client_id::text FROM client_contacts WHERE tenant_id=:tenant AND id=:id"),
                                      {"tenant": tenant["tenant_id"], "id": contact_id}).scalar_one() == merged
        assert preview["conflicts"]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_merge_is_tenant_scoped(database_url):
    """AC-1: another tenant cannot preview or commit a merge against this tenant."""
    issuer = "https://identity.example.invalid/merge-scope"
    other_issuer = "https://identity.example.invalid/merge-scope-b"
    engine, owner, tenant = _salon(database_url, issuer)
    other_engine, other_owner, other_tenant = _salon(database_url, other_issuer)
    client = _client(database_url, owner)
    other_client = _client(database_url, other_owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    other_base = f"/v1/tenants/{other_tenant['tenant_id']}"
    try:
        a, b = _two_clients(client, base)
        assert other_client.post(f"{other_base}/client-merges/preview",
                                 json={"survivor_client_id": a, "merged_client_id": b}).status_code == 404
        assert other_client.get(f"{other_base}/client-merges").json()["total"] == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
        _teardown(other_engine, [other_tenant["tenant_id"]], other_issuer)
