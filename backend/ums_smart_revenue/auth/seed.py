# ============================================================================
# Purpose: Canonical Python registry of role -> permission assignments used to
#   seed the authorization catalog (ROLE_PERMISSIONS + row builders).
# Database/ORM: Source-of-truth data consumed by migrations, security_seed.sql,
#   and the frozen snapshot modules; no statements are emitted here.
# Standards: Must stay synchronized with db/security_seed.sql and the current
#   frozen snapshot; role keys restricted to each RoleDefinition's
#   allowed_scope_types.
# Blast Radius: Authorization catalog — a wrong pair grants or denies real
#   access after the next seed/repair.
# Connections:
#   - File: backend/ums_smart_revenue/db/security_seed.sql -> raw SQL twin.
#   - File: backend/ums_smart_revenue/db/frozen_security_catalog_20260825_0002.py
#     -> current frozen snapshot.
# ============================================================================
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
    """Return the canonical role-to-permission seed rows.

    The rows mirror security_seed.sql's assignments and feed the Alembic
    seed migration and the backup tool's dynamic security floor.
    """
    rows: list[dict[str, str]] = []
    for role, permissions in sorted(ROLE_PERMISSIONS.items(), key=lambda item: item[0].value):
        for permission in sorted(permissions, key=lambda item: item.value):
            rows.append({"role": role.value, "permission": permission.value})
    return rows
