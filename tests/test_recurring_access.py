"""Recurring series routes remain authenticated and validate before tenant access."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from modules.identity.main import AuthenticatedIdentity, create_app, require_identity

TENANT = "11111111-1111-4111-8111-111111111111"
SERIES = "22222222-2222-4222-8222-222222222222"
PATHS = [f"/v1/tenants/{TENANT}/recurring-series/preview",
         f"/v1/tenants/{TENANT}/recurring-series",
         f"/v1/tenants/{TENANT}/recurring-series/{SERIES}/cancel"]

@pytest.mark.parametrize("path", PATHS)
def test_recurring_routes_require_identity_before_any_tenant_access(path):
    response = TestClient(create_app(), raise_server_exceptions=False).post(path, json={})
    assert response.status_code == 401

@pytest.mark.parametrize("path", PATHS)
def test_recurring_series_rejects_invalid_payload_before_tenant_access(path):
    app = create_app()
    app.dependency_overrides[require_identity] = lambda: AuthenticatedIdentity(
        "https://identity.example.invalid/recurring-access", "synthetic-owner")
    response = TestClient(app, raise_server_exceptions=False).post(path, json={})
    assert response.status_code == 422
