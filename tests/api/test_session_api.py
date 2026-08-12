"""Endpoint tests for GET /session/me (production session hydration).

Header-mode cases exercise the trusted-gateway principal + capability
derivation; database-mode cases exercise the SQL-backed enriched principal
plus fail-closed behavior (disabled/unknown). Fixtures mirror
tests/api/test_tenants_api.py and tests/api/test_database_principals.py.
"""
# pylint: disable=redefined-outer-name, too-many-arguments

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.api.session import _derive_capabilities
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
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
    """Return the trusted-gateway token from the environment."""
    return os.environ["UMS_TRUSTED_GATEWAY_TOKEN"]


# ---------------------------------------------------------------------------
# Header-mode helpers
# ---------------------------------------------------------------------------


def _header_principal(
    *,
    role: str,
    user_id: UUID = FINANCE_ADMIN_ID,
    email: str = "session@example.invalid",
    scope_type: str = "global",
    scope_id: str | None = None,
    include_token: bool = True,
    include_identity: bool = True,
) -> dict[str, str]:
    """Build the full trusted-gateway header set for headers-mode auth.

    current_principal_from_headers (active in headers mode) requires
    X-User-ID, X-User-Email, X-Role, X-Scope-Type, and the gateway token.
    X-Scope-Id is included when the requested scope type needs one.
    """
    headers: dict[str, str] = {}
    if include_identity:
        headers.update(
            {
                "X-User-ID": str(user_id),
                "X-User-Email": email,
                "X-Role": role,
                "X-Scope-Type": scope_type,
            }
        )
        if scope_id is not None:
            headers["X-Scope-Id"] = scope_id
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
    """finance_admin: revenue caps true, connector caps false, cache headers set."""
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
    # finance_admin also holds EXPORT_ANALYTICS_REPORT (distinct from revenue).
    assert caps["canExportAnalyticsReports"] is True
    # finance_admin holds VIEW_ANALYTICS at global scope.
    assert caps["canViewAnalytics"] is True
    # finance_admin holds VIEW_REVENUE but NOT MANAGE_CHANNELS (disjoint role sets).
    assert caps["canManageRegistry"] is False
    # finance_admin holds neither MANAGE_CHANNELS nor MANAGE_GROUPS.
    assert caps["canImportChannels"] is False
    assert caps["canViewAudit"] is True
    # finance_admin lacks connector permissions in ROLE_PERMISSIONS (seed.py).
    assert caps["canRunConnectorJobs"] is False
    assert caps["canManageConnectors"] is False

    assert payload["roles"] == [{"role": "finance_admin", "scope_type": "global", "scope_id": None}]
    assert payload["permissions"] == []

    assert response.headers.get("Cache-Control") == "no-store"
    assert response.headers.get("Vary") and "Authorization" in response.headers["Vary"]


def test_session_me_header_mode_finance_month_scope_has_bank_capabilities(
    client_headers_mode,
):
    """A finance_month-scoped finance_admin still gets payment/bank UI hints."""
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(
            role="finance_admin",
            scope_type="finance-month",
            scope_id="2026-03",
        ),
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    caps = payload["capabilities"]
    assert caps["canViewPayments"] is True
    assert caps["canViewBankReconciliation"] is True
    # Org-data capabilities stay false because finance-month is not a channel,
    # company, sector, or global scope.
    assert caps["canViewRevenue"] is False
    assert caps["canExportAnalyticsReports"] is False
    assert payload["roles"] == [
        {"role": "finance_admin", "scope_type": "finance-month", "scope_id": "2026-03"}
    ]


def test_session_me_header_mode_company_scoped_finance_admin_gets_csv_hints(
    client_headers_mode,
):
    """A company-scoped finance_admin can render scoped analytics CSV controls."""
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(
            role="finance_admin",
            scope_type="company",
            scope_id="acme",
        ),
    )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    assert caps["canViewRevenue"] is True
    assert caps["canExportAnalyticsReports"] is True
    # Revenue workbook exports stay global-only in this session hint.
    assert caps["canExportRevenue"] is False


