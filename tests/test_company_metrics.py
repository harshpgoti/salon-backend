"""Tests for company health and aggregate projections (TASK-045, FR-063/FR-064).

AC-1: MRR/churn/usage/support/onboarding metrics use documented denominators.
AC-2: No global client data; duplicate events/deletion/backfill reconcile.
"""

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.identity.main import (
    AuthenticatedIdentity,
    create_app,
    require_company_identity,
)
from test_clients import _salon, _client, _teardown

COMPANY = "https://company.example.invalid/metrics"


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _company_client(database_url: str, subject: str) -> TestClient:
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_company_identity] = lambda: AuthenticatedIdentity(
        COMPANY, subject, "company")
    return TestClient(app)


def _invite(admin, subject, role):
    resp = admin.post("/v1/company/staff", json={
        "issuer": COMPANY, "subject": subject, "display_name": subject.title(),
        "role": role, "mfa_enrolled": True})
    assert resp.status_code == 201, resp.text


def _bootstrap(database_url):
    admin = _company_client(database_url, "admin-1")
    first = admin.post("/v1/company/staff", json={
        "issuer": COMPANY, "subject": "admin-1", "display_name": "Ava Admin",
        "role": "admin", "mfa_enrolled": True})
    assert first.status_code == 201, first.text
    return admin


def _cleanup_company(database_url):
    with create_engine(database_url).connect() as conn:
        conn.execute(text("DELETE FROM company_staff WHERE issuer=:i"), {"i": COMPANY})
        conn.execute(text("DELETE FROM identity_users WHERE issuer=:i"), {"i": COMPANY})
        conn.commit()


def _secret(engine):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT secret FROM billing_provider_secrets WHERE provider='stripe_test'"),
        ).scalar_one()


def _webhook(client, secret, event):
    raw = json.dumps(event).encode()
    return client.post("/v1/billing/webhooks/stripe", content=raw,
                       headers={"Content-Type": "application/json",
                                "X-Provider-Signature": hmac.new(
                                    secret.encode(), raw, hashlib.sha256).hexdigest()})


def _activate(client, engine, tid, secret, customer):
    started = client.post(f"/v1/tenants/{tid}/billing/checkout",
                          json={"plan_code": "growth"})
    assert started.status_code == 201, started.text
    session_ref = started.json()["checkout_url"].rsplit("/", 1)[-1]
    now = datetime.now(timezone.utc)
    out = _webhook(client, secret,
                   {"id": f"evt-m-{customer}", "type": "checkout.session.completed",
                    "occurred_at": now.isoformat(),
                    "data": {"tenant_id": tid, "session_ref": session_ref,
                             "customer_ref": customer, "subscription_ref": f"sub-{customer}",
                             "plan_code": "growth",
                             "period_end": (now + timedelta(days=30)).isoformat()}})
    assert out.json()["status"] == "applied", out.text


def test_metrics_ac1_live_numbers_reconcile(database_url):
    """AC-1: MRR, counts, churn and onboarding follow their denominators."""
    admin = _bootstrap(database_url)
    _invite(admin, "fin-m", "finance")
    finance = _company_client(database_url, "fin-m")
    issuer_a = "https://identity.example.invalid/met-a"
    engine_a, owner_a, tenant_a = _salon(database_url, issuer_a)
    client_a = _client(database_url, owner_a)
    issuer_b = "https://identity.example.invalid/met-b"
    engine_b, owner_b, tenant_b = _salon(database_url, issuer_b)
    client_b = _client(database_url, owner_b)
    try:
        secret = _secret(engine_a)
        _activate(client_b, engine_b, tenant_b["tenant_id"], secret, "cus_test_met_b")
        live = finance.get("/v1/company/metrics").json()["live"]
        # Relative invariants, not absolute counts: the shared dev database
        # accumulates tenants across runs, so only deltas are stable.
        assert live["paying_tenants"] >= 1
        assert live["trial_tenants"] >= 1
        assert live["mrr_minor"] >= 9900
        assert live["locations"] >= 2
        # Cancelling B churns exactly one payer out of MRR.
        now = datetime.now(timezone.utc)
        cancelled = _webhook(client_b, secret,
                             {"id": "evt-m-cancel-b", "type": "customer.subscription.updated",
                              "occurred_at": now.isoformat(),
                              "data": {"customer_ref": "cus_test_met_b",
                                       "provider_status": "canceled"}})
        assert cancelled.json()["status"] == "applied"
        after = finance.get("/v1/company/metrics").json()["live"]
        assert after["paying_tenants"] == live["paying_tenants"] - 1
        assert after["cancelled_tenants"] == live["cancelled_tenants"] + 1
        assert after["churned_this_month"] == live["churned_this_month"] + 1
        assert after["mrr_minor"] == live["mrr_minor"] - 9900
        assert after["onboarding_complete_pct"] == 100
    finally:
        _teardown(engine_a, [tenant_a["tenant_id"]], issuer_a)
        _teardown(engine_b, [tenant_b["tenant_id"]], issuer_b)
        _cleanup_company(database_url)


def test_metrics_ac2_rebuild_stale_and_no_client_data(database_url):
    """AC-2: rebuilds upsert; old snapshots read stale; no client data anywhere."""
    admin = _bootstrap(database_url)
    _invite(admin, "fin-m2", "finance")
    _invite(admin, "suc-m2", "success")
    finance = _company_client(database_url, "fin-m2")
    success = _company_client(database_url, "suc-m2")
    issuer = "https://identity.example.invalid/met-c"
    engine, owner, tenant = _salon(database_url, issuer)
    staff = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        created = staff.post(f"/v1/tenants/{tid}/clients", json={
            "name": "Private Person",
            "contacts": [{"kind": "email", "value": "private@example.com",
                          "is_primary": True}],
        })
        assert created.status_code == 201, created.text
        first = finance.post("/v1/company/metrics/rebuild", json={
            "period": datetime.now(timezone.utc).strftime("%Y-%m")})
        assert first.status_code == 200, first.text
        count = first.json()["stored"]
        assert count > 5
        second = finance.post("/v1/company/metrics/rebuild", json={
            "period": datetime.now(timezone.utc).strftime("%Y-%m")})
        assert second.json()["stored"] == count
        assert finance.post("/v1/company/metrics/rebuild",
                            json={"period": "2020-01"}).status_code == 422
        assert success.post("/v1/company/metrics/rebuild", json={
            "period": datetime.now(timezone.utc).strftime("%Y-%m")}).status_code == 403
        body = finance.get("/v1/company/metrics").json()
        assert all(s["stale"] is False for s in body["snapshots"])
        blob = json.dumps(body)
        assert "Private Person" not in blob and "private@example.com" not in blob
        with engine.begin() as conn:
            conn.execute(
                text("""UPDATE company_metric_snapshots SET computed_at = now() - interval '3 days'"""))
        stale = finance.get("/v1/company/metrics").json()["snapshots"]
        assert stale and all(s["stale"] is True for s in stale)
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url)
