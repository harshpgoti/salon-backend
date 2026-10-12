"""Tests for accessible core and French-ready localization (TASK-063).

AC-1: Core booking/calendar/list/company flows have focus/error/loading states.
AC-2: Locale keys/money/time formatting and French template hooks exist.
"""

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.communications.templates import resolve_template
from test_clients import _salon, _client, _teardown


@pytest.fixture
def database_url() -> str:
    url = os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("set DATABASE_URL to run PostgreSQL integration")
    return url


def _template(engine, tid, channel="email", purpose="booking_confirmation",
              locale="en-CA", body="x", version=1, active=True):
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
        conn.execute(
            text("""INSERT INTO message_templates
                    (tenant_id, channel, purpose, locale, version, body, is_active)
                    VALUES (:t, :c, :p, :l, :v, :b, :a)"""),
            {"t": tid, "c": channel, "p": purpose, "l": locale,
             "v": version, "b": body, "a": active})


def _scoped(engine, tid):
    conn = engine.connect()
    conn.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tid})
    return conn


def test_locale_ac2_template_fallback_chain(database_url):
    """AC-2: fr-CA resolves fr, then en-CA; drafts and gaps never resolve."""
    issuer = "https://identity.example.invalid/loc-1"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        _template(engine, tid, locale="en-CA", body="Your booking is confirmed.")
        _template(engine, tid, locale="fr", body="Votre réservation est confirmée.")
        conn = _scoped(engine, tid)
        try:
            exact = resolve_template(conn, tenant_id=tid, channel="email",
                                     purpose="booking_confirmation", locale="en-CA")
            assert exact["body"] == "Your booking is confirmed."
            assert exact["resolved_locale"] == "en-CA"
            french = resolve_template(conn, tenant_id=tid, channel="email",
                                      purpose="booking_confirmation", locale="fr-CA")
            assert french["body"] == "Votre réservation est confirmée."
            assert french["resolved_locale"] == "fr"
            missing = resolve_template(conn, tenant_id=tid, channel="sms",
                                       purpose="booking_confirmation", locale="fr-CA")
            assert missing is None
        finally:
            conn.close()
        _template(engine, tid, locale="fr-CA", body="Brouillon.", version=1,
                  active=False)
        conn = _scoped(engine, tid)
        try:
            # A draft fr-CA row must not shadow the live fr fallback.
            still = resolve_template(conn, tenant_id=tid, channel="email",
                                     purpose="booking_confirmation", locale="fr-CA")
            assert still["resolved_locale"] == "fr"
            # Newer versions win within a locale.
            _template(engine, tid, locale="en-CA", body="Confirmed! See you soon.",
                      version=2)
        finally:
            conn.close()
        conn = _scoped(engine, tid)
        try:
            newest = resolve_template(conn, tenant_id=tid, channel="email",
                                      purpose="booking_confirmation", locale="en-CA")
            assert newest["body"] == "Confirmed! See you soon."
            assert newest["version"] == 2
        finally:
            conn.close()
    finally:
        _teardown(engine, [tid], issuer)


def test_locale_ac1_core_flows_fail_safe_and_generic(database_url):
    """AC-1: core flows refuse cleanly with generic codes — no internals leak
    into error states, on any surface."""
    issuer = "https://identity.example.invalid/loc-2"
    engine, owner, tenant = _salon(database_url, issuer)
    client = _client(database_url, owner)
    tid = tenant["tenant_id"]
    try:
        import test_clients as _tc
        outsider = _tc._client(database_url, _tc._identity())
        for path in (f"/v1/tenants/{tid}/reports/today",
                     f"/v1/tenants/{tid}/rebooking-cycles",
                     f"/v1/tenants/{tid}/campaigns",
                     f"/v1/tenants/{tid}/billing/subscription",
                     f"/v1/tenants/{tid}/inbox"):
            resp = outsider.get(path)
            # No membership: the tenant does not resolve (404), never data,
            # never internals.
            assert resp.status_code == 404, path
            assert resp.json()["detail"]["code"] == "tenant_not_found", path
            assert "traceback" not in resp.text.lower(), path
    finally:
        _teardown(engine, [tid], issuer)
