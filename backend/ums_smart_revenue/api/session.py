"""GET /session/me — return the authenticated principal's identity + capabilities."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.tenancy.context import get_current_tenant

router = APIRouter(prefix="/session", tags=["session"])


class SessionTenant(BaseModel):
    """Resolved tenant context, or absent when no tenant could be resolved."""

    id: str
    slug: str
    display_name: str


class SessionScopeAssignment(BaseModel):
    """A single active role assignment flattened for the SPA."""

    role: str
    scope_type: str
    scope_id: str | None


class SessionPermissionGrant(BaseModel):
    """A single active direct permission grant flattened for the SPA."""

    permission: str
    scope_type: str
    scope_id: str | None


class SessionCapabilities(BaseModel):
    """Derived global-scope capability booleans the SPA uses to render UI.

    Python attributes stay snake_case (lint-clean); the wire/JSON keys are
    camelCase via the alias generator so the SPA consumes canViewRevenue etc.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    can_view_revenue: bool
    can_view_confidence: bool
    can_view_payments: bool
    can_view_bank_reconciliation: bool
    can_close_month: bool
    can_unlock_month: bool
    can_change_allocation: bool
    can_export_revenue: bool
    can_export_analytics_reports: bool
    can_manage_registry: bool
    can_manage_connectors: bool
    can_run_connector_jobs: bool
    can_view_audit: bool


class SessionMe(BaseModel):
    """Public shape of the authenticated session returned to the browser."""

    user_id: str
    email: str
    tenant: SessionTenant | None
    roles: list[SessionScopeAssignment]
    permissions: list[SessionPermissionGrant]
    is_service_account: bool
    disabled: bool
    capabilities: SessionCapabilities


def _derive_capabilities(principal: UserPrincipal) -> SessionCapabilities:
    """Derive global-scope UI capabilities from the principal's permission grants."""
    # ========================================================================
    # Purpose: Evaluate each UI capability against the principal in-memory at
    #          GLOBAL scope. A GLOBAL grant satisfies a global target; a
    #          narrower (scoped-only) grant does not, so capabilities stay
    #          conservative and are never hardcoded true.
    # Database/ORM: None — pure policy evaluation over the already-loaded
    #               principal; no SQL is issued here.
    # Standards: Single source of truth is the policy layer (has_permission);
    #            permission identity comes from the Permission enum.
    # Blast Radius: Authorization read-only. No write, no broadening — mirrors
    #               the same Permission checks each guarded route enforces.
    #               No graph projection impact detected.
    # Connections:
    #   - File: backend/ums_smart_revenue/auth/policy.py -> has_permission.
    #   - File: backend/ums_smart_revenue/auth/permissions.py -> Permission enum.
    #   - File: backend/ums_smart_revenue/auth/seed.py -> ROLE_PERMISSIONS.
    # ========================================================================
    global_scope = AccessScope.global_scope()

    def _can(permission: Permission) -> bool:
        """Return True if principal holds permission at global scope."""
        return has_permission(principal, permission, global_scope)

    return SessionCapabilities(
        can_view_revenue=_can(Permission.VIEW_REVENUE),
        can_view_confidence=_can(Permission.VIEW_CONFIDENCE),
        can_view_payments=_can(Permission.VIEW_FINALIZED_PAYMENTS),
        can_view_bank_reconciliation=_can(Permission.VIEW_BANK_RECONCILIATION),
        can_close_month=_can(Permission.LOCK_FINANCE_MONTH),
        can_unlock_month=_can(Permission.UNLOCK_FINANCE_MONTH),
        can_change_allocation=_can(Permission.CHANGE_ALLOCATION_RULE),
        can_export_revenue=_can(Permission.EXPORT_REVENUE_REPORT),
        can_export_analytics_reports=_can(Permission.EXPORT_ANALYTICS_REPORT),
        # FIX: MANAGE_GROUPS grants group-structure edits, not channel mapping or
        # account-link proposals; PATCH /channels/{id}/mapping and
        # POST /revenue/channel-account-links both require MANAGE_ORG_MAPPING,
        # so enabling Map/Assign buttons for a MANAGE_GROUPS-only principal
        # would silently fail every write with 403.
        can_manage_registry=(
            _can(Permission.MANAGE_CHANNELS)
            or _can(Permission.MANAGE_ORG_MAPPING)
        ),
        can_manage_connectors=_can(Permission.MANAGE_CONNECTORS),
        can_run_connector_jobs=_can(Permission.RUN_CONNECTOR_JOBS),
        can_view_audit=_can(Permission.VIEW_AUDIT_LOG),
    )


