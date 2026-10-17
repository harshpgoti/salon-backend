"""Tests for company health and aggregate projections (TASK-045, FR-063/FR-064).

AC-1: MRR/churn/usage/support/onboarding metrics use documented denominators.
AC-2: No global client data; duplicate events/deletion/backfill reconcile.
"""

import hashlib
import hmac
import json
import os
import sys
import uuid
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


def _company_client(database_url: str, subject: str, issuer: str = COMPANY) -> TestClient:
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    app.dependency_overrides[require_company_identity] = lambda: AuthenticatedIdentity(
        issuer, subject, "company")
    return TestClient(app)


def _invite(admin, subject, role):
    resp = admin.post("/v1/company/staff", json={
        "issuer": COMPANY, "subject": subject, "display_name": subject.title(),
        "role": role, "mfa_enrolled": True})
    assert resp.status_code == 201, resp.text


def _bootstrap(database_url):
    with create_engine(database_url).connect() as conn:
        existing = conn.execute(text("""SELECT issuer, subject FROM company_staff
            WHERE role='admin' AND is_active AND mfa_enrolled ORDER BY created_at LIMIT 1""")).one_or_none()
    if existing:
        return _company_client(database_url, existing.subject, existing.issuer), None
    subject = "admin-test-" + uuid.uuid4().hex
    admin = _company_client(database_url, subject)
    first = admin.post("/v1/company/staff", json={
        "issuer": COMPANY, "subject": subject, "display_name": "Ava Admin",
        "role": "admin", "mfa_enrolled": True})
    assert first.status_code == 201, first.text
    return admin, subject


def _cleanup_company(database_url, subjects):
    if not subjects:
        return
    with create_engine(database_url).begin() as conn:
        params = {"i": COMPANY, "subjects": subjects}
        conn.execute(text("DELETE FROM company_staff WHERE issuer=:i AND subject = ANY(CAST(:subjects AS text[]))"), params)
        conn.execute(text("DELETE FROM identity_users WHERE issuer=:i AND subject = ANY(CAST(:subjects AS text[]))"), params)


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
    subscription = f"sub-{customer}"
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        conn.execute(text("""INSERT INTO billing_provider_facts
            (tenant_id, subscription_ref, customer_ref, provider_status, plan_code,
             period_end, provider_updated_at)
            VALUES (:tenant, :sub, :customer, 'active', 'growth', :period, :at)"""), {
            "tenant": tid, "sub": subscription, "customer": customer,
            "period": now + timedelta(days=30), "at": now})
    event = {"id": f"evt-m-{customer}", "type": "checkout.session.completed",
             "occurred_at": now.isoformat(),
             "data": {"tenant_id": tid, "session_ref": session_ref,
                      "customer_ref": customer, "subscription_ref": subscription,
                      "plan_code": "growth",
                      "period_end": (now + timedelta(days=30)).isoformat()}}
    out = _webhook(client, secret, event)
    assert out.json()["status"] == "applied", out.text
    return event


