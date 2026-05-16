"""FastAPI middleware that resolves the active tenant from a header.

The middleware reads the ``X-UMS-Tenant`` header on every non-bypassed
request, looks the slug up via the configured
:class:`~ums_smart_revenue.tenancy.repository.TenantRepository`, validates
the tenant's lifecycle status, and stores the resulting
:class:`~ums_smart_revenue.tenancy.models.Tenant` in
:data:`~ums_smart_revenue.tenancy.context.TENANT_CTX` for the duration
of the request task.

Status → response mapping:

* **Missing header**                 ``400 Bad Request``
* **Slug not in registry**           ``404 Not Found``
* **Tenant status = SUSPENDED**      ``423 Locked``
* **Tenant status = ARCHIVED**       ``410 Gone``
* **Tenant status = ACTIVE**         pass-through; contextvar set

The resolver is intentionally **not** installed in the default
:func:`ums_smart_revenue.app.create_app` factory. S2.2 only publishes
the surface; wiring it into the app happens in S2.4 once every test
and every route can satisfy the header requirement. Callers that want
the middleware today can attach it explicitly::

    app.add_middleware(
        TenantResolverMiddleware,
        session_factory=session_factory,
    )

Caching note: per Docs/17 the resolver should cache tenant lookups in
Redis with a short TTL. That cache lands in a follow-up slice; this
implementation hits the database once per non-bypassed request.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Awaitable, Callable

from fastapi import Request
from sqlalchemy.orm import Session
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import TenantStatus
from ums_smart_revenue.tenancy.repository import (
    SqlAlchemyTenantRepository,
    TenantNotFoundError,
    TenantValidationError,
)

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


class TenantResolverMiddleware(BaseHTTPMiddleware):
    """Resolve the active tenant for every request that hits a tenant-scoped route."""

    def __init__(
        self,
        app: ASGIApp,
        session_factory: SessionFactory,
        bypass_paths: Iterable[str] = DEFAULT_BYPASS_PATHS,
    ) -> None:
        super().__init__(app)
        self._session_factory = session_factory
        self._bypass_paths: tuple[str, ...] = tuple(bypass_paths)

    async def dispatch(  # type: ignore[override]
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if self._should_bypass(request.url.path):
            return await call_next(request)

        raw_slug = request.headers.get(TENANT_HEADER, "")
        try:
            tenant = self._resolve(raw_slug)
        except _ResolverError as error:
            return error.to_response()

        token = TENANT_CTX.set(tenant)
        try:
            return await call_next(request)
        finally:
            TENANT_CTX.reset(token)

    def _resolve(self, raw_slug: str):
        session = self._session_factory()
        try:
            try:
                tenant = SqlAlchemyTenantRepository(session).get_by_slug(raw_slug)
            except TenantValidationError as exc:
                raise _ResolverError(
                    status_code=400,
                    detail=str(exc),
                    header=TENANT_HEADER,
                ) from exc
            except TenantNotFoundError:
                raise _ResolverError(
                    status_code=404,
                    detail=f"Tenant {raw_slug.strip().lower()!r} not found",
                ) from None

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

            return tenant
        finally:
            session.close()

    def _should_bypass(self, path: str) -> bool:
        return any(path == bp or path.startswith(bp + "/") for bp in self._bypass_paths)


class _ResolverError(Exception):
    """Internal sentinel — translated to a JSONResponse before leaving dispatch()."""

    def __init__(
        self,
        *,
        status_code: int,
        detail: str,
        header: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.detail = detail
        self.header = header
        super().__init__(detail)

    def to_response(self) -> JSONResponse:
        payload: dict[str, object] = {"detail": self.detail}
        if self.header is not None:
            payload["header"] = self.header
        return JSONResponse(status_code=self.status_code, content=payload)
