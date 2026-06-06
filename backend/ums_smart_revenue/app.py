"""FastAPI application factory and router wiring."""

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from fastapi import FastAPI, HTTPException
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ums_smart_revenue.api.adsense import router as adsense_router
from ums_smart_revenue.api.allocation import router as allocation_router
from ums_smart_revenue.api.audit import router as audit_router
from ums_smart_revenue.api.channel_account_links import (
    router as channel_account_links_router,
)
from ums_smart_revenue.api.channels import (
    current_audit_sink,
    current_channel_registry,
    sql_audit_sink_from_session,
    sql_channel_registry_from_session,
)
from ums_smart_revenue.api.channels import (
    router as channels_router,
)
from ums_smart_revenue.api.connectors import router as connectors_router
from ums_smart_revenue.api.dependencies import (
    authenticated_session_dependency,
    current_db_session,
    current_principal_from_database,
    current_principal_from_headers,
    current_trusted_gateway_identity,
)
from ums_smart_revenue.api.exchange_rates import router as exchange_rates_router
from ums_smart_revenue.api.exports import router as exports_router
from ums_smart_revenue.api.finance_close import router as finance_close_router
from ums_smart_revenue.api.groups import (
    router as groups_router,
)
from ums_smart_revenue.api.registry_dependencies import (
    current_group_registry,
    sql_group_registry_from_session,
)
from ums_smart_revenue.api.reports import router as reports_router
from ums_smart_revenue.api.revenue import (
    current_revenue_audit_sink,
    sql_revenue_audit_sink_from_session,
)
from ums_smart_revenue.api.revenue import router as revenue_router
from ums_smart_revenue.api.security import router as security_router
from ums_smart_revenue.api.session import router as session_router
from ums_smart_revenue.api.tenants import router as tenants_router
from ums_smart_revenue.api.users import router as users_router
from ums_smart_revenue.config.settings import (
    AUTHZ_SOURCE_DATABASE,
    AUTHZ_SOURCE_HEADERS,
    load_app_settings,
)
from ums_smart_revenue.config.version_baseline import STACK_VERSION_BASELINE
from ums_smart_revenue.db.session import build_session_factory, session_dependency
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus
from ums_smart_revenue.tenancy.resolver import (
    DEFAULT_BYPASS_PATHS,
    TenantAuthorizer,
    TenantResolverMiddleware,
    _normalise_bypass_paths,
)
from ums_smart_revenue.tenancy.resolver import (
    SessionFactory as TenantSessionFactory,
)


def create_app(
    *, database_url: str | None = None, authz_source: str | None = None
) -> FastAPI:
    """Create the FastAPI application with optional SQL-backed authorization."""
    settings = load_app_settings()
    resolved_database_url = database_url or settings.database_url
    resolved_authz_source = (authz_source or settings.authz_source).strip().lower()
    if resolved_authz_source not in {AUTHZ_SOURCE_HEADERS, AUTHZ_SOURCE_DATABASE}:
        raise ValueError("authz_source must be 'headers' or 'database'")
    if resolved_authz_source == AUTHZ_SOURCE_DATABASE and not resolved_database_url:
        raise ValueError(
            "database authz_source requires database_url or UMS_DATABASE_URL"
        )
    _app = FastAPI(
        title="UMS Smart Revenue Control Center API",
        version="0.1.0",
        description="Numbers-first internal revenue control API for UMS.",
    )

    if resolved_database_url:
        session_factory = build_session_factory(resolved_database_url)
        overrides = _app.dependency_overrides
        if resolved_authz_source == AUTHZ_SOURCE_DATABASE:
            overrides[current_db_session] = authenticated_session_dependency(
                session_factory
            )
        else:
            overrides[current_db_session] = session_dependency(session_factory)
            _app.add_middleware(DefaultTenantMiddleware)
        overrides[current_channel_registry] = sql_channel_registry_from_session
        overrides[current_group_registry] = sql_group_registry_from_session
        overrides[current_audit_sink] = sql_audit_sink_from_session
        overrides[current_revenue_audit_sink] = sql_revenue_audit_sink_from_session
        if resolved_authz_source == AUTHZ_SOURCE_DATABASE:
            overrides[current_principal_from_headers] = current_principal_from_database
            _app.add_middleware(
                TrustedGatewayTenantResolverMiddleware,
                session_factory=session_factory,
                authorize_tenant=_allow_database_auth_tenant,
            )

    _app.include_router(adsense_router)
    _app.include_router(allocation_router)
    _app.include_router(audit_router)
    _app.include_router(channel_account_links_router)
    _app.include_router(channels_router)
    _app.include_router(connectors_router)
    _app.include_router(exchange_rates_router)
    _app.include_router(exports_router)
    _app.include_router(finance_close_router)
    _app.include_router(groups_router)
    _app.include_router(reports_router)
    _app.include_router(revenue_router)
    _app.include_router(security_router)
    _app.include_router(session_router)
    _app.include_router(tenants_router)
    _app.include_router(users_router)

    def health_payload() -> dict[str, object]:
        """Return service and pinned runtime health metadata."""
        return {
            "status": "ok",
            "service": "ums-smart-revenue",
            "runtime": {
                "python": STACK_VERSION_BASELINE["runtime"]["python"],
                "fastapi": STACK_VERSION_BASELINE["backend"]["fastapi"],
                "pydantic": STACK_VERSION_BASELINE["backend"]["pydantic"],
            },
        }

    @_app.get("/health", tags=["system"])
    def health() -> dict[str, object]:
        """Return service health status."""
        return health_payload()

    @_app.get("/livez", tags=["system"])
    def livez() -> dict[str, object]:
        """Return liveness probe status."""
        return health_payload()

    return _app