def test_metrics_ac1_live_numbers_reconcile(database_url):
    """AC-1: MRR, counts, churn and onboarding follow their denominators."""
    admin, bootstrap_subject = _bootstrap(database_url)
    finance_subject = "fin-m-" + uuid.uuid4().hex
    _invite(admin, finance_subject, "finance")
    finance = _company_client(database_url, finance_subject)
    issuer_a = "https://identity.example.invalid/met-a"
    engine_a, owner_a, tenant_a = _salon(database_url, issuer_a)
    client_a = _client(database_url, owner_a)
    issuer_b = "https://identity.example.invalid/met-b"
    engine_b, owner_b, tenant_b = _salon(database_url, issuer_b)
    client_b = _client(database_url, owner_b)
    try:
        secret = _secret(engine_a)
        event = _activate(client_b, engine_b, tenant_b["tenant_id"], secret, "cus_test_met_b")
        assert _webhook(client_b, secret, event).json()["status"] == "duplicate"
        report = finance.get("/v1/company/metrics").json()
        live = report["live"]
        assert report["metric_contract"]["timezone"] == "UTC"
        assert report["metric_contract"]["currency"] == "CAD"
        assert "paid tenant cohort at month start" in report["metric_contract"]["definitions"]["logo_churn_rate_pct"]
        # Relative invariants, not absolute counts: the shared dev database
        # accumulates tenants across runs, so only deltas are stable.
        assert live["paying_tenants"] >= 1
        assert live["trial_tenants"] >= 1
        assert live["mrr_minor"] >= 9900
        assert live["locations"] >= 2
        assert live["paying_tenant_denominator"] >= live["paying_tenants"]
        assert live["logo_churn_available"] is False
        assert live["logo_churn_rate_pct"] is None
        assert live["logo_churn_denominator"] is None
        assert live["onboarding_tenant_denominator"] >= live["onboarding_complete_count"]
        assert live["support_case_denominator"] >= live["support_open"]
        assert live["provider_matched_cost_messages_month"] >= 0
        assert live["provider_usage_pending_month"] >= 0
        with engine_a.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant_a["tenant_id"]})
            conn.execute(text("""INSERT INTO tenant_setup_steps (tenant_id, step, state)
                VALUES (:t, 'business_identity', 'pending')
                ON CONFLICT (tenant_id, step) DO UPDATE SET state='pending', updated_at=now()"""),
                {"t": tenant_a["tenant_id"]})
        pending_setup = finance.get("/v1/company/metrics").json()["live"]
        with engine_a.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant_a["tenant_id"]})
            conn.execute(text("""UPDATE tenant_setup_steps SET state='complete'
                WHERE tenant_id=:t AND step='business_identity'"""),
                {"t": tenant_a["tenant_id"]})
        complete_setup = finance.get("/v1/company/metrics").json()["live"]
        assert complete_setup["onboarding_tenant_denominator"] == pending_setup["onboarding_tenant_denominator"]
        assert complete_setup["onboarding_complete_count"] == pending_setup["onboarding_complete_count"] + 1
        assert complete_setup["onboarding_complete_pct"] == round(
            100 * complete_setup["onboarding_complete_count"]
            / complete_setup["onboarding_tenant_denominator"], 1)
        # Cancelling B churns exactly one payer out of MRR.
        now = datetime.now(timezone.utc)
        with engine_b.begin() as conn:
            conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"),
                         {"t": tenant_b["tenant_id"]})
            conn.execute(text("""UPDATE billing_provider_facts
                SET provider_status='canceled', provider_updated_at=:at
                WHERE tenant_id=:tenant AND customer_ref=:customer"""), {
                "at": now, "tenant": tenant_b["tenant_id"], "customer": "cus_test_met_b"})
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
        assert after["onboarding_complete_pct"] == complete_setup["onboarding_complete_pct"]
    finally:
        _teardown(engine_a, [tenant_a["tenant_id"]], issuer_a)
        _teardown(engine_b, [tenant_b["tenant_id"]], issuer_b)
        _cleanup_company(database_url, [s for s in (bootstrap_subject, finance_subject) if s])


def test_metrics_ac2_rebuild_stale_and_no_client_data(database_url):
    """AC-2: rebuilds upsert; old snapshots read stale; no client data anywhere."""
    admin, bootstrap_subject = _bootstrap(database_url)
    finance_subject = "fin-m2-" + uuid.uuid4().hex
    success_subject = "suc-m2-" + uuid.uuid4().hex
    _invite(admin, finance_subject, "finance")
    _invite(admin, success_subject, "success")
    finance = _company_client(database_url, finance_subject)
    success = _company_client(database_url, success_subject)
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
        support_case = admin.post("/v1/company/cases", json={
            "tenant_id": tid, "title": "Synthetic metric fixture", "severity": "low"})
        assert support_case.status_code == 201, support_case.text
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
        assert success.get("/v1/company/metrics").status_code == 403
        body = finance.get("/v1/company/metrics").json()
        assert all(s["stale"] is False for s in body["snapshots"])
        support_before_delete = body["live"]["support_open"]
        blob = json.dumps(body)
        assert "Private Person" not in blob and "private@example.com" not in blob
        period = datetime.now(timezone.utc).strftime("%Y-%m")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM support_cases WHERE id=:id"),
                         {"id": support_case.json()["id"]})
            conn.execute(text("""INSERT INTO company_metric_snapshots
                (metric, period, value_num, value_json, source)
                VALUES ('removed_source_metric', :period, 999, :payload, 'live')
                ON CONFLICT (metric, period) DO UPDATE SET value_num=999"""),
                {"period": period, "payload": json.dumps({"value": 999})})
        rebuilt = finance.post("/v1/company/metrics/rebuild", json={"period": period})
        assert rebuilt.status_code == 200, rebuilt.text
        corrected = finance.get("/v1/company/metrics").json()
        assert all(row["metric"] != "removed_source_metric" for row in corrected["snapshots"])
        support_snapshot = next(row for row in corrected["snapshots"]
                                if row["metric"] == "support_open")
        assert support_snapshot["value_num"] == support_before_delete - 1
        assert support_snapshot["stale"] is False
        with engine.begin() as conn:
            conn.execute(
                text("""UPDATE company_metric_snapshots SET computed_at = now() - interval '3 days'"""))
        stale = finance.get("/v1/company/metrics").json()["snapshots"]
        assert stale and all(s["stale"] is True for s in stale)
    finally:
        _teardown(engine, [tid], issuer)
        _cleanup_company(database_url, [s for s in (bootstrap_subject, finance_subject, success_subject) if s])
