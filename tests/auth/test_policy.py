import pytest

from ums_smart_revenue.auth.models import PermissionGrant, RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import (
    can_assign_roles,
    can_change_allocation_rule,
    can_export_finance_report,
    can_lock_month,
    can_manage_connectors,
    can_run_connector_job,
    can_unlock_month,
    can_view_channel_analytics,
    can_view_channel_revenue,
    can_view_company_revenue,
    can_view_neo4j_graph,
)
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex


@pytest.fixture
def org_index() -> OrgAccessIndex:
    return OrgAccessIndex(
        channel_company={
            "channel-tv-a": "company-tv-a",
            "channel-news-a": "company-news-a",
        },
        channel_sector={
            "channel-tv-a": "tv",
            "channel-news-a": "news",
        },
        company_sector={
            "company-tv-a": "tv",
            "company-news-a": "news",
        },
    )


def principal(*assignments: RoleAssignment, grants=None) -> UserPrincipal:
    return UserPrincipal(
        user_id="user-1",
        email="user@example.com",
        role_assignments=list(assignments),
        direct_permissions=list(grants or []),
    )


def assigned(role: RoleKey, scope: AccessScope) -> RoleAssignment:
    return RoleAssignment(role=role, scope=scope)


def granted(permission: Permission, scope: AccessScope) -> PermissionGrant:
    return PermissionGrant(permission=permission, scope=scope)


def test_super_owner_can_see_everything_and_manage_sensitive_controls(org_index):
    user = principal(assigned(RoleKey.SUPER_OWNER, AccessScope.global_scope()))

    assert can_view_channel_analytics(user, "channel-news-a", org_index)
    assert can_view_channel_revenue(user, "channel-news-a", org_index)
    assert can_view_company_revenue(user, "company-news-a", org_index)
    assert can_export_finance_report(
        user, AccessScope.company("company-news-a"), org_index
    )
    assert can_lock_month(user, "2026-03")
    assert can_change_allocation_rule(user, "2026-03")
    assert can_manage_connectors(user)
    assert can_assign_roles(user)


def test_assistant_analyst_can_view_assigned_analytics_but_not_finance_by_default(
    org_index,
):
    user = principal(
        assigned(RoleKey.ASSISTANT_ANALYST, AccessScope.company("company-tv-a"))
    )

    assert can_view_channel_analytics(user, "channel-tv-a", org_index)
    assert not can_view_channel_analytics(user, "channel-news-a", org_index)
    assert not can_view_channel_revenue(user, "channel-tv-a", org_index)
    assert not can_export_finance_report(
        user, AccessScope.company("company-tv-a"), org_index
    )


def test_explicit_finance_grant_allows_assistant_revenue_for_only_that_scope(org_index):
    user = principal(
        assigned(RoleKey.ASSISTANT_ANALYST, AccessScope.company("company-tv-a")),
        grants=[granted(Permission.VIEW_REVENUE, AccessScope.company("company-tv-a"))],
    )

    assert can_view_channel_revenue(user, "channel-tv-a", org_index)
    assert not can_view_channel_revenue(user, "channel-news-a", org_index)


def test_company_manager_cannot_view_another_company(org_index):
    user = principal(
        assigned(RoleKey.COMPANY_MANAGER, AccessScope.company("company-tv-a"))
    )

    assert can_view_channel_analytics(user, "channel-tv-a", org_index)
    assert not can_view_channel_analytics(user, "channel-news-a", org_index)
    assert not can_view_company_revenue(user, "company-news-a", org_index)


def test_export_operator_can_export_revenue_only_with_view_and_export_grants(org_index):
    user = principal(
        assigned(RoleKey.EXPORT_OPERATOR, AccessScope.company("company-tv-a")),
        grants=[
            granted(Permission.VIEW_REVENUE, AccessScope.company("company-tv-a")),
            granted(
                Permission.EXPORT_REVENUE_REPORT, AccessScope.company("company-tv-a")
            ),
        ],
    )

    assert can_export_finance_report(
        user, AccessScope.company("company-tv-a"), org_index
    )
    assert not can_export_finance_report(
        user, AccessScope.company("company-news-a"), org_index
    )
    assert not can_change_allocation_rule(user, "2026-03")


def test_finance_viewer_reads_finance_but_cannot_lock_or_change_allocation(org_index):
    user = principal(assigned(RoleKey.FINANCE_VIEWER, AccessScope.global_scope()))

    assert can_view_company_revenue(user, "company-news-a", org_index)
    assert not can_lock_month(user, "2026-03")
    assert not can_change_allocation_rule(user, "2026-03")


def test_finance_admin_can_lock_month_and_change_allocation_rules():
    user = principal(assigned(RoleKey.FINANCE_ADMIN, AccessScope.global_scope()))

    assert can_lock_month(user, "2026-03")
    assert can_unlock_month(user, "2026-03")
    assert can_change_allocation_rule(user, "2026-03")


def test_connector_admin_can_manage_connectors_but_revenue_ops_can_only_run_jobs():
    connector_admin = principal(
        assigned(RoleKey.CONNECTOR_ADMIN, AccessScope.global_scope())
    )
    revenue_ops = principal(
        assigned(RoleKey.REVENUE_OPERATIONS_ADMIN, AccessScope.global_scope())
    )
    assistant = principal(
        assigned(RoleKey.ASSISTANT_ANALYST, AccessScope.global_scope())
    )

    assert can_manage_connectors(connector_admin)
    assert not can_manage_connectors(revenue_ops)
    assert can_run_connector_job(revenue_ops, "youtube")
    assert not can_run_connector_job(assistant, "youtube")


def test_narrow_connector_scope_does_not_grant_global_connector_admin():
    connector_admin = principal(
        assigned(RoleKey.CONNECTOR_ADMIN, AccessScope.connector("youtube"))
    )

    assert not can_manage_connectors(connector_admin)


def test_graph_finance_view_requires_graph_permission_and_underlying_finance_scope(
    org_index,
):
    assistant = principal(
        assigned(RoleKey.ASSISTANT_ANALYST, AccessScope.company("company-tv-a"))
    )
    finance_viewer = principal(
        assigned(RoleKey.FINANCE_VIEWER, AccessScope.company("company-tv-a"))
    )

    assert can_view_neo4j_graph(
        assistant, AccessScope.company("company-tv-a"), org_index
    )
    assert not can_view_neo4j_graph(
        assistant,
        AccessScope.company("company-tv-a"),
        org_index,
        contains_finance=True,
    )
    assert can_view_neo4j_graph(
        finance_viewer,
        AccessScope.company("company-tv-a"),
        org_index,
        contains_finance=True,
    )


def test_corporate_admin_can_assign_roles_without_finance_visibility(org_index):
    user = principal(assigned(RoleKey.CORPORATE_ADMIN, AccessScope.global_scope()))

    assert can_assign_roles(user)
    assert not can_view_company_revenue(user, "company-tv-a", org_index)