def _allow_database_auth_tenant(_scope: object, _tenant_slug: str) -> bool:
    """Allow active tenant resolution before SQL principal loading checks identity."""
    return True


class TrustedGatewayTenantResolverMiddleware:
    """Resolve tenant context only after trusted gateway headers are valid."""

    def __init__(
        self,
        asgi_app: ASGIApp,
        session_factory: TenantSessionFactory,
        bypass_paths: Iterable[str] = DEFAULT_BYPASS_PATHS,
        authorize_tenant: TenantAuthorizer | None = None,
    ) -> None:
        """Initialise with the inner ASGI app, session factory, and optional bypass paths."""
        self._bypass_paths = _normalise_bypass_paths(bypass_paths)
        self._resolver = TenantResolverMiddleware(
            asgi_app,
            session_factory=session_factory,
            bypass_paths=self._bypass_paths,
            authorize_tenant=authorize_tenant,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Validate the trusted-gateway token before delegating to the tenant resolver."""
        if scope["type"] != "http" or scope.get("method", "").upper() == "OPTIONS":
            await self._resolver(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if _tenant_resolution_bypassed(path, self._bypass_paths):
            await self._resolver(scope, receive, send)
            return

        gateway_error = _trusted_gateway_error(scope)
        if gateway_error is not None:
            await _send_http_exception(gateway_error, scope, receive, send)
            return

        await self._resolver(scope, receive, send)


class DefaultTenantMiddleware:
    """Bind the bootstrap UMS tenant for SQL-backed trusted-header requests."""

    def __init__(
        self,
        asgi_app: ASGIApp,
        bypass_paths: Iterable[str] = DEFAULT_BYPASS_PATHS,
    ) -> None:
        """Initialise with the inner ASGI app and optional bypass paths."""
        self.app = asgi_app
        self._bypass_paths = _normalise_bypass_paths(bypass_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Bind the bootstrap tenant context and delegate to the inner app."""
        if scope["type"] != "http" or scope.get("method", "").upper() == "OPTIONS":
            await self.app(scope, receive, send)
            return
        path = str(scope.get("path") or "")
        if _tenant_resolution_bypassed(path, self._bypass_paths):
            await self.app(scope, receive, send)
            return

        token = TENANT_CTX.set(_bootstrap_tenant())
        try:
            await self.app(scope, receive, send)
        finally:
            TENANT_CTX.reset(token)


def _trusted_gateway_error(scope: Scope) -> HTTPException | None:
    """Validate trusted gateway headers; return an HTTPException if they are invalid."""
    headers = Headers(scope=scope)
    try:
        current_trusted_gateway_identity(
            x_user_id=headers.get("x-user-id"),
            x_ums_trusted_gateway_token=headers.get("x-ums-trusted-gateway-token"),
        )
    except HTTPException as exc:
        return exc
    return None


async def _send_http_exception(
    exc: HTTPException,
    scope: Scope,
    receive: Receive,
    send: Send,
) -> None:
    """Write an HTTP exception response directly to the ASGI send channel."""
    await JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )(scope, receive, send)


def _bootstrap_tenant() -> Tenant:
    """Return the bootstrap UMS tenant for single-tenant trusted-header requests."""
    now = datetime.now(UTC)
    return Tenant(
        id=UUID(UMS_TENANT_ID),
        slug="ums",
        display_name="UMS",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )


def _tenant_resolution_bypassed(path: str, bypass_paths: Iterable[str]) -> bool:
    """Return True if the request path matches a configured bypass path."""
    return any(
        path == bypass or path.startswith(bypass + "/") for bypass in bypass_paths
    )


app = create_app()
