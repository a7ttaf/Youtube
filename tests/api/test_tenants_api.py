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
from ums_smart_revenue.config.settings import (
    AUTHZ_SOURCE_ENV,
    TENANT_PRIMARY_CURRENCY_ENV,
    load_app_settings,
)
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
    """Return the minimal trusted-gateway headers used by database-mode tests."""
    return {
        "X-User-ID": str(user_id),
        "X-UMS-Trusted-Gateway-Token": os.environ["UMS_TRUSTED_GATEWAY_TOKEN"],
    }


def _full_principal_headers(user_id: UUID = TEST_USER_ID) -> dict[str, str]:
    """Return the full set of trusted-gateway headers required by
    current_principal_from_headers in authz_source="headers" mode.

    In database mode current_principal_from_headers is overridden to
    current_principal_from_database, which only needs X-User-ID + token.
    In headers mode the raw dependency is active and requires all five fields:
    X-User-ID, X-User-Email, X-Role, X-Scope-Type, and the gateway token.
    X-Scope-Id is omitted because X-Scope-Type="global" must not have a scope_id.
    """
    return {
        "X-User-ID": str(user_id),
        "X-User-Email": "spec-a@example.invalid",
        "X-Role": ROLE_KEY,
        "X-Scope-Type": "global",
        "X-UMS-Trusted-Gateway-Token": os.environ["UMS_TRUSTED_GATEWAY_TOKEN"],
    }


@pytest.fixture
def db_engine_url(tmp_path):
    """Return an isolated SQLite URL for the tenant API fixtures."""
    db_path = tmp_path / "spec_a.sqlite"
    return f"sqlite:///{db_path}"


@pytest.fixture
def seeded_engine(db_engine_url):
    """Create and seed an SQLite database for tenant API integration tests."""
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
    """Insert the active bootstrap tenant used by resolver tests."""
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
    """Build the database-auth app against the seeded SQLite engine."""
    app = create_app(database_url=str(seeded_engine.url), authz_source="database")
    return app


@pytest.fixture
def client_db_mode(app_db_mode):
    """Yield a database-auth TestClient for tenant endpoint cases."""
    with TestClient(app_db_mode) as client:
        yield client


# ---------------------------------------------------------------------------
# Case 1 — happy path
# ---------------------------------------------------------------------------


def test_tenants_me_returns_bootstrap_tenant_for_resolved_slug(
    client_db_mode,
):
    """Return the resolved tenant identity and its database currency label."""
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
        "primary_currency": "USD",
    }
    assert response.headers.get("Vary") and "X-UMS-Tenant" in response.headers["Vary"]


def test_openapi_documents_primary_currency_on_both_hydration_responses(
    client_db_mode,
):
    """The published OpenAPI contract carries ``primary_currency`` on BOTH hydration endpoints.

    The SPA hydrates from whichever of GET /tenants/me or GET /session/me
    answers first, so a schema-generated client must see the field on both
    response models — an undocumented field is a contract regression even
    when the runtime response carries it.
    """
    schema = client_db_mode.get("/openapi.json").json()

    tenant_properties = schema["components"]["schemas"]["TenantRead"]["properties"]
    assert "primary_currency" in tenant_properties, (
        "GET /tenants/me must document primary_currency"
    )

    session_properties = schema["components"]["schemas"]["SessionTenant"]["properties"]
    assert "primary_currency" in session_properties, (
        "GET /session/me must document primary_currency on its nested tenant"
    )


# ---------------------------------------------------------------------------
# Case 2 — missing X-UMS-Tenant header → 400
# ---------------------------------------------------------------------------


def test_tenants_me_rejects_missing_tenant_header(client_db_mode):
    """Reject a request that does not identify a tenant slug."""
    response = client_db_mode.get("/tenants/me", headers=_gateway_headers())
    assert response.status_code == 400
    assert response.json()["detail"] == "Tenant slug must not be blank"
    assert "X-UMS-Tenant" in (response.headers.get("Vary") or "")


