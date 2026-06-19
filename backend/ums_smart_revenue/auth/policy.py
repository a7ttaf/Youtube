"""Authorization predicates for tenant users and platform admins."""

from types import MappingProxyType

from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.platform_admin import (
    PlatformAdminPrincipal,
    PlatformAdminStatus,
    Principal,
)
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex, ScopeType
from ums_smart_revenue.auth.seed import ROLE_PERMISSIONS

EMPTY_ORG_INDEX = OrgAccessIndex()
_CONNECTOR_KEY_ALIASES = MappingProxyType(
    {
        "youtube-reporting": ("youtube_reporting",),
        "youtube_reporting": ("youtube-reporting",),
        "youtube-analytics": ("youtube_analytics",),
        "youtube_analytics": ("youtube-analytics",),
        # FIX: Treat the legacy AdSense connector scope as an umbrella for the
        # AdSense management run-history keys so scoped health reads stay complete.
        "adsense": ("adsense-management", "adsense_management"),
        "adsense-management": ("adsense_management",),
        "adsense_management": ("adsense-management",),
    }
)


def has_permission(
    user: UserPrincipal,
    permission: Permission,
    scope: AccessScope,
    org_index: OrgAccessIndex | None = None,
) -> bool:
    """Return whether a tenant user has a permission on a scope."""
    if user.disabled:
        return False

    index = org_index or EMPTY_ORG_INDEX

    for grant in user.direct_permissions:
        if grant.active and grant.permission == permission and index.contains(grant.scope, scope):
            return True

    for assignment in user.role_assignments:
        if not assignment.active:
            continue
        role_permissions = ROLE_PERMISSIONS.get(assignment.role, frozenset())
        if permission in role_permissions and index.contains(assignment.scope, scope):
            return True

    return False


# ============================================================================
# Purpose: Return the connector IDs that are allowed to view connector health,
#          or None when the caller has global VIEW_CONNECTOR_HEALTH access.
# Database/ORM: None — pure policy evaluation over the already-loaded principal.
# Standards: Fail closed for disabled users and malformed/empty connector scope
#            grants. Global access is represented by None so callers can
#            distinguish unfiltered reads from scoped filtering.
# Blast Radius: Authorization read path only. No write, finance, audit, or
#               graph impact.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py -> GET /connectors/runs
#     must filter run-history reads to these IDs.
#   - File: backend/ums_smart_revenue/api/session.py -> /session/me capability
#     derivation uses the same decision for the SPA.
# ============================================================================
def connector_health_connector_ids(user: UserPrincipal) -> frozenset[str] | None:
    """Return allowed connector IDs for VIEW_CONNECTOR_HEALTH, or None for global access."""
    global_scope = AccessScope.global_scope()
    if has_permission(user, Permission.VIEW_CONNECTOR_HEALTH, global_scope):
        return None

    connector_ids: set[str] = set()

    for grant in user.direct_permissions:
        if (
            grant.active
            and grant.permission == Permission.VIEW_CONNECTOR_HEALTH
            and grant.scope.type == ScopeType.CONNECTOR
            and grant.scope.id
            and has_permission(
                user,
                Permission.VIEW_CONNECTOR_HEALTH,
                AccessScope.connector(grant.scope.id),
            )
        ):
            connector_ids.update(_connector_key_candidates(grant.scope.id))

    for assignment in user.role_assignments:
        if (
            assignment.active
            and assignment.scope.type == ScopeType.CONNECTOR
            and assignment.scope.id
            and has_permission(
                user,
                Permission.VIEW_CONNECTOR_HEALTH,
                AccessScope.connector(assignment.scope.id),
            )
        ):
            connector_ids.update(_connector_key_candidates(assignment.scope.id))

    return frozenset(connector_ids)


def _connector_key_candidates(connector_id: str) -> tuple[str, ...]:
    """Return the connector id plus its alias form for permission filtering."""
    candidates = [connector_id]
    candidates.extend(_CONNECTOR_KEY_ALIASES.get(connector_id, ()))
    return tuple(dict.fromkeys(candidates))


# ============================================================================
# Purpose: Decide whether a principal may see ANY analytics surface, i.e.
#          holds an active VIEW_ANALYTICS grant (direct or via role) at ANY
#          scope (global, sector, company, or channel). The SPA uses this to
#          mount the analytics panel; the backend analytics routes still
#          re-check VIEW_ANALYTICS per requested scope, so this is a render
#          hint, never the authorization boundary.
# Database/ORM: None — pure policy evaluation over the already-loaded principal.
# Standards: Fail closed for disabled users (has_permission returns False for
#            disabled, and the loops below short-circuit on the disabled flag).
#            Mirrors the scope-aware connector_health_connector_ids derivation
#            rather than the global-only has_permission(global_scope) check,
#            which would hide the panel from a legitimately company/sector/
#            channel-scoped analytics user.
# Blast Radius: Authorization read path only. No write, finance, audit, or
#               graph impact. No graph projection impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/api/session.py -> /session/me capability
#     derivation (can_view_analytics).
#   - File: backend/ums_smart_revenue/auth/seed.py -> ROLE_PERMISSIONS holds the
#     VIEW_ANALYTICS membership per role.
# ============================================================================
def analytics_view_granted_any_scope(user: UserPrincipal) -> bool:
    """Return True if the user holds VIEW_ANALYTICS at any scope; disabled → False."""
    if user.disabled:
        return False

    for grant in user.direct_permissions:
        if grant.active and grant.permission == Permission.VIEW_ANALYTICS:
            return True

    for assignment in user.role_assignments:
        if not assignment.active:
            continue
        role_permissions = ROLE_PERMISSIONS.get(assignment.role, frozenset())
        if Permission.VIEW_ANALYTICS in role_permissions:
            return True

    return False


