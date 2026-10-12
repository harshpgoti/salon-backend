from __future__ import annotations

import base64
import hashlib
from urllib.parse import parse_qs, urlparse
import time
import sys
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import modules.identity.main as identity_module
from modules.identity.main import _pack_flow, _unpack_flow, _validate_id_token, create_app


def test_signed_flow_state_is_surface_bound_and_expires(monkeypatch):
    monkeypatch.setenv("AUTH_STATE_SECRET", "synthetic-development-secret-0123456789")
    flow = {"surface": "staff", "state": "opaque-state", "nonce": "opaque-nonce", "verifier": "pkce", "expires_at": int(time.time()) + 60}
    packed = _pack_flow(flow)
    assert _unpack_flow(packed, "staff") == flow
    with pytest.raises(identity_module.HTTPException):
        _unpack_flow(packed, "company")
    with pytest.raises(identity_module.HTTPException):
        _unpack_flow(packed[:-1] + ("0" if packed[-1] != "0" else "1"), "staff")
    expired = _pack_flow({**flow, "expires_at": int(time.time()) - 1})
    with pytest.raises(identity_module.HTTPException):
        _unpack_flow(expired, "staff")


def test_unconfigured_oidc_fails_closed(monkeypatch):
    for name in ("STAFF_OIDC_ISSUER", "STAFF_OIDC_CLIENT_ID", "STAFF_OIDC_AUTHORIZATION_ENDPOINT",
                 "STAFF_OIDC_TOKEN_ENDPOINT", "STAFF_OIDC_JWKS_URL"):
        monkeypatch.delenv(name, raising=False)
    response = TestClient(create_app()).get("/v1/auth/staff/start")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "identity_provider_unconfigured"


def test_id_token_requires_signature_issuer_audience_nonce_and_company_mfa(monkeypatch):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    monkeypatch.setattr(identity_module.jwt, "PyJWKClient", lambda *args, **kwargs: SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=public_key)))
    config = {"jwks_url": "https://issuer.example.invalid/jwks", "issuer": "https://issuer.example.invalid", "client_id": "synthetic-client"}

    def issue(**overrides):
        claims = {"iss": config["issuer"], "aud": config["client_id"], "sub": "synthetic-subject", "iat": int(time.time()),
                  "exp": int(time.time()) + 300, "nonce": "expected-nonce", "token_use": "id", "amr": ["pwd", "mfa"]}
        claims.update(overrides)
        return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "synthetic-key"})

    assert _validate_id_token(issue(), config, "expected-nonce", "staff")["sub"] == "synthetic-subject"
    assert _validate_id_token(issue(), config, "expected-nonce", "company")["amr"] == ["pwd", "mfa"]
    for token, nonce, surface in [(issue(amr=["pwd"]), "expected-nonce", "company"),
                                  (issue(iss="https://wrong.example.invalid"), "expected-nonce", "staff"),
                                  (issue(aud="wrong-client"), "expected-nonce", "staff"),
                                  (issue(), "wrong-nonce", "staff")]:
        with pytest.raises(identity_module.HTTPException):
            _validate_id_token(token, config, nonce, surface)