# ---------------------------------------------------------------------------
# Case 3 — whitespace slug → 400 / over-255-char slug → 400
# ---------------------------------------------------------------------------


def test_tenants_me_rejects_whitespace_tenant_header(client_db_mode):
    """Reject a tenant header containing only whitespace."""
    response = client_db_mode.get(
        "/tenants/me",
        headers={"X-UMS-Tenant": "   ", **_gateway_headers()},
    )
    assert response.status_code == 400
    assert response.json()["detail"] == "Tenant slug must not be blank"


def test_tenants_me_rejects_overlong_tenant_header(client_db_mode):
    """Reject a tenant slug longer than the repository limit."""
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
    """Reject duplicate tenant headers before selecting either value."""
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


def test_tenants_me_rejects_mismatched_duplicate_tenant_headers(client_db_mode):
    """Different values across duplicate X-UMS-Tenant headers must also 400.

    Regression coverage requested in the CodeRabbit review of PR #41: the
    resolver must reject every duplicate-header case (identical OR mismatched)
    before silently selecting one of the values.
    """
    request = client_db_mode.build_request(
        "GET",
        "/tenants/me",
        headers=[
            ("X-UMS-Tenant", "ums"),
            ("X-UMS-Tenant", "not-a-tenant"),
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
    """Return not-found when the requested slug is absent from the registry."""
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
        """Return a minimal principal so the denial path reaches authorization."""
        return UserPrincipal(
            user_id=str(TEST_USER_ID),
            email="spec-a-deny@example.invalid",
            tenant_id=str(BOOTSTRAP_TENANT_ID),
        )

    app.dependency_overrides[current_principal_from_headers] = _stub_principal
    app.add_middleware(
        TrustedGatewayTenantResolverMiddleware,
        session_factory=session_factory,
        authorize_tenant=lambda *_: False,
    )
    with TestClient(app) as client:
        yield client


def test_tenants_me_returns_403_when_authorizer_denies(client_deny_authz):
    """Return forbidden when the tenant authorizer denies an active tenant."""
    response = client_deny_authz.get(
        "/tenants/me",
        headers={"X-UMS-Tenant": "ums", **_gateway_headers()},
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "Tenant access denied"


# ---------------------------------------------------------------------------
# Case 7 — missing / invalid gateway auth → 401
# ---------------------------------------------------------------------------
# current_trusted_gateway_identity raises HTTP_401_UNAUTHORIZED for both:
#   (a) missing/empty X-User-ID (dependencies.py:127-131)
#   (b) missing or wrong X-UMS-Trusted-Gateway-Token (_require_trusted_gateway_token:273)
# TrustedGatewayTenantResolverMiddleware intercepts these before the resolver,
# so the response is emitted from _send_http_exception, not the route handler.
# ---------------------------------------------------------------------------


def test_tenants_me_returns_401_when_gateway_user_id_missing(client_db_mode):
    """Reject a request with no trusted gateway user identity."""
    headers = _gateway_headers()
    headers.pop("X-User-ID")
    headers["X-UMS-Tenant"] = "ums"
    response = client_db_mode.get("/tenants/me", headers=headers)
    assert response.status_code == 401


def test_tenants_me_returns_401_when_gateway_token_invalid(client_db_mode):
    """Reject a request carrying an invalid trusted gateway token."""
    response = client_db_mode.get(
        "/tenants/me",
        headers={
            "X-UMS-Tenant": "ums",
            "X-User-ID": str(TEST_USER_ID),
            "X-UMS-Trusted-Gateway-Token": "definitely-not-the-token",
        },
    )
    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Cases 8-9 — headers-mode (DefaultTenantMiddleware) behaviour
# ---------------------------------------------------------------------------
# create_app(authz_source="headers") wires DefaultTenantMiddleware (binds the
# bootstrap tenant unconditionally) without overriding current_principal_from_headers.
# The raw current_principal_from_headers dependency therefore remains active and
# requires: X-User-ID, X-User-Email, X-Role, X-Scope-Type, and the gateway token.
# Without ALL of these, the principal dependency raises 401 before the route runs.
# ---------------------------------------------------------------------------


@pytest.fixture
def client_headers_mode(seeded_engine):
    """Yield a headers-auth TestClient using the seeded SQLite engine."""
    app = create_app(database_url=str(seeded_engine.url), authz_source="headers")
    with TestClient(app) as client:
        yield client


def test_tenants_me_headers_mode_requires_gateway_auth(client_headers_mode):
    """Default headers mode must NOT leak the bootstrap tenant identity to
    unauthenticated callers. The principal dependency fails closed even when
    DefaultTenantMiddleware would have bound the bootstrap tenant.
    """
    response = client_headers_mode.get("/tenants/me")
    assert response.status_code == 401


def test_tenants_me_headers_mode_returns_bootstrap_after_gateway_auth(
    client_headers_mode,
):
    """Headers mode is documented as a developer-shortcut for bootstrap-only
    deployments. With the full set of trusted-gateway principal headers in
    place, DefaultTenantMiddleware binds the bootstrap tenant even when
    X-UMS-Tenant is absent. This test documents the behavior — it is NOT
    security coverage for /tenants/me.

    NOTE: current_principal_from_headers (the active dependency in headers
    mode, not overridden) requires X-User-ID, X-User-Email, X-Role,
    X-Scope-Type, and X-UMS-Trusted-Gateway-Token. The spec row 9 describes
    "full trusted principal headers" — _gateway_headers() alone (only
    X-User-ID + token) would return 401 because email/role/scope are absent.
    This test therefore supplies all required fields via _full_principal_headers().
    """
    response = client_headers_mode.get("/tenants/me", headers=_full_principal_headers())
    assert response.status_code == 200
    payload = response.json()
    assert payload["slug"] == "ums"
    assert payload["id"] == str(BOOTSTRAP_TENANT_ID)


# ---------------------------------------------------------------------------
# EGP program Phase 1 — the bootstrap tenant's declared currency is CONFIGURED
# ---------------------------------------------------------------------------
# Headers mode fabricates its tenant instead of reading the `tenants` row, so
# UMS_TENANT_PRIMARY_CURRENCY is the only thing that can decide the declared
# currency there. These two tests pin both halves of that contract: the
# configured code reaches the wire, and the unset default is still "USD" so
# Phase 1 flips nothing. Nothing here converts an amount — UMS never does.
# ---------------------------------------------------------------------------


def _headers_mode_primary_currency(seeded_engine) -> str:
    """Return the primary_currency GET /tenants/me reports in headers mode."""
    app = create_app(database_url=str(seeded_engine.url), authz_source="headers")
    with TestClient(app) as client:
        response = client.get("/tenants/me", headers=_full_principal_headers())
    assert response.status_code == 200, response.text
    currency = response.json()["primary_currency"]
    assert isinstance(currency, str)
    return currency


def test_tenants_me_headers_mode_carries_the_configured_primary_currency(
    seeded_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bootstrap tenant declares UMS_TENANT_PRIMARY_CURRENCY, not a literal.

    Deliberately asserts a NON-USD code: a regression that re-hardcodes "USD"
    in ``app._bootstrap_tenant`` fails here, which a USD-valued assertion
    could not detect.
    """
    monkeypatch.setenv(TENANT_PRIMARY_CURRENCY_ENV, "EGP")
    load_app_settings.cache_clear()
    assert _headers_mode_primary_currency(seeded_engine) == "EGP"


def test_tenants_me_headers_mode_primary_currency_defaults_to_usd(
    seeded_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the env unset the declared currency stays USD — Phase 1 flips nothing."""
    monkeypatch.delenv(TENANT_PRIMARY_CURRENCY_ENV, raising=False)
    load_app_settings.cache_clear()
    assert _headers_mode_primary_currency(seeded_engine) == "USD"


def test_tenants_me_db_mode_reports_the_tenant_row_currency_not_the_setting(
    seeded_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Database mode reads ``tenants.primary_currency``; the env must not leak in.

    The row declares AED while the env declares EGP, so neither the setting nor
    the USD fallback can accidentally satisfy the assertion.
    """
    monkeypatch.setenv(TENANT_PRIMARY_CURRENCY_ENV, "EGP")
    load_app_settings.cache_clear()
    with Session(seeded_engine) as session:
        tenant = session.get(TenantORM, BOOTSTRAP_TENANT_ID)
        assert tenant is not None
        tenant.primary_currency = "AED"
        session.commit()

    app = create_app(database_url=str(seeded_engine.url), authz_source="database")
    with TestClient(app) as client:
        response = client.get(
            "/tenants/me",
            headers={"X-UMS-Tenant": "ums", **_gateway_headers()},
        )
    assert response.status_code == 200, response.text
    assert response.json()["primary_currency"] == "AED"


def test_tenants_me_db_override_ignores_invalid_headers_currency_setting(
    seeded_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit database-mode app starts despite invalid headers-only config."""
    monkeypatch.setenv(AUTHZ_SOURCE_ENV, "headers")
    monkeypatch.setenv(TENANT_PRIMARY_CURRENCY_ENV, "not-a-currency")
    load_app_settings.cache_clear()

    app = create_app(database_url=str(seeded_engine.url), authz_source="database")
    with TestClient(app) as client:
        response = client.get(
            "/tenants/me",
            headers={"X-UMS-Tenant": "ums", **_gateway_headers()},
        )

    assert response.status_code == 200, response.text
    assert response.json()["primary_currency"] == "USD"


def test_tenants_me_configured_db_mode_ignores_invalid_headers_currency_setting(
    seeded_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Environment-selected database mode also skips the unused currency setting."""
    monkeypatch.setenv(AUTHZ_SOURCE_ENV, "database")
    monkeypatch.setenv(TENANT_PRIMARY_CURRENCY_ENV, "not-a-currency")
    load_app_settings.cache_clear()

    app = create_app(database_url=str(seeded_engine.url))
    with TestClient(app) as client:
        response = client.get(
            "/tenants/me",
            headers={"X-UMS-Tenant": "ums", **_gateway_headers()},
        )

    assert response.status_code == 200, response.text
    assert response.json()["primary_currency"] == "USD"


def test_tenants_me_headers_override_rejects_invalid_currency_setting(
    seeded_engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit headers-mode app keeps strict currency validation."""
    monkeypatch.setenv(AUTHZ_SOURCE_ENV, "database")
    monkeypatch.setenv(TENANT_PRIMARY_CURRENCY_ENV, "not-a-currency")
    load_app_settings.cache_clear()

    with pytest.raises(ValueError, match=TENANT_PRIMARY_CURRENCY_ENV):
        create_app(database_url=str(seeded_engine.url), authz_source="headers")


# ---------------------------------------------------------------------------
# Case 10 — no tenant middleware installed → controlled 503 (not 500)
# ---------------------------------------------------------------------------
# create_app(database_url=None) does NOT install TenantResolverMiddleware nor
# DefaultTenantMiddleware (the `if resolved_database_url:` branch is skipped).
# A request whose principal dependency succeeds therefore reaches
# require_current_tenant() with no TENANT_CTX set. The route must map the
# resulting TenantContextMissing into a controlled 503 instead of an
# unhandled 500, preserving fail-closed semantics.
# ---------------------------------------------------------------------------


@pytest.fixture
def client_no_tenant_middleware():
    """App constructed without any tenant middleware (database_url unset)."""
    app = create_app(database_url=None, authz_source="headers")
    with TestClient(app) as client:
        yield client


def test_tenants_me_returns_503_when_tenant_middleware_missing(
    client_no_tenant_middleware,
):
    """Map missing tenant middleware to the controlled fail-closed 503 response."""
    response = client_no_tenant_middleware.get(
        "/tenants/me",
        headers=_full_principal_headers(),
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "Tenant resolver middleware is not installed"
