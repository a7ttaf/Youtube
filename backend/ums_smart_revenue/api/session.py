"""GET /session/me — return the authenticated principal's identity + capabilities."""
# ============================================================================
# Purpose: Public session-hydration route. Reads the trusted-gateway-loaded
#   principal and derives the SPA's render-hint capabilities (camelCase wire
#   keys) from its in-memory permission grants. No finance/auth write surface;
#   the underlying routes re-check each grant per requested scope, so the
#   capability flags here stay conservative render hints, never the boundary.
# Database/ORM: None — pure policy evaluation over the already-loaded principal.
# Standards: Thin route handler; capability derivation is a single sourced helper
#   (_derive_capabilities) that mirrors the same Permission checks each guarded
#   route enforces. No client-side authorization is invented.
# Blast Radius: Authorization read surface only. No write, no broadening.
# Connections:
#   - File: backend/ums_smart_revenue/auth/policy.py -> has_permission.
#   - File: backend/ums_smart_revenue/auth/permissions.py -> Permission enum.
# ============================================================================

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import (
    analytics_view_granted_any_scope,
    bank_reconciliation_view_granted_any_scope,
    connector_health_connector_ids,
    has_permission,
    org_data_permission_granted_any_scope,
    payments_view_granted_any_scope,
    scoped_finance_view_hint,
)
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


class ScopedFinanceViewHint(BaseModel):
    """Where one finance view permission is granted, at month resolution.

    ``global_scope`` is True when any global grant carries the permission;
    ``finance_months`` lists the explicit finance-month scope ids that do.
    A render hint only — the guarded routes re-check the requested month.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    global_scope: bool
    finance_months: list[str]


class SessionCapabilities(BaseModel):
    """Derived global-scope capability booleans the SPA uses to render UI.

    Python attributes stay snake_case (lint-clean); the wire/JSON keys are
    camelCase via the alias generator so the SPA consumes canViewRevenue etc.
    Most capabilities are global-scope checks; connector health, analytics, and
    revenue-valued analytics CSV hints are scope-aware so scoped users can open
    legitimate panels without over-broadening the underlying grant.
    """

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    can_view_revenue: bool
    # Global-scope-only variant of can_view_revenue: true ONLY when
    # VIEW_REVENUE is held at global scope. Gates surfaces whose backend
    # boundary checks VIEW_REVENUE @ global specifically (the composed
    # gap-explanation read), where the scope-aware hint above would let a
    # company/sector/channel-scoped viewer fire a guaranteed-403 fetch.
    can_view_revenue_global: bool
    can_view_confidence: bool
    can_view_analytics: bool
    can_view_payments: bool
    can_view_bank_reconciliation: bool
    # Month-resolution variants of the two booleans above: which months a
    # payments/bank read can possibly succeed for (global flag + explicit
    # finance-month ids). Lets month-bound surfaces (the gap-narrative panel)
    # restrict per SELECTED month instead of firing guaranteed-403 fetches.
    payments_view_scopes: ScopedFinanceViewHint
    bank_reconciliation_view_scopes: ScopedFinanceViewHint
    can_close_month: bool
    can_unlock_month: bool
    can_change_allocation: bool
    can_export_revenue: bool
    can_export_analytics_reports: bool
    can_manage_registry: bool
    can_manage_groups: bool
    can_import_channels: bool
    can_manage_connectors: bool
    can_view_connector_health: bool
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


def _scoped_hint(principal: UserPrincipal, permission: Permission) -> ScopedFinanceViewHint:
    """Wrap the policy layer's month-resolution hint into the wire model."""
    global_granted, finance_months = scoped_finance_view_hint(principal, permission)
    return ScopedFinanceViewHint(
        global_scope=global_granted,
        finance_months=list(finance_months),
    )