def test_staff_and_company_oidc_callbacks_create_separate_revocable_sessions(monkeypatch):
    import os
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        pytest.skip("set DATABASE_URL to run PostgreSQL OIDC callback integration")
    monkeypatch.setenv("AUTH_STATE_SECRET", "synthetic-development-secret-0123456789")
    monkeypatch.setenv("API_BASE_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("WEB_BASE_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    monkeypatch.setattr(identity_module.jwt, "PyJWKClient", lambda *args, **kwargs: SimpleNamespace(get_signing_key_from_jwt=lambda token: SimpleNamespace(key=public_key)))
    issued = {"surface": "staff", "amr": ["pwd"]}

    def token_exchange(url, *, data, **kwargs):
        assert data["grant_type"] == "authorization_code"
        assert data["code_verifier"]
        issuer = "https://staff.example.invalid" if issued["surface"] == "staff" else "https://company.example.invalid"
        audience = "staff-client" if issued["surface"] == "staff" else "company-client"
        claims = {"iss": issuer, "aud": audience, "sub": "synthetic-" + issued["surface"], "iat": int(time.time()),
                  "exp": int(time.time()) + 300, "nonce": issued["nonce"], "token_use": "id", "amr": issued["amr"]}
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"id_token": jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "synthetic-key"})})

    monkeypatch.setattr(identity_module.httpx, "post", token_exchange)
    monkeypatch.setenv("STAFF_OIDC_ISSUER", "https://staff.example.invalid")
    monkeypatch.setenv("STAFF_OIDC_CLIENT_ID", "staff-client")
    monkeypatch.setenv("STAFF_OIDC_AUTHORIZATION_ENDPOINT", "https://staff-login.example.invalid/authorize")
    monkeypatch.setenv("STAFF_OIDC_TOKEN_ENDPOINT", "https://staff-login.example.invalid/token")
    monkeypatch.setenv("STAFF_OIDC_JWKS_URL", "https://staff.example.invalid/jwks")
    monkeypatch.setenv("COMPANY_OIDC_ISSUER", "https://company.example.invalid")
    monkeypatch.setenv("COMPANY_OIDC_CLIENT_ID", "company-client")
    monkeypatch.setenv("COMPANY_OIDC_AUTHORIZATION_ENDPOINT", "https://company-login.example.invalid/authorize")
    monkeypatch.setenv("COMPANY_OIDC_TOKEN_ENDPOINT", "https://company-login.example.invalid/token")
    monkeypatch.setenv("COMPANY_OIDC_JWKS_URL", "https://company.example.invalid/jwks")
    app = create_app(engine_factory=lambda: create_engine(database_url, pool_pre_ping=True))
    client = TestClient(app, follow_redirects=False)

    try:
        start = client.get("/v1/auth/staff/start")
        assert start.status_code == 302
        flow_cookie = client.cookies.get("salon_staff_oidc_flow")
        flow = _unpack_flow(flow_cookie, "staff")
        start_query = parse_qs(urlparse(start.headers["location"]).query)
        expected_challenge = base64.urlsafe_b64encode(hashlib.sha256(str(flow["verifier"]).encode()).digest()).rstrip(b"=").decode()
        assert start_query["code_challenge_method"] == ["S256"]
        assert start_query["code_challenge"] == [expected_challenge]
        assert start.headers["set-cookie"].lower().find("httponly") >= 0
        issued.update(surface="staff", nonce=flow["nonce"], amr=["pwd"])
        callback = client.get("/v1/auth/staff/callback", params={"code": "synthetic-code", "state": flow["state"]})
        assert callback.status_code == 303
        assert callback.headers["location"].endswith("/login?surface=staff&status=signed-in")
        staff_token = client.cookies.get("salon_staff_session")
        assert staff_token and "httponly" in callback.headers["set-cookie"].lower()
        assert "secure" not in callback.headers["set-cookie"].lower()

        company_client = TestClient(app, follow_redirects=False)
        issued.update(surface="company", amr=["pwd"])
        start_company = company_client.get("/v1/auth/company/start")
        company_flow = _unpack_flow(company_client.cookies.get("salon_company_oidc_flow"), "company")
        issued["nonce"] = company_flow["nonce"]
        rejected = company_client.get("/v1/auth/company/callback", params={"code": "synthetic-code", "state": company_flow["state"]})
        assert rejected.status_code == 401

        monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
        company_client = TestClient(app, base_url="https://testserver", follow_redirects=False)
        issued.update(surface="company", amr=["pwd", "mfa"])
        company_client.get("/v1/auth/company/start")
        company_flow = _unpack_flow(company_client.cookies.get("salon_company_oidc_flow"), "company")
        issued["nonce"] = company_flow["nonce"]
        company_callback = company_client.get("/v1/auth/company/callback", params={"code": "synthetic-code", "state": company_flow["state"]})
        assert company_callback.status_code == 303
        company_token = company_client.cookies.get("salon_company_session")
        assert company_token and not company_client.cookies.get("salon_staff_session")
        assert "secure" in company_callback.headers["set-cookie"].lower()
        assert company_client.post("/v1/onboarding", json={"business_name":"Denied", "location_name":"Main", "timezone":"America/Toronto"},
                                headers={"Origin": "http://127.0.0.1:3000"}).status_code == 401

        with create_engine(database_url).connect() as connection:
            for token, surface, subject in [(staff_token, "staff", "synthetic-staff"), (company_token, "company", "synthetic-company")]:
                session_hash = hashlib.sha256(token.encode()).hexdigest()
                row = connection.execute(text("SELECT subject,surface FROM auth_sessions WHERE session_hash=:hash"), {"hash": session_hash}).one()
                assert row == (subject, surface)
        check_engine = create_engine(database_url)
        try:
            assert identity_module._session_identity(check_engine, staff_token, "company") is None
        finally:
            check_engine.dispose()
        rejected_origin = client.post("/v1/auth/staff/logout", headers={"Origin": "https://attacker.example.invalid"})
        assert rejected_origin.status_code == 403
        logout = client.post("/v1/auth/staff/logout", headers={"Origin": "http://127.0.0.1:3000"})
        assert logout.status_code == 204
        with create_engine(database_url).connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM auth_sessions WHERE session_hash=:hash"),
                                      {"hash": hashlib.sha256(staff_token.encode()).hexdigest()}).scalar_one() == 0
    finally:
        cleanup_engine = create_engine(database_url)
        with cleanup_engine.begin() as connection:
            connection.execute(text("DELETE FROM auth_sessions WHERE subject IN ('synthetic-staff', 'synthetic-company') AND issuer IN (:staff, :company)"),
                               {"staff": "https://staff.example.invalid", "company": "https://company.example.invalid"})
        cleanup_engine.dispose()
