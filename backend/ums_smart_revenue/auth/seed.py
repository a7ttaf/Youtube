from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.roles import RoleKey

ALL_PERMISSIONS = frozenset(Permission)

ROLE_PERMISSIONS: dict[RoleKey, frozenset[Permission]] = {
    RoleKey.SUPER_OWNER: ALL_PERMISSIONS,
    RoleKey.CORPORATE_ADMIN: frozenset(
        {
            Permission.VIEW_ANALYTICS,
            Permission.VIEW_CONFIDENCE,
            Permission.EXPORT_ANALYTICS_REPORT,
            Permission.MANAGE_EXPORT_TEMPLATES,
            Permission.MANAGE_CHANNELS,
            Permission.MANAGE_ORG_MAPPING,
            Permission.MANAGE_GROUPS,
            Permission.VIEW_CONNECTOR_HEALTH,
            Permission.VIEW_AUDIT_LOG,
            Permission.MANAGE_USERS,
            Permission.ASSIGN_ROLES,
            Permission.MANAGE_PLATFORM_SETTINGS,
        }
    ),
    RoleKey.REVENUE_OPERATIONS_ADMIN: frozenset(
        {
            Permission.VIEW_ANALYTICS,
            Permission.VIEW_CONFIDENCE,
            Permission.EXPORT_ANALYTICS_REPORT,
            Permission.MANAGE_CHANNELS,
            Permission.MANAGE_ORG_MAPPING,
            Permission.MANAGE_GROUPS,
            Permission.VIEW_CONNECTOR_HEALTH,
            Permission.RUN_CONNECTOR_JOBS,
        }
    ),
    RoleKey.FINANCE_ADMIN: frozenset(
        {
            Permission.VIEW_ANALYTICS,
            Permission.VIEW_CONFIDENCE,
            Permission.VIEW_REVENUE,
            Permission.VIEW_FINALIZED_PAYMENTS,
            Permission.VIEW_BANK_RECONCILIATION,
            Permission.MANAGE_BANK_RECONCILIATION,
            Permission.CREATE_MANUAL_OVERRIDE,
            Permission.APPROVE_MANUAL_OVERRIDE,
            Permission.LOCK_FINANCE_MONTH,
            Permission.UNLOCK_FINANCE_MONTH,
            Permission.CHANGE_ALLOCATION_RULE,
            Permission.EXPORT_ANALYTICS_REPORT,
            Permission.EXPORT_REVENUE_REPORT,
            Permission.VIEW_AUDIT_LOG,
            Permission.ASSIGN_ROLES,
        }
    ),
    RoleKey.BETA_OPERATOR: frozenset(
        {
            Permission.VIEW_ANALYTICS,
            Permission.VIEW_CONFIDENCE,
            Permission.VIEW_REVENUE,
            Permission.VIEW_FINALIZED_PAYMENTS,
            Permission.VIEW_BANK_RECONCILIATION,
            Permission.MANAGE_BANK_RECONCILIATION,
            Permission.CREATE_MANUAL_OVERRIDE,
            Permission.APPROVE_MANUAL_OVERRIDE,
            Permission.LOCK_FINANCE_MONTH,
            Permission.UNLOCK_FINANCE_MONTH,
            Permission.CHANGE_ALLOCATION_RULE,
            Permission.EXPORT_ANALYTICS_REPORT,
            Permission.EXPORT_REVENUE_REPORT,
            Permission.VIEW_AUDIT_LOG,
            Permission.IMPORT_MANUAL_REVENUE,
        }
    ),
    RoleKey.FINANCE_APPROVER: frozenset(
        {
            Permission.VIEW_ANALYTICS,
            Permission.VIEW_CONFIDENCE,
            Permission.VIEW_REVENUE,
            Permission.VIEW_FINALIZED_PAYMENTS,
            Permission.VIEW_BANK_RECONCILIATION,
            Permission.MANAGE_BANK_RECONCILIATION,
            Permission.APPROVE_MANUAL_OVERRIDE,
            Permission.UNLOCK_FINANCE_MONTH,
            Permission.CHANGE_ALLOCATION_RULE,
            Permission.EXPORT_REVENUE_REPORT,
            Permission.VIEW_AUDIT_LOG,
        }
    ),
    RoleKey.FINANCE_VIEWER: frozenset(
        {
            Permission.VIEW_ANALYTICS,
            Permission.VIEW_CONFIDENCE,
            Permission.VIEW_REVENUE,
            Permission.VIEW_FINALIZED_PAYMENTS,
            Permission.VIEW_BANK_RECONCILIATION,
        }
    ),
    RoleKey.TV_SECTOR_MANAGER: frozenset(
        {
            Permission.VIEW_ANALYTICS,
            Permission.VIEW_CONFIDENCE,
            Permission.EXPORT_ANALYTICS_REPORT,
        }
    ),
    RoleKey.NEWS_SECTOR_MANAGER: frozenset(
        {
            Permission.VIEW_ANALYTICS,
            Permission.VIEW_CONFIDENCE,
            Permission.EXPORT_ANALYTICS_REPORT,
        }
    ),
    RoleKey.COMPANY_MANAGER: frozenset(
        {
            Permission.VIEW_ANALYTICS,
            Permission.VIEW_CONFIDENCE,
            Permission.EXPORT_ANALYTICS_REPORT,
        }
    ),
    RoleKey.CHANNEL_MANAGER: frozenset(
        {
            Permission.VIEW_ANALYTICS,
            Permission.VIEW_CONFIDENCE,
            Permission.EXPORT_ANALYTICS_REPORT,
        }
    ),
    RoleKey.ASSISTANT_ANALYST: frozenset(
        {
            Permission.VIEW_ANALYTICS,
            Permission.VIEW_CONFIDENCE,
        }
    ),
    RoleKey.EXPORT_OPERATOR: frozenset(
        {
            Permission.VIEW_ANALYTICS,
            Permission.VIEW_CONFIDENCE,
            Permission.EXPORT_ANALYTICS_REPORT,
        }
    ),
    RoleKey.AUDIT_VIEWER: frozenset(
        {
            Permission.VIEW_AUDIT_LOG,
        }
    ),
    RoleKey.SYSTEM_INTEGRATION_USER: frozenset(
        {
            Permission.VIEW_CONNECTOR_HEALTH,
            Permission.RUN_CONNECTOR_JOBS,
        }
    ),
    RoleKey.CONNECTOR_ADMIN: frozenset(
        {
            Permission.VIEW_CONNECTOR_HEALTH,
            Permission.RUN_CONNECTOR_JOBS,
            Permission.MANAGE_CONNECTORS,
            Permission.VIEW_RAW_FILES,
        }
    ),
    RoleKey.DATA_STEWARD: frozenset(
        {
            Permission.VIEW_ANALYTICS,
            Permission.VIEW_CONFIDENCE,
            Permission.MANAGE_CHANNELS,
            Permission.MANAGE_ORG_MAPPING,
            Permission.MANAGE_GROUPS,
        }
    ),
}


def initial_role_permission_rows() -> list[dict[str, str]]:
    """Flatten the role/permission registry into deterministic seed rows."""
    rows: list[dict[str, str]] = []
    for role, permissions in sorted(ROLE_PERMISSIONS.items(), key=lambda item: item[0].value):
        for permission in sorted(permissions, key=lambda item: item.value):
            rows.append({"role": role.value, "permission": permission.value})
    return rows
