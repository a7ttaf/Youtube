"""Endpoint tests for GET /session/me (production session hydration).

Header-mode cases exercise the trusted-gateway principal + capability
derivation; database-mode cases exercise the SQL-backed enriched principal
plus fail-closed behavior (disabled/unknown). Fixtures mirror
tests/api/test_tenants_api.py and tests/api/test_database_principals.py.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    RoleORM,
    SecurityBase,
    UserORM,
    UserRoleAssignmentORM,
)
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.models import TenantStatus

BOOTSTRAP_TENANT_ID = UUID(UMS_TENANT_ID)
BOOTSTRAP_DISPLAY = "UMS"

FINANCE_ADMIN_ID = UUID("00000000-0000-0000-0000-0000000000f1")
CONNECTOR_OPS_ID = UUID("00000000-0000-0000-0000-0000000000c1")
DISABLED_USER_ID = UUID("00000000-0000-0000-0000-0000000000d1")
UNKNOWN_USER_ID = UUID("00000000-0000-0000-0000-0000000000e1")

# A single global AccessScope per tenant (DB enforces uniqueness on
# (tenant_id, scope_type) for global scopes), shared by every seeded user.
GLOBAL_SCOPE_ID = UUID("00000000-0000-0000-0000-0000000000a2")


def _token() -> str:
    return os.environ["UMS_TRUSTED_GATEWAY_TOKEN"]


# ---------------------------------------------------------------------------
# Header-mode helpers
# ---------------------------------------------------------------------------


def _header_principal(
    *,
    role: str,
    user_id: UUID = FINANCE_ADMIN_ID,
    email: str = "session@example.invalid",
    include_token: bool = True,
    include_identity: bool = True,
) -> dict[str, str]:
    """Build the full trusted-gateway header set for headers-mode auth.

    current_principal_from_headers (active in headers mode) requires
    X-User-ID, X-User-Email, X-Role, X-Scope-Type, and the gateway token.
    X-Scope-Id is omitted because X-Scope-Type="global" forbids a scope_id.
    """
    headers: dict[str, str] = {}
    if include_identity:
        headers.update(
            {
                "X-User-ID": str(user_id),
                "X-User-Email": email,
                "X-Role": role,
                "X-Scope-Type": "global",
            }
        )
    if include_token:
        headers["X-UMS-Trusted-Gateway-Token"] = _token()
    return headers


@pytest.fixture
def client_headers_mode():
    """Headers-mode app with no database_url (no tenant middleware installed)."""
    app = create_app(database_url=None, authz_source="headers")
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Header mode — finance_admin: revenue capabilities yes, connector jobs NO
# ---------------------------------------------------------------------------


def test_session_me_header_mode_finance_admin_capabilities(client_headers_mode):
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(role="finance_admin"),
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["user_id"] == str(FINANCE_ADMIN_ID)
    assert payload["email"] == "session@example.invalid"
    assert payload["is_service_account"] is False
    assert payload["disabled"] is False
    # No tenant middleware in headers/no-db mode -> tenant is optional/null.
    assert payload["tenant"] is None

    caps = payload["capabilities"]
    assert caps["canViewRevenue"] is True
    assert caps["canViewConfidence"] is True
    assert caps["canViewPayments"] is True
    assert caps["canViewBankReconciliation"] is True
    assert caps["canCloseMonth"] is True
    assert caps["canUnlockMonth"] is True
    assert caps["canChangeAllocation"] is True
    assert caps["canExportRevenue"] is True
    assert caps["canViewAudit"] is True
    # finance_admin lacks connector permissions in ROLE_PERMISSIONS (seed.py).
    assert caps["canRunConnectorJobs"] is False
    assert caps["canManageConnectors"] is False

    assert payload["roles"] == [
        {"role": "finance_admin", "scope_type": "global", "scope_id": None}
    ]
    assert payload["permissions"] == []

    assert response.headers.get("Cache-Control") == "no-store"
    assert response.headers.get("Vary") and "Authorization" in response.headers["Vary"]


# ---------------------------------------------------------------------------
# Header mode — connector/ops role: canRunConnectorJobs TRUE
# ---------------------------------------------------------------------------


def test_session_me_header_mode_revenue_ops_admin_can_run_connector_jobs(
    client_headers_mode,
):
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(
            role="revenue_operations_admin", user_id=CONNECTOR_OPS_ID
        ),
    )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    assert caps["canRunConnectorJobs"] is True
    # revenue_operations_admin does NOT manage connectors, only runs jobs.
    assert caps["canManageConnectors"] is False
    # ...and has no finance revenue visibility.
    assert caps["canViewRevenue"] is False


def test_session_me_header_mode_connector_admin_manages_and_runs(client_headers_mode):
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(role="connector_admin", user_id=CONNECTOR_OPS_ID),
    )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    assert caps["canRunConnectorJobs"] is True
    assert caps["canManageConnectors"] is True


# ---------------------------------------------------------------------------
# Header mode — fail-closed: missing token / missing identity headers
# ---------------------------------------------------------------------------


def test_session_me_header_mode_missing_token_unauthorized(client_headers_mode):
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(role="finance_admin", include_token=False),
    )
    assert response.status_code == 401


def test_session_me_header_mode_missing_identity_headers_unauthorized(
    client_headers_mode,
):
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(role="finance_admin", include_identity=False),
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Missing authentication headers"


# ---------------------------------------------------------------------------
# Database-mode fixtures
# ---------------------------------------------------------------------------


def _gateway_headers(user_id: UUID) -> dict[str, str]:
    """Database-mode only needs X-User-ID + token (principal loaded from SQL)."""
    return {
        "X-User-ID": str(user_id),
        "X-UMS-Tenant": "ums",
        "X-UMS-Trusted-Gateway-Token": _token(),
    }


def _build_database_url(tmp_path) -> str:
    """Return a unique isolated SQLite URL (mirrors sibling DB-mode API tests)."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def _seed_bootstrap_tenant(session: Session) -> None:
    now = datetime.now(UTC)
    session.add(
        TenantORM(
            id=BOOTSTRAP_TENANT_ID,
            slug="ums",
            display_name=BOOTSTRAP_DISPLAY,
            primary_currency="USD",
            status=TenantStatus.ACTIVE.value,
            onboarding_at=now,
            created_at=now,
            updated_at=now,
        )
    )


