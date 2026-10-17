"""Tests for plans and hosted subscription checkout (TASK-046, FR-065/FR-066).

AC-1: Plan/limits/version and hosted checkout/portal/invoices accessible to billing role.
AC-2: Browser success never grants access; no card data stored.
"""

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_clients import _salon, _client, _teardown
from modules.identity.main import AuthenticatedIdentity


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def test_billing_ac1_plans_and_trial_subscription(database_url):
    """AC-1: seeded catalogue with limits/versions; onboarding opens a trial."""
    issuer = "https://identity.example.invalid/bill-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/billing"
    try:
        plans = client.get(f"{base}/plans")
        assert plans.status_code == 200, plans.text
        items = plans.json()["items"]
        assert [p["code"] for p in items] == ["essential", "growth", "multi_location"]
        assert all(p["currency"] == "CAD" and p["version"] == 1 for p in items)
        essential = next(p for p in items if p["code"] == "essential")
        assert essential["price_minor"] == 4900
        assert essential["limits"]["max_locations"] == 1
        assert essential["limits"]["included_sms_segments"] == 200
        sub = client.get(f"{base}/subscription")
        assert sub.status_code == 200, sub.text
        assert sub.json()["status"] == "trial"
        assert sub.json()["plan_code"] == "essential"
        assert sub.json()["trial_ends_at"]
        # A stranger's session learns nothing about this salon.
        from test_clients import _identity
        outsider = _client(database_url, _identity())
        assert outsider.get(f"{base}/plans").status_code == 404
        assert outsider.get(f"{base}/subscription").status_code == 404
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_billing_ac2_checkout_grants_nothing(database_url):
    """AC-2: hosted checkout stays pending; visiting success changes nothing."""
    issuer = "https://identity.example.invalid/bill-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/billing"
    try:
        started = client.post(f"{base}/checkout", json={"plan_code": "growth"})
        assert started.status_code == 201, started.text
        session = started.json()
        assert session["status"] == "pending"
        assert session["checkout_url"].startswith("https://checkout.stripe.test/pay/")
        # Polling the session (the browser success return) grants nothing.
        reread = client.get(f"{base}/checkout/{session['session_id']}")
        assert reread.status_code == 200 and reread.json()["status"] == "pending"
        sub = client.get(f"{base}/subscription").json()
        assert sub["status"] == "trial" and sub["plan_code"] == "essential"
        bad_plan = client.post(f"{base}/checkout", json={"plan_code": "platinum"})
        assert bad_plan.status_code == 422
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_billing_ac1_portal_and_invoices_without_card_data(database_url):
    """AC-1/AC-2: portal and invoices work; card data exists nowhere."""
    issuer = "https://identity.example.invalid/bill-3"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/billing"
    try:
        portal = client.post(f"{base}/portal")
        assert portal.status_code == 200, portal.text
        assert portal.json()["url"].startswith("https://billing.stripe.test/portal/")
        invoices = client.get(f"{base}/invoices")
        assert invoices.status_code == 200, invoices.text
        items = invoices.json()["items"]
        assert len(items) == 1
        assert items[0]["plan"] == "Essential" and items[0]["currency"] == "CAD"
        blob = str(invoices.json()) + str(portal.json())
        assert "card" not in blob.lower() and "4242" not in blob
        with engine.connect() as conn:
            cols = conn.execute(
                text("""SELECT column_name FROM information_schema.columns
                        WHERE table_schema='public'
                          AND table_name IN ('tenant_subscriptions',
                                             'billing_checkout_sessions', 'billing_plans')""")
            ).scalars().all()
        assert not [c for c in cols if "card" in c or "pan" in c or "cvv" in c]
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_billing_billing_role_is_owner():
    """AC-1: subscription writes need the billing role (owner's team.manage)."""
    from modules.identity.main import ROLE_PERMISSIONS
    assert "*" in ROLE_PERMISSIONS["owner"]
    assert "team.manage" not in ROLE_PERMISSIONS["manager"]
    assert "team.manage" not in ROLE_PERMISSIONS["receptionist"]


def test_billing_plan_catalogue_is_readable_but_billing_actions_are_owner_only(database_url):
    """AC-1: salon managers may inspect plans; only the billing owner can act."""
    issuer = "https://identity.example.invalid/bill-manager"
    engine, owner, tenant = _salon(database_url, issuer)
    manager = AuthenticatedIdentity(issuer, "manager-" + tenant["tenant_id"])
    with engine.begin() as connection:
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                           {"t": tenant["tenant_id"]})
        user_id = connection.execute(
            text("INSERT INTO identity_users (issuer,subject) VALUES (:i,:s) RETURNING id"),
            {"i": manager.issuer, "s": manager.subject},
        ).scalar_one()
        connection.execute(
            text("INSERT INTO memberships (tenant_id,user_id,role) VALUES (:t,:u,'manager')"),
            {"t": tenant["tenant_id"], "u": str(user_id)},
        )
    client = _client(database_url, manager)
    base = f"/v1/tenants/{tenant['tenant_id']}/billing"
    try:
        assert client.get(f"{base}/plans").status_code == 200
        assert client.get(f"{base}/subscription").status_code == 403
        assert client.post(f"{base}/checkout", json={"plan_code": "growth"}).status_code == 403
        assert client.post(f"{base}/portal").status_code == 403
        assert client.get(f"{base}/invoices").status_code == 403
        # The owner remains the authorized billing role for the same tenant.
        owner_client = _client(database_url, owner)
        assert owner_client.get(f"{base}/subscription").status_code == 200
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)


def test_synthetic_billing_provider_fails_closed_outside_local_modes(database_url, monkeypatch):
    """AC-2 and G-COMMERCIAL: no test adapter may be presented as live billing."""
    issuer = "https://identity.example.invalid/bill-production-disabled"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    base = f"/v1/tenants/{tenant['tenant_id']}/billing"
    monkeypatch.setenv("APP_ENV", "production")
    try:
        # Reading the local catalogue and trial remains available for supportability.
        assert client.get(f"{base}/plans").status_code == 200
        assert client.get(f"{base}/subscription").status_code == 200
        def unavailable(response):
            assert response.status_code == 503, response.text
            assert response.json()["detail"]["code"] == "billing_provider_unconfigured"

        unavailable(client.post(f"{base}/checkout", json={"plan_code": "growth"}))
        unavailable(client.get(f"{base}/checkout/00000000-0000-4000-8000-000000000000"))
        unavailable(client.post(f"{base}/portal"))
        unavailable(client.get(f"{base}/invoices"))
        unavailable(client.post(f"{base}/subscription/plan", json={"plan_code": "growth"}))
        unavailable(client.post(f"{base}/subscription/cancel", json={"at_period_end": True}))
        unavailable(client.post("/v1/billing/webhooks/stripe", json={}))
        assert client.get(f"{base}/subscription").json()["plan_code"] == "essential"
    finally:
        _teardown(engine, [tenant["tenant_id"]], issuer)