def test_session_me_header_mode_revenue_ops_admin_can_run_connector_jobs(
    client_headers_mode,
):
    """revenue_operations_admin: canRunConnectorJobs true, canViewRevenue false."""
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(role="revenue_operations_admin", user_id=CONNECTOR_OPS_ID),
    )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    assert caps["canRunConnectorJobs"] is True
    # revenue_operations_admin does NOT manage connectors, only runs jobs.
    assert caps["canManageConnectors"] is False
    # ...and has no finance revenue visibility.
    assert caps["canViewRevenue"] is False
    assert caps["canExportRevenue"] is False
    # revenue_operations_admin DOES hold EXPORT_ANALYTICS_REPORT — must not
    # collapse to canExportRevenue=False (the two permissions are distinct).
    assert caps["canExportAnalyticsReports"] is True
    # revenue_operations_admin holds MANAGE_ORG_MAPPING (and MANAGE_CHANNELS);
    # the capability gates the live Map/Assign routes that require MANAGE_ORG_MAPPING.
    assert caps["canManageRegistry"] is True


def test_session_me_header_mode_revenue_ops_admin_can_manage_groups(client_headers_mode):
    """revenue_operations_admin holds MANAGE_GROUPS: canManageGroups true."""
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(role="revenue_operations_admin", user_id=CONNECTOR_OPS_ID),
    )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    assert caps["canManageGroups"] is True
    # ...and MANAGE_CHANNELS, so the both-permission import hint is true too.
    assert caps["canImportChannels"] is True


def test_session_me_header_mode_finance_admin_cannot_manage_groups(client_headers_mode):
    """finance_admin lacks MANAGE_GROUPS: canManageGroups false."""
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(role="finance_admin"),
    )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    assert caps["canManageGroups"] is False


def test_session_me_header_mode_finance_approver_analytics_false_revenue_true(
    client_headers_mode,
):
    """finance_approver: canExportRevenue true but canExportAnalyticsReports false."""
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(role="finance_approver"),
    )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    # finance_approver holds EXPORT_REVENUE_REPORT but NOT EXPORT_ANALYTICS_REPORT.
    assert caps["canExportRevenue"] is True
    assert caps["canExportAnalyticsReports"] is False


def test_session_me_header_mode_connector_admin_manages_and_runs(client_headers_mode):
    """connector_admin: both canRunConnectorJobs and canManageConnectors true."""
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(role="connector_admin", user_id=CONNECTOR_OPS_ID),
    )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    assert caps["canRunConnectorJobs"] is True
    assert caps["canManageConnectors"] is True
    assert caps["canViewConnectorHealth"] is True


def test_session_me_header_mode_connector_scoped_connector_admin_can_view_health(
    client_headers_mode,
):
    """Connector-scoped connector_admin still exposes the run-history panel."""
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(
            role="connector_admin",
            user_id=CONNECTOR_OPS_ID,
            scope_type="connector",
            scope_id="youtube_reporting",
        ),
    )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    assert caps["canViewConnectorHealth"] is True


# ---------------------------------------------------------------------------
# Header mode — canViewAnalytics is scope-aware: a company-scoped analytics
# role sees it true; a role without VIEW_ANALYTICS sees it false.
# ---------------------------------------------------------------------------


def test_session_me_header_mode_company_scoped_manager_can_view_analytics(
    client_headers_mode,
):
    """A company-scoped company_manager exposes canViewAnalytics true (scope-aware)."""
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(
            role="company_manager",
            scope_type="company",
            scope_id="acme",
        ),
    )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    # company_manager holds VIEW_ANALYTICS only at company scope; a global-only
    # check would wrongly hide the analytics panel from this legitimate user.
    assert caps["canViewAnalytics"] is True


def test_session_me_header_mode_audit_viewer_cannot_view_analytics(
    client_headers_mode,
):
    """audit_viewer holds no VIEW_ANALYTICS grant at any scope → canViewAnalytics false."""
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(role="audit_viewer"),
    )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    assert caps["canViewAnalytics"] is False


# ---------------------------------------------------------------------------
# Capability derivation — canImportChannels needs MANAGE_CHANNELS AND
# MANAGE_GROUPS (POST /channels/import gates on both once a roster carries
# Group_ID). No seeded role holds either permission alone, so the each-alone
# cases build role-less principals from direct global-scope grants.
# ---------------------------------------------------------------------------


def _direct_grant_principal(*permissions: Permission) -> UserPrincipal:
    """Build a role-less principal holding *permissions* as direct global grants."""
    return UserPrincipal(
        user_id=str(FINANCE_ADMIN_ID),
        email="direct-grants@example.invalid",
        direct_permissions=tuple(
            PermissionGrant(permission=permission, scope=AccessScope.global_scope())
            for permission in permissions
        ),
    )


