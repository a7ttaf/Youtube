"""GET /tenants/me — return the tenant resolved by middleware after auth."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.tenancy.context import require_current_tenant

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantRead(BaseModel):
    """Public shape of the tenant context returned to the browser."""

    id: UUID
    slug: str
    display_name: str


# ============================================================================
# Purpose: Return the tenant resolved by TenantResolverMiddleware after the
#          gateway/principal dependency has authenticated the caller.
# Database/ORM: No SQL from this handler; current_principal_from_headers is
#               overridden to SQL principal loading in database auth mode.
# Standards: Thin route; dependency-owned auth; explicit field construction
#            because Tenant is a domain dataclass, not a Pydantic model.
# Blast Radius: Authorization dependency required; no write path, no finance
#               impact. No graph projection impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/api/dependencies.py -> auth dependency.
#   - File: backend/ums_smart_revenue/tenancy/resolver.py -> sets TENANT_CTX.
#   - File: backend/ums_smart_revenue/tenancy/context.py -> require_current_tenant.
#   - File: backend/ums_smart_revenue/app.py -> include_router wiring.
# ============================================================================
@router.get("/me", response_model=TenantRead)
def get_current_tenant_endpoint(
    _principal: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
) -> TenantRead:
    tenant = require_current_tenant()
    return TenantRead(
        id=tenant.id,
        slug=tenant.slug,
        display_name=tenant.display_name,
    )
