"""FastAPI middleware that resolves the active tenant from a header.

The middleware reads the ``X-UMS-Tenant`` header on every non-bypassed
request, looks the slug up via the configured
:class:`~ums_smart_revenue.tenancy.repository.TenantRepository`, validates
the tenant's lifecycle status, and stores the resulting
:class:`~ums_smart_revenue.tenancy.models.Tenant` in
:data:`~ums_smart_revenue.tenancy.context.TENANT_CTX` for the duration
of the request task. Callers must provide an ``authorize_tenant`` callback
before non-bypassed requests can reach tenant-scoped application code; the
default is deny-all until S2.3 wires the principal-to-tenant authorization
contract. Tenant lookup runs before authorization so invalid slugs and
non-active lifecycle states keep the documented response mapping.

Status → response mapping:

* **Missing header**                 ``400 Bad Request``
* **Slug not in registry**           ``404 Not Found``
* **Tenant status = SUSPENDED**      ``423 Locked``
* **Tenant status = ARCHIVED**       ``410 Gone``
* **Authorization denies active**    ``403 Forbidden``
* **Tenant status = ACTIVE**         pass-through; contextvar set

The resolver is intentionally **not** installed in the default
:func:`ums_smart_revenue.app.create_app` factory. S2.2 only publishes
the surface; wiring it into the app happens in S2.4 once every test
and every route can satisfy the header requirement. Callers that want
the middleware today can attach it explicitly::

    app.add_middleware(
        TenantResolverMiddleware,
        session_factory=session_factory,
        authorize_tenant=principal_tenant_authorizer,
    )

Caching note: per Docs/17 the resolver should cache tenant lookups in
Redis with a short TTL. That cache lands in a follow-up slice; this
implementation hits the database once per non-bypassed request.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable
from concurrent.futures import CancelledError as FutureCancelledError

from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus
from ums_smart_revenue.tenancy.repository import (
    SqlAlchemyTenantRepository,
    TenantNotFoundError,
    TenantValidationError,
    _normalise_slug,
)

_LOGGER = logging.getLogger(__name__)

TENANT_HEADER = "X-UMS-Tenant"
DEFAULT_BYPASS_PATHS: tuple[str, ...] = (
    "/health",
    "/livez",
    "/readyz",
    "/docs",
    "/redoc",
    "/openapi.json",
)


SessionFactory = Callable[[], Session]
TenantAuthorizer = Callable[[Scope, str], bool]


class TenantResolverMiddleware:
    """Resolve the active tenant for every request that hits a tenant-scoped route."""

    def __init__(
        self,
        app: ASGIApp,
        session_factory: SessionFactory,
        bypass_paths: Iterable[str] = DEFAULT_BYPASS_PATHS,
        authorize_tenant: TenantAuthorizer | None = None,
    ) -> None:
        """Store resolver dependencies and validate the bypass path contract."""
        self.app = app
        self._session_factory = session_factory
        self._bypass_paths = _normalise_bypass_paths(bypass_paths)
        self._authorize_tenant = authorize_tenant or _deny_tenant_access

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Resolve the tenant, set request context, and fail closed on lookup errors."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if scope.get("method", "").upper() == "OPTIONS":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        if self._should_bypass(path):
            await self.app(scope, receive, send)
            return

        raw_slug = Headers(scope=scope).get(TENANT_HEADER, "")
        try:
            normalised_slug = _normalise_tenant_slug(raw_slug)
        except _ResolverError as error:
            await error.to_response()(scope, receive, send)
            return

        try:
            tenant = await run_in_threadpool(self._resolve, normalised_slug)
        except _ResolverError as error:
            await error.to_response()(scope, receive, send)
            return
        except (asyncio.CancelledError, FutureCancelledError):
            raise
        except Exception as exc:
            _LOGGER.error(
                "Tenant registry lookup failed: %s",
                exc,
                extra={
                    "path": scope.get("path"),
                    "scope_type": scope.get("type"),
                    "tenant_slug": normalised_slug,
                },
                exc_info=True,
            )
            await _ResolverError(
                status_code=503,
                detail="Tenant registry unavailable",
            ).to_response()(scope, receive, send)
            return

        if not self._is_authorized(scope, normalised_slug):
            await _ResolverError(
                status_code=403,
                detail="Tenant access denied",
            ).to_response()(scope, receive, send)
            return

        token = TENANT_CTX.set(tenant)
        try:
            await self.app(scope, receive, send)
        finally:
            TENANT_CTX.reset(token)

    def _resolve(self, normalised_slug: str) -> Tenant:
        """Open a tenant registry session for a validated slug."""
        session = self._session_factory()
        lookup_error: BaseException | None = None
        try:
            try:
                tenant = SqlAlchemyTenantRepository(session).get_by_slug(
                    normalised_slug
                )
            except TenantNotFoundError:
                raise _ResolverError(
                    status_code=404,
                    detail=f"Tenant {normalised_slug!r} not found",
                ) from None

            return _ensure_active_tenant(tenant)
        except BaseException as exc:
            lookup_error = exc
            raise
        finally:
            try:
                session.close()
            except Exception as exc:
                _LOGGER.warning(
                    "Tenant registry session close failed: %s",
                    exc,
                    exc_info=True,
                )
                if lookup_error is None:
                    raise

    def _should_bypass(self, path: str) -> bool:
        """Return whether a path is explicitly exempt from tenant resolution."""
        return any(path == bp or path.startswith(bp + "/") for bp in self._bypass_paths)

    def _is_authorized(self, scope: Scope, normalised_slug: str) -> bool:
        """Run the caller-supplied authorization hook and fail closed on errors."""
        try:
            decision = self._authorize_tenant(scope, normalised_slug)
        except Exception as exc:
            _LOGGER.warning(
                "Tenant authorization callback failed: %s",
                exc,
                extra={
                    "path": scope.get("path"),
                    "scope_type": scope.get("type"),
                    "tenant_slug": normalised_slug,
                },
                exc_info=True,
            )
            return False
        if not isinstance(decision, bool):
            _LOGGER.warning(
                "Tenant authorization callback returned non-bool decision",
                extra={
                    "path": scope.get("path"),
                    "scope_type": scope.get("type"),
                    "tenant_slug": normalised_slug,
                    "decision_type": type(decision).__name__,
                },
            )
            return False
        return decision


def _normalise_bypass_paths(bypass_paths: Iterable[str]) -> tuple[str, ...]:
    """Validate bypass paths and return a deduplicated normalized tuple."""
    if isinstance(bypass_paths, str):
        raise ValueError("bypass_paths must be an iterable of path strings")

    normalised_paths: list[str] = []
    for path in bypass_paths:
        if not isinstance(path, str):
            raise ValueError("bypass path must be a string")
        normalised = path.strip().rstrip("/")
        if not normalised or normalised == "/":
            raise ValueError("bypass path must not be empty or root")
        if not normalised.startswith("/"):
            raise ValueError("bypass path must start with '/'")
        normalised_paths.append(normalised)
    return tuple(dict.fromkeys(normalised_paths))


def _normalise_tenant_slug(raw_slug: str) -> str:
    """Normalize tenant slugs and translate invalid headers to resolver errors."""
    try:
        return _normalise_slug(raw_slug)
    except TenantValidationError as exc:
        raise _ResolverError(
            status_code=400,
            detail=str(exc),
            header=TENANT_HEADER,
        ) from exc


def _ensure_active_tenant(tenant: Tenant) -> Tenant:
    """Return active tenants and reject every non-active lifecycle state."""
    if tenant.status == TenantStatus.ACTIVE:
        return tenant
    if tenant.status == TenantStatus.SUSPENDED:
        raise _ResolverError(
            status_code=423,
            detail=f"Tenant {tenant.slug!r} is suspended",
        )
    if tenant.status == TenantStatus.ARCHIVED:
        raise _ResolverError(
            status_code=410,
            detail=f"Tenant {tenant.slug!r} is archived",
        )
    raise _ResolverError(
        status_code=500,
        detail=f"Tenant {tenant.slug!r} has invalid status {tenant.status!r}",
    )


def _deny_tenant_access(_scope: Scope, _slug: str) -> bool:
    """Default authorizer used until the caller provides a real tenancy policy."""
    return False


class _ResolverError(Exception):
    """Internal sentinel — translated to a JSONResponse before leaving dispatch()."""

    def __init__(
        self,
        *,
        status_code: int,
        detail: str,
        header: str | None = None,
    ) -> None:
        """Capture the JSON error contract produced by resolver failures."""
        self.status_code = status_code
        self.detail = detail
        self.header = header
        super().__init__(detail)

    def to_response(self) -> JSONResponse:
        """Build the Starlette response returned to the client."""
        payload: dict[str, object] = {"detail": self.detail}
        if self.header is not None:
            payload["header"] = self.header
        return JSONResponse(status_code=self.status_code, content=payload)
