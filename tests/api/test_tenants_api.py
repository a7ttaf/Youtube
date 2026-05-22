# tests/api/test_tenants_api.py
"""Endpoint tests for GET /tenants/me (Spec A).

Cases 1-9 from Docs/superpowers/specs/2026-05-22-spec-a-frontend-tenant-header-design.md
section 9.1. Fixtures mirror tests/api/test_database_principals.py.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.app import (
    TrustedGatewayTenantResolverMiddleware,
    create_app,
)
from ums_smart_revenue.auth.models import UserPrincipal
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

TEST_USER_ID = UUID("00000000-0000-0000-0000-0000000000aa")
BOOTSTRAP_TENANT_ID = UUID(UMS_TENANT_ID)
BOOTSTRAP_DISPLAY = "UMS"


def _gateway_headers(user_id: UUID = TEST_USER_ID) -> dict[str, str]:
    return {
        "X-User-ID": str(user_id),
        "X-UMS-Trusted-Gateway-Token": os.environ["UMS_TRUSTED_GATEWAY_TOKEN"],
    }


@pytest.fixture
def db_engine_url(tmp_path):
    db_path = tmp_path / "spec_a.sqlite"
    return f"sqlite:///{db_path}"


@pytest.fixture
def seeded_engine(db_engine_url):
    engine = create_engine(db_engine_url, future=True)
    TenantBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as session:
        _seed_bootstrap_tenant(session)
        _seed_enabled_user(session)
        session.commit()
    yield engine
    engine.dispose()


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


SCOPE_ID = UUID("00000000-0000-0000-0000-0000000000b1")
ROLE_KEY = "assistant_analyst"


def _seed_enabled_user(session: Session) -> None:
    """Seed ONE enabled SQL principal so the principal loader can hydrate it.

    Column set reconciled against backend/ums_smart_revenue/db/security_models.py:
    - RoleORM PK is ``key`` (Text), not ``id``; no ``tenant_id``/``status`` columns.
    - UserORM status values are lowercase ('active'); bool field is ``is_service_account``.
    - UserRoleAssignmentORM uses ``role_key``, ``scope_id`` (FK → AccessScopeORM.id),
      and ``active`` (bool); no ``status`` column.
    - AccessScopeORM row is required because the loader joins on ``scope_id``.
    """
    now = datetime.now(UTC)
    role = RoleORM(
        key=ROLE_KEY,
        label="Assistant Analyst",
        description="Assigned-scope analyst for analytics without default finance access.",
        service_only=False,
        created_at=now,
    )
    scope = AccessScopeORM(
        id=SCOPE_ID,
        scope_type="global",
        scope_id=None,
        label="Global",
        created_at=now,
        tenant_id=BOOTSTRAP_TENANT_ID,
    )
    user = UserORM(
        id=TEST_USER_ID,
        tenant_id=BOOTSTRAP_TENANT_ID,
        email="spec-a@example.invalid",
        display_name="Spec A Test User",
        status="active",
        is_service_account=False,
        created_at=now,
        updated_at=now,
    )
    assignment = UserRoleAssignmentORM(
        id=UUID("00000000-0000-0000-0000-0000000000a2"),
        tenant_id=BOOTSTRAP_TENANT_ID,
        user_id=TEST_USER_ID,
        role_key=ROLE_KEY,
        scope_id=SCOPE_ID,
        active=True,
        assigned_at=now,
    )
    session.add_all([role, scope, user, assignment])


@pytest.fixture
def app_db_mode(seeded_engine):
    app = create_app(database_url=str(seeded_engine.url), authz_source="database")
    return app


@pytest.fixture
def client_db_mode(app_db_mode):
    with TestClient(app_db_mode) as client:
        yield client


# ---------------------------------------------------------------------------
# Case 1 — happy path
# ---------------------------------------------------------------------------


def test_tenants_me_returns_bootstrap_tenant_for_resolved_slug(
    client_db_mode,
):
    response = client_db_mode.get(
        "/tenants/me",
        headers={"X-UMS-Tenant": "ums", **_gateway_headers()},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload == {
        "id": str(BOOTSTRAP_TENANT_ID),
        "slug": "ums",
        "display_name": BOOTSTRAP_DISPLAY,
    }
    assert response.headers.get("Vary") and "X-UMS-Tenant" in response.headers["Vary"]


# ---------------------------------------------------------------------------
# Case 2 — missing X-UMS-Tenant header → 400
# ---------------------------------------------------------------------------


def test_tenants_me_rejects_missing_tenant_header(client_db_mode):
    response = client_db_mode.get("/tenants/me", headers=_gateway_headers())
    assert response.status_code == 400
    assert response.json()["detail"] == "Tenant slug must not be blank"
    assert "X-UMS-Tenant" in (response.headers.get("Vary") or "")


# ---------------------------------------------------------------------------
# Case 3 — whitespace slug → 400 / over-255-char slug → 400
# ---------------------------------------------------------------------------


def test_tenants_me_rejects_whitespace_tenant_header(client_db_mode):
    response = client_db_mode.get(
        "/tenants/me",
        headers={"X-UMS-Tenant": "   ", **_gateway_headers()},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Tenant slug must not be blank"


def test_tenants_me_rejects_overlong_tenant_header(client_db_mode):
    response = client_db_mode.get(
        "/tenants/me",
        headers={"X-UMS-Tenant": "a" * 256, **_gateway_headers()},
    )
    assert response.status_code == 400
    assert "at most 255 characters" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Case 4 — duplicate X-UMS-Tenant header → 400
# ---------------------------------------------------------------------------


def test_tenants_me_rejects_duplicate_tenant_headers(client_db_mode):
    request = client_db_mode.build_request(
        "GET",
        "/tenants/me",
        headers=[
            ("X-UMS-Tenant", "ums"),
            ("X-UMS-Tenant", "ums"),
            *_gateway_headers().items(),
        ],
    )
    response = client_db_mode.send(request)
    assert response.status_code == 400
    assert response.json()["detail"] == "X-UMS-Tenant must be provided exactly once"


# ---------------------------------------------------------------------------
# Case 5 — unknown slug → 404
# ---------------------------------------------------------------------------


def test_tenants_me_returns_404_for_unknown_slug(client_db_mode):
    response = client_db_mode.get(
        "/tenants/me",
        headers={"X-UMS-Tenant": "not-a-tenant", **_gateway_headers()},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Tenant 'not-a-tenant' not found"


# ---------------------------------------------------------------------------
# Case 6 — custom-wired app whose authorizer always denies → 403
# ---------------------------------------------------------------------------


@pytest.fixture
def client_deny_authz(seeded_engine):
    """Isolated app whose tenant authorizer denies every resolution."""
    from fastapi import FastAPI
    from sqlalchemy.orm import sessionmaker

    from ums_smart_revenue.api.dependencies import (
        current_db_session,
        current_principal_from_headers,
    )
    from ums_smart_revenue.api.tenants import router as tenants_router
    from ums_smart_revenue.db.session import session_dependency

    app = FastAPI()
    session_factory = sessionmaker(bind=seeded_engine, future=True)
    app.include_router(tenants_router)
    app.dependency_overrides[current_db_session] = session_dependency(session_factory)

    # ============================================================================
    # Purpose: Stub out the principal dependency so the route dependency graph
    #          succeeds and the request reaches the resolver's authorize_tenant
    #          check, isolating the 403 path to the authorizer outcome rather
    #          than a principal-lookup failure (401/503).
    # Database/ORM: None — returns an in-memory UserPrincipal with no SQL read.
    # Standards: Minimal valid construction; only required fields set explicitly.
    # Blast Radius: Test fixture only; no production path affected.
    # ============================================================================
    def _stub_principal() -> UserPrincipal:
        return UserPrincipal(
            user_id=str(TEST_USER_ID),
            email="spec-a-deny@example.invalid",
            tenant_id=str(BOOTSTRAP_TENANT_ID),
        )

    app.dependency_overrides[current_principal_from_headers] = _stub_principal
    app.add_middleware(
        TrustedGatewayTenantResolverMiddleware,
        session_factory=lambda: session_factory(),
        authorize_tenant=lambda *_: False,
    )
    with TestClient(app) as client:
        yield client


def test_tenants_me_returns_403_when_authorizer_denies(client_deny_authz):
    response = client_deny_authz.get(
        "/tenants/me",
        headers={"X-UMS-Tenant": "ums", **_gateway_headers()},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Tenant access denied"