def _resolve_tenant() -> SessionTenant | None:
    """Return the request-scoped tenant if present, else None; never raises."""
    # ========================================================================
    # Purpose: Return the request-scoped tenant if the resolver populated it,
    #          else None. Unlike /tenants/me this MUST NOT fail closed on a
    #          missing tenant — the SPA still needs identity + capabilities even
    #          when tenant context is unavailable.
    # Database/ORM: None — reads the contextvar set by tenant middleware.
    # Standards: get_current_tenant() (the non-raising read) by design returns
    #            None instead of raising TenantContextMissing.
    # Blast Radius: Read-only. No graph projection impact detected.
    # Connections:
    #   - File: backend/ums_smart_revenue/tenancy/context.py -> get_current_tenant.
    # ========================================================================
    tenant = get_current_tenant()
    if tenant is None:
        return None
    return SessionTenant(
        id=str(tenant.id),
        slug=tenant.slug,
        display_name=tenant.display_name,
    )


# ============================================================================
# Purpose: Return the authenticated principal's identity, tenant (optional),
#          active roles/permissions, and derived global-scope UI capabilities
#          so the SPA can render the correct surface instead of a permanent
#          access-denied screen.
# Database/ORM: No SQL from this handler. current_principal_from_headers is
#               overridden to SQL principal loading in database auth mode, so
#               all fail-closed behavior (401 missing token/headers, 403
#               disabled/unknown, 503 storage/data errors) is preserved by the
#               injected dependency, not re-implemented here.
# Standards: Thin route; dependency-owned auth; capabilities DERIVED from the
#            policy layer (never hardcoded); no audit log (self identity read,
#            mirrors /tenants/me which does not audit). Cache-Control: no-store
#            and Vary: Authorization prevent caching a per-principal response.
# Blast Radius: Authorization read-only — no permission broadening, no write,
#               no finance number impact. No graph projection impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/api/dependencies.py -> auth dependency.
#   - File: backend/ums_smart_revenue/auth/policy.py -> capability evaluation.
#   - File: backend/ums_smart_revenue/tenancy/context.py -> optional tenant read.
#   - File: backend/ums_smart_revenue/app.py -> include_router + DB override.
# ============================================================================
@router.get("/me", response_model=SessionMe, response_model_by_alias=True)
def get_current_session_endpoint(
    response: Response,
    principal: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
) -> SessionMe:
    """Return the authenticated principal's identity and derived capabilities."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Authorization"

    roles = [
        SessionScopeAssignment(
            role=assignment.role.value,
            scope_type=assignment.scope.type.value,
            scope_id=assignment.scope.id,
        )
        for assignment in principal.role_assignments
        if assignment.active
    ]
    permissions = [
        SessionPermissionGrant(
            permission=grant.permission.value,
            scope_type=grant.scope.type.value,
            scope_id=grant.scope.id,
        )
        for grant in principal.direct_permissions
        if grant.active
    ]

    return SessionMe(
        user_id=principal.user_id,
        email=principal.email,
        tenant=_resolve_tenant(),
        roles=roles,
        permissions=permissions,
        is_service_account=principal.is_service_account,
        disabled=principal.disabled,
        capabilities=_derive_capabilities(principal),
    )
