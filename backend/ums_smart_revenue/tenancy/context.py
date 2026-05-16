"""Per-request tenant context, anchored on a stdlib :mod:`contextvars`.

Once the :class:`TenantResolverMiddleware` has identified the tenant for
an incoming request, it sets :data:`TENANT_CTX`. Any code running inside
that request's task can read the active tenant without taking it as an
argument — the same way Starlette / FastAPI propagate request state.

Two read helpers are exposed:

* :func:`get_current_tenant` — returns ``None`` if no tenant is set.
  Use this when a route legitimately serves both tenant and non-tenant
  callers (e.g. ``/health``, future platform-admin endpoints).
* :func:`require_current_tenant` — raises :class:`TenantContextMissing`
  when no tenant is set. Use this from any code path that assumes a
  request-scoped tenant; it converts a silent ``None`` into a loud
  programmer error.

Tenant context is *never* shared between requests. Each request gets
its own context, set + reset by the middleware in a try/finally.
"""

from __future__ import annotations

import contextvars
from typing import Final

from ums_smart_revenue.tenancy.models import Tenant

TENANT_CTX: Final[contextvars.ContextVar["Tenant | None"]] = contextvars.ContextVar(
    "ums_current_tenant", default=None
)


class TenantContextMissing(LookupError):
    """Raised when code asks for the active tenant but none is set.

    Inheriting from :class:`LookupError` (rather than e.g. ``RuntimeError``)
    keeps the semantics aligned with stdlib ``contextvars.ContextVar.get()``
    behaviour when no default is supplied.
    """


def get_current_tenant() -> Tenant | None:
    """Return the active tenant for the current request, or ``None``."""
    return TENANT_CTX.get()


def require_current_tenant() -> Tenant:
    """Return the active tenant; raise :class:`TenantContextMissing` if absent."""
    tenant = TENANT_CTX.get()
    if tenant is None:
        raise TenantContextMissing(
            "No tenant in context — middleware likely missing or "
            "request bypassed the resolver"
        )
    return tenant
