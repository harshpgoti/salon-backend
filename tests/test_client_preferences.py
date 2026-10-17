"""Tests for client preferences (TASK-011, FR-013)."""

import os
import pytest
import uuid
from test_clients import _salon, _client, _teardown
from sqlalchemy import text


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL CRM integration")
    return url


def test_preference_crud_and_structure(database_url):
    """AC-1: Preferences persist with structured JSON and typed fields."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/pref-1")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        # Create client
        resp = client.post(f"{base}/clients", json={"name": "Preference Test"})
        assert resp.status_code == 201
        client_id = resp.json()["id"]

        # Add colour formula preference
        resp = client.post(f"{base}/clients/{client_id}/preferences", json={
            "preference_type": "colour_formula",
            "value_json": {"formula": "7.0 + 9.1 + 10ml 6%", "developer": "20vol", "processing_time": 35},
            "source": "staff"
        })
        assert resp.status_code == 201
        pref_id_1 = resp.json()["id"]
        assert resp.json()["preference_type"] == "colour_formula"
        assert resp.json()["value_json"]["formula"] == "7.0 + 9.1 + 10ml 6%"

        # Add stylist preference
        resp = client.post(f"{base}/clients/{client_id}/preferences", json={
            "preference_type": "stylist_preference",
            "value_json": {"stylist_name": "Jane", "avoid": ["John"]},
            "source": "client"
        })
        assert resp.status_code == 201
        pref_id_2 = resp.json()["id"]

        # Add product preference
        resp = client.post(f"{base}/clients/{client_id}/preferences", json={
            "preference_type": "product",
            "value_json": {"brand": "Olaplex", "products": ["No.3", "No.4"]},
            "source": "staff"
        })
        assert resp.status_code == 201

        # Add rebooking interval
        resp = client.post(f"{base}/clients/{client_id}/preferences", json={
            "preference_type": "rebooking_interval",
            "value_json": {"days": 42, "service": "colour"},
            "source": "staff"
        })
        assert resp.status_code == 201

        # List preferences
        resp = client.get(f"{base}/clients/{client_id}/preferences")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 4
        types = {i["preference_type"] for i in items}
        assert types == {"colour_formula", "stylist_preference", "product", "rebooking_interval"}

        # Update preference
        resp = client.patch(f"{base}/clients/{client_id}/preferences/{pref_id_1}",
                            json={"value_json": {"formula": "8.0 + 9.1", "developer": "30vol"},
                                  "expected_version": 1})
        assert resp.status_code == 200
        assert resp.json()["value_json"]["formula"] == "8.0 + 9.1"
        stale = client.patch(f"{base}/clients/{client_id}/preferences/{pref_id_1}",
                             json={"value_json": {"formula": "stale"}, "expected_version": 1})
        assert stale.status_code == 409
        missing = client.patch(f"{base}/clients/{client_id}/preferences/{pref_id_1}",
                               json={"value_json": {"formula": "missing version"}})
        assert missing.status_code == 428
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/pref-1")


def test_preference_tenant_isolation(database_url):
    """AC-2: Preferences are tenant-isolated."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/pref-2")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        resp = client.post(f"{base}/clients", json={"name": "Pref Isolation"})
        client_id = resp.json()["id"]

        resp = client.post(f"{base}/clients/{client_id}/preferences", json={
            "preference_type": "colour_formula", "value_json": {"formula": "6.0"}, "source": "staff"
        })
        assert resp.status_code == 201

        other_engine, other_owner, other_tenant = _salon(database_url, "https://identity.example.invalid/pref-2b")
        try:
            other_client = _client(database_url, other_owner)
            other_base = f"/v1/tenants/{other_tenant['tenant_id']}/clients"
            resp = other_client.get(f"{other_base}/{client_id}/preferences")
            assert resp.status_code == 404
        finally:
            _teardown(other_engine, [other_tenant["tenant_id"]], "https://identity.example.invalid/pref-2b")
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/pref-2")


def test_preferences_reject_sensitive_types_and_unrecognized_shapes(database_url):
    """AC-2: unsupported sensitive data and arbitrary JSON are rejected at the API seam."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/pref-policy")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        created = client.post(f"{base}/clients", json={"name": "Policy Test"})
        assert created.status_code == 201
        client_id = created.json()["id"]
        for preference_type, value in (
            ("product_allergy", {"ingredient": "synthetic"}),
            ("sensitivity_note", {"details": "synthetic"}),
            ("colour_formula", {"formula": "6.0", "unreviewed": "extra"}),
            ("communication_preference", {"channels": [{"unreviewed": "value"}]}),
        ):
            response = client.post(f"{base}/clients/{client_id}/preferences", json={
                "preference_type": preference_type, "value_json": value,
            })
            assert response.status_code == 422, response.text
        valid = client.post(f"{base}/clients/{client_id}/preferences", json={
            "preference_type": "communication_preference", "value_json": {"channels": ["email"]},
        })
        assert valid.status_code == 201, valid.text
        stylist = client.post(f"{base}/clients/{client_id}/preferences", json={
            "preference_type": "stylist_preference", "value_json": {"stylist_name": "Synthetic stylist"},
        })
        assert stylist.status_code == 201, stylist.text
        null_patch = client.patch(f"{base}/clients/{client_id}/preferences/{stylist.json()['id']}", json={
            "value_json": None, "expected_version": 1,
        })
        assert null_patch.status_code == 422, null_patch.text
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/pref-policy")


def test_legacy_sensitive_preferences_are_not_returned(database_url):
    """Unsupported rows from older versions stay out of operational reads."""
    engine, owner, tenant = _salon(database_url, "https://identity.example.invalid/pref-legacy")
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}"
    try:
        created = client.post(f"{base}/clients", json={"name": "Legacy Policy Test"})
        assert created.status_code == 201
        client_id = created.json()["id"]
        with engine.begin() as connection:
            connection.execute(text("SELECT set_config('app.tenant_id', :tenant, true)"),
                               {"tenant": tenant["tenant_id"]})
            connection.execute(text("""INSERT INTO client_preferences
                (tenant_id, client_id, preference_type, value_json)
                VALUES (:tenant, :client, 'product_allergy', '{"ingredient":"synthetic"}'::jsonb)"""),
                               {"tenant": tenant["tenant_id"], "client": client_id})
        response = client.get(f"{base}/clients/{client_id}/preferences")
        assert response.status_code == 200, response.text
        assert response.json()["items"] == []
        assert response.json()["total"] == 0
    finally:
        _teardown(engine, [tenant["tenant_id"]], "https://identity.example.invalid/pref-legacy")
