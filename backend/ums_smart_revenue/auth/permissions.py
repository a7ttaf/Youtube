from dataclasses import dataclass
from enum import StrEnum


class Permission(StrEnum):
    """Every permission key the platform can grant or check."""

    VIEW_ANALYTICS = "analytics.view"
    VIEW_CONFIDENCE = "analytics.view_confidence"
    VIEW_REVENUE = "finance.view_revenue"
    VIEW_FINALIZED_PAYMENTS = "finance.view_finalized_payments"
    VIEW_BANK_RECONCILIATION = "finance.view_bank_reconciliation"
    MANAGE_BANK_RECONCILIATION = "finance.manage_bank_reconciliation"
    CREATE_MANUAL_OVERRIDE = "finance.create_manual_override"
    APPROVE_MANUAL_OVERRIDE = "finance.approve_manual_override"
    LOCK_FINANCE_MONTH = "finance.lock_month"
    UNLOCK_FINANCE_MONTH = "finance.unlock_month"
    CHANGE_ALLOCATION_RULE = "finance.change_allocation_rule"
    EXPORT_ANALYTICS_REPORT = "exports.analytics"
    EXPORT_REVENUE_REPORT = "exports.revenue"
    MANAGE_EXPORT_TEMPLATES = "exports.manage_templates"
    IMPORT_MANUAL_REVENUE = "finance.import_manual_revenue"
    MANAGE_CHANNELS = "registry.manage_channels"
    MANAGE_ORG_MAPPING = "registry.manage_org_mapping"
    MANAGE_GROUPS = "registry.manage_groups"
    VIEW_CONNECTOR_HEALTH = "connectors.view_health"
    RUN_CONNECTOR_JOBS = "connectors.run_jobs"
    MANAGE_CONNECTORS = "connectors.manage"
    VIEW_RAW_FILES = "raw_files.view"
    VIEW_AUDIT_LOG = "audit.view"
    VIEW_SENSITIVE_AUDIT_PAYLOADS = "audit.view_sensitive_payloads"
    MANAGE_USERS = "users.manage"
    ASSIGN_ROLES = "roles.assign"
    MANAGE_PLATFORM_SETTINGS = "platform.manage_settings"


@dataclass(frozen=True)
class PermissionDefinition:
    """Admin-facing metadata and sensitivity flags for one permission."""

    permission: Permission
    label: str
    sensitive: bool
    audit_on_use: bool = False


