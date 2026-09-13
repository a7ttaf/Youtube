"""GET /tenants/me — return the tenant resolved by middleware after auth."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.tenancy.context import (
    TenantContextMissing,
    require_current_tenant,
)
from ums_smart_revenue.tenancy.currency import get_tenant_primary_currency

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantRead(BaseModel):
    """Public shape of the tenant context returned to the browser."""

    id: UUID
    slug: str
    display_name: str
    # Additive (2026-08-27): the tenant's DECLARED ISO-4217 reporting currency,
    # so the SPA can label money without hardcoding a literal of its own. A
    # label, not a rate — UMS performs no currency conversion, and this field
    # says nothing about the currency any stored amount is denominated in.
    primary_currency: str


# ============================================================================
# Purpose: Return the tenant resolved by TenantResolverMiddleware after the
#          gateway/principal dependency has authenticated the caller.
# Database/ORM: No SQL from this handler; current_principal_from_headers is
#               overridden to SQL principal loading in database auth mode.
# Standards: Thin route; dependency-owned auth; explicit field construction
#            because Tenant is a domain dataclass, not a Pydantic model.
#            TenantContextMissing (no resolver middleware installed) is mapped
#            to a controlled 503 instead of an unhandled 500 so the route
#            fails closed in valid app configurations that lack tenant middleware.
# Blast Radius: Authorization dependency required; no write path, no finance
#               impact. No graph projection impact detected. The additive
#               primary_currency field is a read-only DECLARED label; it
#               carries no rate and triggers no conversion.
# Connections:
#   - File: backend/ums_smart_revenue/api/dependencies.py -> auth dependency.
#   - File: backend/ums_smart_revenue/tenancy/resolver.py -> sets TENANT_CTX.
#   - File: backend/ums_smart_revenue/tenancy/context.py -> require_current_tenant.
#   - File: backend/ums_smart_revenue/tenancy/currency.py ->
#     get_tenant_primary_currency supplies the primary_currency field.
#   - File: backend/ums_smart_revenue/app.py -> include_router wiring.
# ============================================================================
@router.get("/me", response_model=TenantRead)
def get_current_tenant_endpoint(
    _principal: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
) -> TenantRead:
    """Return the request-scoped tenant's identity and declared primary currency."""
    try:
        tenant = require_current_tenant()
        # Read through the tenancy helper rather than off `tenant` directly so
        # every display surface that needs the tenant currency goes through one
        # fail-closed accessor; both reads see the same request-scoped tenant.
        primary_currency = get_tenant_primary_currency()
    except TenantContextMissing as exc:
        # FIX: Translate missing tenant context into an explicit 503 instead
        # of letting it bubble as an unhandled 500. create_app omits tenant
        # middleware when database_url is unset, so a valid app config can
        # reach this line without a resolver having populated TENANT_CTX.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Tenant resolver middleware is not installed",
        ) from exc
    return TenantRead(
        id=tenant.id,
        slug=tenant.slug,
        display_name=tenant.display_name,
        primary_currency=primary_currency,
    )