def _seed_role(session: Session, key: str, label: str) -> None:
    session.add(
        RoleORM(
            key=key,
            label=label,
            description=label,
            service_only=False,
            created_at=datetime.now(UTC),
        )
    )


def _seed_global_scope(session: Session, scope_id: UUID) -> None:
    session.add(
        AccessScopeORM(
            id=scope_id,
            scope_type="global",
            scope_id=None,
            label="Global",
            created_at=datetime.now(UTC),
            tenant_id=BOOTSTRAP_TENANT_ID,
        )
    )


def _seed_user_with_role(
    session: Session,
    *,
    user_id: UUID,
    email: str,
    role_key: str,
    scope_id: UUID,
    status: str = "active",
) -> None:
    now = datetime.now(UTC)
    session.add(
        UserORM(
            id=user_id,
            tenant_id=BOOTSTRAP_TENANT_ID,
            email=email,
            display_name=email,
            status=status,
            is_service_account=False,
            created_at=now,
            updated_at=now,
        )
    )
    session.add(
        UserRoleAssignmentORM(
            id=uuid4(),
            tenant_id=BOOTSTRAP_TENANT_ID,
            user_id=user_id,
            role_key=role_key,
            scope_id=scope_id,
            active=True,
            assigned_at=now,
        )
    )


@pytest.fixture
def seeded_db_engine(tmp_path):
    """Isolated SQLite engine seeded with finance_admin, connector-ops, disabled."""
    engine = create_engine(_build_database_url(tmp_path), future=True)
    TenantBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, future=True)
    with factory() as session:
        _seed_bootstrap_tenant(session)
        _seed_role(session, "finance_admin", "Finance Admin")
        _seed_role(session, "revenue_operations_admin", "Revenue Operations Admin")
        _seed_global_scope(session, GLOBAL_SCOPE_ID)
        _seed_user_with_role(
            session,
            user_id=FINANCE_ADMIN_ID,
            email="finance-admin@example.invalid",
            role_key="finance_admin",
            scope_id=GLOBAL_SCOPE_ID,
        )
        _seed_user_with_role(
            session,
            user_id=CONNECTOR_OPS_ID,
            email="connector-ops@example.invalid",
            role_key="revenue_operations_admin",
            scope_id=GLOBAL_SCOPE_ID,
        )
        _seed_user_with_role(
            session,
            user_id=DISABLED_USER_ID,
            email="disabled@example.invalid",
            role_key="finance_admin",
            scope_id=GLOBAL_SCOPE_ID,
            status="disabled",
        )
        session.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def client_db_mode(seeded_db_engine):
    app = create_app(database_url=str(seeded_db_engine.url), authz_source="database")
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Database mode — enriched finance_admin: capabilities from SQL grants
# ---------------------------------------------------------------------------


def test_session_me_db_mode_finance_admin_capabilities(client_db_mode):
    response = client_db_mode.get(
        "/session/me",
        headers=_gateway_headers(FINANCE_ADMIN_ID),
    )
    assert response.status_code == 200, response.text
    payload = response.json()

    assert payload["user_id"] == str(FINANCE_ADMIN_ID)
    assert payload["email"] == "finance-admin@example.invalid"
    # Database mode resolves the real tenant via middleware.
    assert payload["tenant"] == {
        "id": str(BOOTSTRAP_TENANT_ID),
        "slug": "ums",
        "display_name": BOOTSTRAP_DISPLAY,
    }

    caps = payload["capabilities"]
    assert caps["canViewRevenue"] is True
    assert caps["canCloseMonth"] is True
    assert caps["canChangeAllocation"] is True
    assert caps["canExportRevenue"] is True
    assert caps["canViewAudit"] is True
    assert caps["canRunConnectorJobs"] is False
    assert caps["canManageConnectors"] is False

    assert payload["roles"] == [
        {"role": "finance_admin", "scope_type": "global", "scope_id": None}
    ]
    assert response.headers.get("Cache-Control") == "no-store"


# ---------------------------------------------------------------------------
# Database mode — connector/ops role: canRunConnectorJobs TRUE
# ---------------------------------------------------------------------------


def test_session_me_db_mode_revenue_ops_admin_can_run_connector_jobs(client_db_mode):
    response = client_db_mode.get(
        "/session/me",
        headers=_gateway_headers(CONNECTOR_OPS_ID),
    )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    assert caps["canRunConnectorJobs"] is True
    assert caps["canViewRevenue"] is False


# ---------------------------------------------------------------------------
# Database mode — fail-closed: disabled user (403), unknown user (403)
# ---------------------------------------------------------------------------


def test_session_me_db_mode_disabled_user_forbidden(client_db_mode):
    response = client_db_mode.get(
        "/session/me",
        headers=_gateway_headers(DISABLED_USER_ID),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"


def test_session_me_db_mode_unknown_user_forbidden(client_db_mode):
    response = client_db_mode.get(
        "/session/me",
        headers=_gateway_headers(UNKNOWN_USER_ID),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"