def _derive_capabilities(principal: UserPrincipal) -> SessionCapabilities:
    """Derive UI capabilities from the principal's permission grants."""
    # ========================================================================
    # Purpose: Evaluate each UI capability against the principal in-memory.
    #          Most are evaluated at GLOBAL scope; analytics CSV hints, payments,
    #          and bank-reconciliation are scope-aware where scoped users
    #          legitimately need those panels or export controls. The
    #          underlying routes still re-check the grant for the requested
    #          scope, so capabilities remain conservative render hints.
    # Database/ORM: None — pure policy evaluation over the already-loaded
    #               principal; no SQL is issued here.
    # Standards: Single source of truth is the policy layer (has_permission and
    #            scope-aware hint helpers); permission identity comes from the
    #            Permission enum.
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

    connector_health_ids = connector_health_connector_ids(principal)

    return SessionCapabilities(
        can_view_revenue=org_data_permission_granted_any_scope(
            principal,
            Permission.VIEW_REVENUE,
        ),
        # Global-only (the _can check, deliberately NOT the scope-aware hint):
        # the composed gap-explanation route gates on VIEW_REVENUE @ global,
        # so a scoped revenue viewer must see the restricted band, not fire a
        # guaranteed-403 fetch.
        can_view_revenue_global=_can(Permission.VIEW_REVENUE),
        can_view_confidence=_can(Permission.VIEW_CONFIDENCE),
        # Scope-aware (NOT the global-only _can): VIEW_ANALYTICS is held by
        # nearly every role, many only at company/sector/channel scope. A
        # global-only check would hide the analytics panel from a legitimately
        # scoped analytics user. The analytics routes still re-check the grant
        # per requested scope, so this stays a render hint, not the boundary.
        can_view_analytics=analytics_view_granted_any_scope(principal),
        # Scope-aware (NOT the global-only _can): VIEW_FINALIZED_PAYMENTS and
        # VIEW_BANK_RECONCILIATION are frequently granted only at finance_month
        # scope. A global-only check would hide the bank-reconciliation panel from
        # legitimately month-scoped finance users; the underlying routes still
        # re-check the grant for the requested month, so this stays a render hint.
        can_view_payments=payments_view_granted_any_scope(principal),
        can_view_bank_reconciliation=bank_reconciliation_view_granted_any_scope(principal),
        payments_view_scopes=_scoped_hint(
            principal, Permission.VIEW_FINALIZED_PAYMENTS
        ),
        bank_reconciliation_view_scopes=_scoped_hint(
            principal, Permission.VIEW_BANK_RECONCILIATION
        ),
        can_close_month=_can(Permission.LOCK_FINANCE_MONTH),
        can_unlock_month=_can(Permission.UNLOCK_FINANCE_MONTH),
        can_change_allocation=_can(Permission.CHANGE_ALLOCATION_RULE),
        can_export_revenue=_can(Permission.EXPORT_REVENUE_REPORT),
        can_export_analytics_reports=org_data_permission_granted_any_scope(
            principal,
            Permission.EXPORT_ANALYTICS_REPORT,
        ),
        # FIX: Map/Assign are gated on MANAGE_ORG_MAPPING at the backend routes
        # (PATCH /channels/{id}/mapping requires it on current + target scope;
        # POST /revenue/channel-account-links requires it globally).
        # MANAGE_CHANNELS is for channel creation (POST /channels/), not mapping;
        # a principal with MANAGE_CHANNELS but not MANAGE_ORG_MAPPING would see
        # live Map/Assign controls that silently 403 on every write.
        can_manage_registry=_can(Permission.MANAGE_ORG_MAPPING),
        # Gates the Groups view's sync/clear/archive controls (frontend): a
        # principal without MANAGE_GROUPS would see live controls that
        # silently 403 on every write, mirroring the can_manage_registry
        # rationale above.
        can_manage_groups=_can(Permission.MANAGE_GROUPS),
        # Both-permission gate (NOT either-of): POST /channels/import requires
        # MANAGE_CHANNELS at global scope always, and additionally MANAGE_GROUPS
        # whenever the roster carries Group_ID values. The conservative render
        # hint therefore requires both — a channels-only principal would
        # otherwise see a live import control whose group-bearing rosters 403
        # mid-flow (the same silent-403 trap can_manage_registry names above).
        can_import_channels=_can(Permission.MANAGE_CHANNELS) and _can(Permission.MANAGE_GROUPS),
        can_manage_connectors=_can(Permission.MANAGE_CONNECTORS),
        # Read-only run-history / health visibility — gates the ConnectorsView
        # run-history panel; mirrors GET /connectors/runs. This one capability
        # is scope-aware so connector-scoped users can open the panel while the
        # backend still restricts the row set to their granted connector IDs.
        can_view_connector_health=connector_health_ids is None or bool(connector_health_ids),
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
