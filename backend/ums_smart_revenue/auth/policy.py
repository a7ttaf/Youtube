from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex
from ums_smart_revenue.auth.seed import ROLE_PERMISSIONS


EMPTY_ORG_INDEX = OrgAccessIndex()


def has_permission(
    user: UserPrincipal,
    permission: Permission,
    scope: AccessScope,
    org_index: OrgAccessIndex | None = None,
) -> bool:
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


def can_view_channel_analytics(
    user: UserPrincipal,
    channel_id: str,
    org_index: OrgAccessIndex,
) -> bool:
    return has_permission(user, Permission.VIEW_ANALYTICS, AccessScope.channel(channel_id), org_index)


def can_view_channel_revenue(
    user: UserPrincipal,
    channel_id: str,
    org_index: OrgAccessIndex,
) -> bool:
    return has_permission(user, Permission.VIEW_REVENUE, AccessScope.channel(channel_id), org_index)


def can_view_company_revenue(
    user: UserPrincipal,
    company_id: str,
    org_index: OrgAccessIndex,
) -> bool:
    return has_permission(user, Permission.VIEW_REVENUE, AccessScope.company(company_id), org_index)


def can_export_finance_report(
    user: UserPrincipal,
    scope: AccessScope,
    org_index: OrgAccessIndex,
) -> bool:
    return has_permission(user, Permission.EXPORT_REVENUE_REPORT, scope, org_index) and has_permission(
        user,
        Permission.VIEW_REVENUE,
        scope,
        org_index,
    )


def can_lock_month(user: UserPrincipal, month: str) -> bool:
    return has_permission(user, Permission.LOCK_FINANCE_MONTH, AccessScope.finance_month(month))


def can_unlock_month(user: UserPrincipal, month: str) -> bool:
    return has_permission(user, Permission.UNLOCK_FINANCE_MONTH, AccessScope.finance_month(month))


def can_change_allocation_rule(user: UserPrincipal, month: str) -> bool:
    return has_permission(user, Permission.CHANGE_ALLOCATION_RULE, AccessScope.finance_month(month))


def can_view_neo4j_graph(
    user: UserPrincipal,
    scope: AccessScope,
    org_index: OrgAccessIndex,
    *,
    contains_finance: bool = False,
) -> bool:
    if not has_permission(user, Permission.VIEW_GRAPH, scope, org_index):
        return False
    if not contains_finance:
        return True
    return has_permission(user, Permission.VIEW_GRAPH_FINANCE, scope, org_index) and has_permission(
        user,
        Permission.VIEW_REVENUE,
        scope,
        org_index,
    )


def can_manage_connectors(user: UserPrincipal) -> bool:
    return has_permission(user, Permission.MANAGE_CONNECTORS, AccessScope.global_scope())


def can_run_connector_job(user: UserPrincipal, connector_id: str) -> bool:
    return has_permission(user, Permission.RUN_CONNECTOR_JOBS, AccessScope.connector(connector_id))


def can_assign_roles(user: UserPrincipal) -> bool:
    return has_permission(user, Permission.ASSIGN_ROLES, AccessScope.global_scope())