def test_derive_capabilities_import_channels_requires_both_permissions():
    """MANAGE_CHANNELS + MANAGE_GROUPS together turn the import hint on."""
    capabilities = _derive_capabilities(
        _direct_grant_principal(Permission.MANAGE_CHANNELS, Permission.MANAGE_GROUPS)
    )
    assert capabilities.can_import_channels is True


def test_derive_capabilities_import_channels_channels_alone_insufficient():
    """MANAGE_CHANNELS alone stays false: group-bearing rosters would 403 mid-flow."""
    capabilities = _derive_capabilities(_direct_grant_principal(Permission.MANAGE_CHANNELS))
    assert capabilities.can_import_channels is False


def test_derive_capabilities_import_channels_groups_alone_insufficient():
    """MANAGE_GROUPS alone stays false: the route always requires MANAGE_CHANNELS."""
    capabilities = _derive_capabilities(_direct_grant_principal(Permission.MANAGE_GROUPS))
    assert capabilities.can_import_channels is False


# ---------------------------------------------------------------------------
# Header mode — fail-closed: missing token / missing identity headers
# ---------------------------------------------------------------------------


def test_session_me_header_mode_missing_token_unauthorized(client_headers_mode):
    """Omitting the gateway token must return 401 (fail closed)."""
    response = client_headers_mode.get(
        "/session/me",
        headers=_header_principal(role="finance_admin", include_token=False),
    )
    assert response.status_code == 401


def test_session_me_header_mode_missing_identity_headers_unauthorized(
    client_headers_mode,
):
    """Omitting identity headers must return 401 with the "Missing..." detail."""
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
    """Seed the bootstrap UMS tenant record into *session*."""
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
    """Seed a RoleORM row with *key* and *label* into *session*."""
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
    """Seed a global-type AccessScopeORM for the bootstrap tenant into *session*."""
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
    """Seed a UserORM + active UserRoleAssignmentORM for *user_id* into *session*."""
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
    """Database-mode test client wired to the pre-seeded SQLite engine."""
    app = create_app(database_url=str(seeded_db_engine.url), authz_source="database")
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Database mode — enriched finance_admin: capabilities from SQL grants
# ---------------------------------------------------------------------------


def test_session_me_db_mode_finance_admin_capabilities(client_db_mode):
    """DB mode: finance_admin capabilities derived from SQL grants; tenant resolved."""
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
    assert caps["canExportAnalyticsReports"] is True
    assert caps["canManageRegistry"] is False
    # finance_admin holds neither MANAGE_CHANNELS nor MANAGE_GROUPS.
    assert caps["canImportChannels"] is False
    assert caps["canViewAudit"] is True
    assert caps["canRunConnectorJobs"] is False
    assert caps["canManageConnectors"] is False

    assert payload["roles"] == [{"role": "finance_admin", "scope_type": "global", "scope_id": None}]
    assert response.headers.get("Cache-Control") == "no-store"


# ---------------------------------------------------------------------------
# Database mode — connector/ops role: canRunConnectorJobs TRUE
# ---------------------------------------------------------------------------


def test_session_me_db_mode_revenue_ops_admin_can_run_connector_jobs(client_db_mode):
    """DB mode: revenue_operations_admin canRunConnectorJobs true, canViewRevenue false."""
    response = client_db_mode.get(
        "/session/me",
        headers=_gateway_headers(CONNECTOR_OPS_ID),
    )
    assert response.status_code == 200, response.text
    caps = response.json()["capabilities"]
    assert caps["canRunConnectorJobs"] is True
    assert caps["canViewRevenue"] is False
    # revenue_operations_admin holds EXPORT_ANALYTICS_REPORT but not EXPORT_REVENUE_REPORT.
    assert caps["canExportAnalyticsReports"] is True
    assert caps["canExportRevenue"] is False
    # revenue_operations_admin holds both MANAGE_CHANNELS and MANAGE_GROUPS.
    assert caps["canImportChannels"] is True


# ---------------------------------------------------------------------------
# Database mode — fail-closed: disabled user (403), unknown user (403)
# ---------------------------------------------------------------------------


def test_session_me_db_mode_disabled_user_forbidden(client_db_mode):
    """DB mode: a disabled user must receive 403 Forbidden (fail closed)."""
    response = client_db_mode.get(
        "/session/me",
        headers=_gateway_headers(DISABLED_USER_ID),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"


def test_session_me_db_mode_unknown_user_forbidden(client_db_mode):
    """DB mode: an unknown user_id must receive 403 Forbidden (fail closed)."""
    response = client_db_mode.get(
        "/session/me",
        headers=_gateway_headers(UNKNOWN_USER_ID),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Forbidden"
