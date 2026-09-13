# ============================================================================
# Purpose: Single fail-closed accessor for the ACTIVE tenant's declared
#   primary currency. Callers that need to label a display surface with "the
#   tenant's currency" read it here instead of hardcoding a literal or
#   re-deriving it from a fact row's stored currency.
# Database/ORM: None. ``Tenant`` already carries ``primary_currency`` (it is
#   hydrated once per request from the ``tenants`` row by
#   ``SqlAlchemyTenantRepository`` / the resolver, or fabricated by
#   ``app._bootstrap_tenant`` in headers mode), so this helper is a pure
#   contextvar read -- deliberately NOT a per-call database query.
# Standards: Fail-closed. It delegates to ``require_current_tenant()`` so a
#   missing tenant context raises the existing typed ``TenantContextMissing``
#   rather than silently substituting a default currency. There is no
#   ``default=`` parameter on purpose: a silent fallback is exactly the bug
#   this helper exists to prevent.
# Blast Radius: Display labelling only. This module performs NO currency
#   conversion, holds no rate, and touches no fact table, CHECK constraint, or
#   finance calculation -- UMS converts nothing, anywhere.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/context.py -> require_current_tenant
#     supplies the request-scoped tenant and the typed missing-context error.
#   - File: backend/ums_smart_revenue/tenancy/models.py -> Tenant.primary_currency.
#   - File: backend/ums_smart_revenue/api/tenants.py -> GET /tenants/me exposes
#     the value as the additive ``primary_currency`` field.
#   - File: backend/ums_smart_revenue/config/settings.py -> supplies the value
#     for the fabricated headers-mode bootstrap tenant.
# ============================================================================
"""Read the active tenant's declared primary currency from request context."""

from __future__ import annotations

from ums_smart_revenue.tenancy.context import require_current_tenant


def get_tenant_primary_currency() -> str:
    """Return the current tenant's ISO-4217 primary currency code.

    The value is the tenant's *declared* currency label — it says how the
    tenant reports, never how any stored amount should be converted (UMS
    performs no currency conversion).

    Raises:
        TenantContextMissing: If no tenant is bound to the current request
            context. Fail-closed by design; there is no default currency.
    """
    return require_current_tenant().primary_currency