def _is_payment_or_bank_eligible_scope(scope: AccessScope) -> bool:
    """Return True for scopes that can satisfy payment/bank reads.

    VIEW_FINALIZED_PAYMENTS and VIEW_BANK_RECONCILIATION authorize against
    AccessScope.finance_month(month) at the bank-reconciliation endpoint, so
    org-scoped (company/sector/channel) assignments of those permissions must
    not hint the SPA to mount the panel. Global and finance-month scopes are
    the only eligible hint sources.
    """
    return scope.type in (ScopeType.GLOBAL, ScopeType.FINANCE_MONTH)


def payments_view_granted_any_scope(user: UserPrincipal) -> bool:
    """Return True if the user holds VIEW_FINALIZED_PAYMENTS at global or finance-month scope."""
    if user.disabled:
        return False

    for grant in user.direct_permissions:
        if (
            grant.active
            and grant.permission == Permission.VIEW_FINALIZED_PAYMENTS
            and _is_payment_or_bank_eligible_scope(grant.scope)
        ):
            return True

    for assignment in user.role_assignments:
        if not assignment.active:
            continue
        role_permissions = ROLE_PERMISSIONS.get(assignment.role, frozenset())
        if (
            Permission.VIEW_FINALIZED_PAYMENTS in role_permissions
            and _is_payment_or_bank_eligible_scope(assignment.scope)
        ):
            return True

    return False


def bank_reconciliation_view_granted_any_scope(user: UserPrincipal) -> bool:
    """Return True if the user holds VIEW_BANK_RECONCILIATION at global or finance-month scope."""
    if user.disabled:
        return False

    for grant in user.direct_permissions:
        if (
            grant.active
            and grant.permission == Permission.VIEW_BANK_RECONCILIATION
            and _is_payment_or_bank_eligible_scope(grant.scope)
        ):
            return True

    for assignment in user.role_assignments:
        if not assignment.active:
            continue
        role_permissions = ROLE_PERMISSIONS.get(assignment.role, frozenset())
        if (
            Permission.VIEW_BANK_RECONCILIATION in role_permissions
            and _is_payment_or_bank_eligible_scope(assignment.scope)
        ):
            return True

    return False


def can_view_channel_analytics(
    user: UserPrincipal,
    channel_id: str,
    org_index: OrgAccessIndex,
) -> bool:
    """Authorize analytics reads for a specific channel."""
    return has_permission(
        user, Permission.VIEW_ANALYTICS, AccessScope.channel(channel_id), org_index
    )


def can_view_channel_revenue(
    user: UserPrincipal,
    channel_id: str,
    org_index: OrgAccessIndex,
) -> bool:
    """Authorize revenue reads for a specific channel."""
    return has_permission(user, Permission.VIEW_REVENUE, AccessScope.channel(channel_id), org_index)


def can_view_company_revenue(
    user: UserPrincipal,
    company_id: str,
    org_index: OrgAccessIndex,
) -> bool:
    """Authorize revenue reads for a company scope."""
    return has_permission(user, Permission.VIEW_REVENUE, AccessScope.company(company_id), org_index)


def can_export_finance_report(
    user: UserPrincipal,
    scope: AccessScope,
    org_index: OrgAccessIndex,
) -> bool:
    """Authorize exports only when the user can export and view revenue."""
    return has_permission(
        user, Permission.EXPORT_REVENUE_REPORT, scope, org_index
    ) and has_permission(
        user,
        Permission.VIEW_REVENUE,
        scope,
        org_index,
    )


def can_lock_month(user: UserPrincipal, month: str) -> bool:
    """Authorize locking a finance month."""
    return has_permission(user, Permission.LOCK_FINANCE_MONTH, AccessScope.finance_month(month))


def can_unlock_month(user: UserPrincipal, month: str) -> bool:
    """Authorize unlocking a finance month."""
    return has_permission(user, Permission.UNLOCK_FINANCE_MONTH, AccessScope.finance_month(month))


def can_change_allocation_rule(user: UserPrincipal, month: str) -> bool:
    """Authorize changes to allocation rules for a finance month."""
    return has_permission(user, Permission.CHANGE_ALLOCATION_RULE, AccessScope.finance_month(month))


def can_manage_connectors(user: UserPrincipal) -> bool:
    """Authorize connector administration."""
    return has_permission(user, Permission.MANAGE_CONNECTORS, AccessScope.global_scope())


def can_run_connector_job(user: UserPrincipal, connector_id: str) -> bool:
    """Authorize running a connector job."""
    return has_permission(user, Permission.RUN_CONNECTOR_JOBS, AccessScope.connector(connector_id))


def can_assign_roles(user: UserPrincipal) -> bool:
    """Authorize global role assignment."""
    return has_permission(user, Permission.ASSIGN_ROLES, AccessScope.global_scope())


def is_platform_admin(principal: Principal) -> bool:
    """Return True when the principal is a live platform admin.

    Used by the platform-admin route surface (introduced in a later S2
    slice) to refuse tenant-user callers. ``UserPrincipal`` always
    returns False — platform admins live exclusively in the
    ``platform_admins`` table.
    """
    if not isinstance(principal, PlatformAdminPrincipal):
        return False
    return principal.status == PlatformAdminStatus.ACTIVE


def can_manage_tenants(principal: Principal) -> bool:
    """Authorisation predicate for tenant CRUD endpoints.

    Currently equivalent to :func:`is_platform_admin`. Kept as a
    distinct function so the call sites read intent rather than role
    membership, and so we can split it later (e.g. read-only platform
    auditors) without churn at every consumer.
    """
    return is_platform_admin(principal)
