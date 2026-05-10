from fastapi.testclient import TestClient

from ums_smart_revenue.app import create_app


def test_health_exposes_latest_stable_backend_baseline():
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ums-smart-revenue",
        "runtime": {
            "python": "3.14.4",
            "fastapi": "0.136.1",
            "pydantic": "2.13.4",
        },
    }


def test_security_metadata_endpoints_are_available_for_frontend():
    client = TestClient(create_app())

    headers = {
        "x-user-id": "user-1",
        "x-user-email": "user@example.com",
        "x-role": "super_owner",
        "x-scope-type": "global",
    }
    roles = client.get("/security/roles", headers=headers)
    permissions = client.get("/security/permissions", headers=headers)

    assert roles.status_code == 200
    assert permissions.status_code == 200
    assert any(role["key"] == "finance_admin" for role in roles.json())
    assert any(permission["key"] == "finance.view_revenue" for permission in permissions.json())

