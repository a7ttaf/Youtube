import os

from fastapi.testclient import TestClient

from ums_smart_revenue.app import create_app
from ums_smart_revenue.config.version_baseline import STACK_VERSION_BASELINE


def test_health_exposes_latest_stable_backend_baseline():
    client = TestClient(create_app())

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "service": "ums-smart-revenue",
        "runtime": {
            "python": STACK_VERSION_BASELINE["runtime"]["python"],
            "fastapi": STACK_VERSION_BASELINE["backend"]["fastapi"],
            "pydantic": STACK_VERSION_BASELINE["backend"]["pydantic"],
        },
    }


def test_livez_exposes_runtime_health_contract():
    client = TestClient(create_app())

    response = client.get("/livez")

    assert response.status_code == 200
    assert response.json() == client.get("/health").json()


def test_security_metadata_endpoints_visible_to_audit_permissioned_principals():
    """The role/permission catalog is readable by audit-permissioned principals.

    Both a super_owner (all permissions) and an audit_viewer (whose only
    permission is audit.view) receive the catalog -- proving the gate is exactly
    the audit-view permission, not super-owner-only.
    """
    client = TestClient(create_app())

    base = {
        "x-user-id": "user-1",
        "x-user-email": "user@example.com",
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": os.environ["UMS_TRUSTED_GATEWAY_TOKEN"],
    }
    for role in ("super_owner", "audit_viewer"):
        headers = {**base, "x-role": role}
        roles = client.get("/security/roles", headers=headers)
        permissions = client.get("/security/permissions", headers=headers)

        assert roles.status_code == 200, role
        assert permissions.status_code == 200, role
        assert any(r["key"] == "finance_admin" for r in roles.json())
        assert any(p["key"] == "finance.view_revenue" for p in permissions.json())


def test_security_metadata_endpoints_forbidden_without_audit_permission():
    """The role/permission catalog (incl. sensitive / auditOnUse flags) is
    auditor-gated, not readable by every authenticated principal.

    A legitimate but non-audit principal (finance_viewer, which lacks
    audit.view) must be refused both endpoints so the authorization model and
    which actions are audited are not disclosed to low-privilege callers.
    """
    client = TestClient(create_app())
    headers = {
        "x-user-id": "viewer-1",
        "x-user-email": "viewer@example.com",
        "x-role": "finance_viewer",
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": os.environ["UMS_TRUSTED_GATEWAY_TOKEN"],
    }
    assert client.get("/security/roles", headers=headers).status_code == 403
    assert client.get("/security/permissions", headers=headers).status_code == 403
