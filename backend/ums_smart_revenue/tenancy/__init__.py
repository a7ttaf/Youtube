"""Tenancy public surface — domain model, contextvar, repository, middleware.

Wired in S2.2. The resolver middleware is published but NOT yet
installed by :func:`ums_smart_revenue.app.create_app`; that happens in
S2.4 once every route + test can satisfy the ``X-UMS-Tenant`` header
contract.
"""

from ums_smart_revenue.tenancy.context import (
    TENANT_CTX,
    TenantContextMissing,
    get_current_tenant,
    require_current_tenant,
)
from ums_smart_revenue.tenancy.models import (
    DEFAULT_PRIMARY_CURRENCY,
    PLACEHOLDER_TENANT_EPOCH,
    Tenant,
    TenantStatus,
    make_placeholder_tenant,
)
from ums_smart_revenue.tenancy.repository import (
    SqlAlchemyTenantRepository,
    TenantNotFoundError,
    TenantRepository,
    TenantValidationError,
    normalise_slug,
)
from ums_smart_revenue.tenancy.resolver import (
    DEFAULT_BYPASS_PATHS,
    TENANT_HEADER,
    TenantResolverMiddleware,
)

__all__ = [
    "DEFAULT_BYPASS_PATHS",
    "DEFAULT_PRIMARY_CURRENCY",
    "PLACEHOLDER_TENANT_EPOCH",
    "SqlAlchemyTenantRepository",
    "TENANT_CTX",
    "TENANT_HEADER",
    "Tenant",
    "TenantContextMissing",
    "TenantNotFoundError",
    "TenantRepository",
    "TenantResolverMiddleware",
    "TenantStatus",
    "TenantValidationError",
    "get_current_tenant",
    "make_placeholder_tenant",
    "normalise_slug",
    "require_current_tenant",
]