PERMISSION_DEFINITIONS: dict[Permission, PermissionDefinition] = {
    Permission.VIEW_ANALYTICS: PermissionDefinition(
        Permission.VIEW_ANALYTICS,
        "View performance analytics",
        sensitive=False,
    ),
    Permission.VIEW_CONFIDENCE: PermissionDefinition(
        Permission.VIEW_CONFIDENCE,
        "View confidence labels and issue flags",
        sensitive=False,
    ),
    Permission.VIEW_REVENUE: PermissionDefinition(
        Permission.VIEW_REVENUE,
        "View revenue values",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.VIEW_FINALIZED_PAYMENTS: PermissionDefinition(
        Permission.VIEW_FINALIZED_PAYMENTS,
        "View finalized payments",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.VIEW_BANK_RECONCILIATION: PermissionDefinition(
        Permission.VIEW_BANK_RECONCILIATION,
        "View bank reconciliation",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.MANAGE_BANK_RECONCILIATION: PermissionDefinition(
        Permission.MANAGE_BANK_RECONCILIATION,
        "Manage bank reconciliation",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.CREATE_MANUAL_OVERRIDE: PermissionDefinition(
        Permission.CREATE_MANUAL_OVERRIDE,
        "Create manual override",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.APPROVE_MANUAL_OVERRIDE: PermissionDefinition(
        Permission.APPROVE_MANUAL_OVERRIDE,
        "Approve manual override",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.LOCK_FINANCE_MONTH: PermissionDefinition(
        Permission.LOCK_FINANCE_MONTH,
        "Lock finance month",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.UNLOCK_FINANCE_MONTH: PermissionDefinition(
        Permission.UNLOCK_FINANCE_MONTH,
        "Unlock finance month",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.CHANGE_ALLOCATION_RULE: PermissionDefinition(
        Permission.CHANGE_ALLOCATION_RULE,
        "Change allocation rule",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.EXPORT_ANALYTICS_REPORT: PermissionDefinition(
        Permission.EXPORT_ANALYTICS_REPORT,
        "Export analytics report",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.EXPORT_REVENUE_REPORT: PermissionDefinition(
        Permission.EXPORT_REVENUE_REPORT,
        "Export revenue report",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.MANAGE_EXPORT_TEMPLATES: PermissionDefinition(
        Permission.MANAGE_EXPORT_TEMPLATES,
        "Manage export templates",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.IMPORT_MANUAL_REVENUE: PermissionDefinition(
        Permission.IMPORT_MANUAL_REVENUE,
        "Import manual revenue facts",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.MANAGE_CHANNELS: PermissionDefinition(
        Permission.MANAGE_CHANNELS,
        "Manage channel registry",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.MANAGE_ORG_MAPPING: PermissionDefinition(
        Permission.MANAGE_ORG_MAPPING,
        "Manage organization mapping",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.MANAGE_GROUPS: PermissionDefinition(
        Permission.MANAGE_GROUPS,
        "Manage channel groups",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.VIEW_CONNECTOR_HEALTH: PermissionDefinition(
        Permission.VIEW_CONNECTOR_HEALTH,
        "View connector health",
        sensitive=False,
    ),
    Permission.RUN_CONNECTOR_JOBS: PermissionDefinition(
        Permission.RUN_CONNECTOR_JOBS,
        "Run connector jobs",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.MANAGE_CONNECTORS: PermissionDefinition(
        Permission.MANAGE_CONNECTORS,
        "Manage connectors",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.VIEW_RAW_FILES: PermissionDefinition(
        Permission.VIEW_RAW_FILES,
        "View raw report files",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.VIEW_AUDIT_LOG: PermissionDefinition(
        Permission.VIEW_AUDIT_LOG,
        "View audit log",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.VIEW_SENSITIVE_AUDIT_PAYLOADS: PermissionDefinition(
        Permission.VIEW_SENSITIVE_AUDIT_PAYLOADS,
        "View sensitive audit payloads",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.MANAGE_USERS: PermissionDefinition(
        Permission.MANAGE_USERS,
        "Manage users",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.ASSIGN_ROLES: PermissionDefinition(
        Permission.ASSIGN_ROLES,
        "Assign roles",
        sensitive=True,
        audit_on_use=True,
    ),
    Permission.MANAGE_PLATFORM_SETTINGS: PermissionDefinition(
        Permission.MANAGE_PLATFORM_SETTINGS,
        "Manage platform settings",
        sensitive=True,
        audit_on_use=True,
    ),
}


SENSITIVE_PERMISSIONS = frozenset(
    permission for permission, definition in PERMISSION_DEFINITIONS.items() if definition.sensitive
)


if set(PERMISSION_DEFINITIONS) != set(Permission):
    missing = sorted(
        permission.value for permission in set(Permission) - set(PERMISSION_DEFINITIONS)
    )
    extra = sorted(permission.value for permission in set(PERMISSION_DEFINITIONS) - set(Permission))
    raise RuntimeError(f"Permission definition coverage mismatch. missing={missing} extra={extra}")

_mismatched_permission_definitions = sorted(
    permission.value
    for permission, definition in PERMISSION_DEFINITIONS.items()
    if definition.permission != permission
)
if _mismatched_permission_definitions:
    raise RuntimeError(f"Permission metadata key mismatch: {_mismatched_permission_definitions}")
