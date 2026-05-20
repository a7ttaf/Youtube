from dataclasses import dataclass, field
from enum import StrEnum


class RoleKey(StrEnum):
    SUPER_OWNER = "super_owner"
    CORPORATE_ADMIN = "corporate_admin"
    REVENUE_OPERATIONS_ADMIN = "revenue_operations_admin"
    FINANCE_ADMIN = "finance_admin"
    FINANCE_APPROVER = "finance_approver"
    FINANCE_VIEWER = "finance_viewer"
    TV_SECTOR_MANAGER = "tv_sector_manager"
    NEWS_SECTOR_MANAGER = "news_sector_manager"
    COMPANY_MANAGER = "company_manager"
    CHANNEL_MANAGER = "channel_manager"
    ASSISTANT_ANALYST = "assistant_analyst"
    EXPORT_OPERATOR = "export_operator"
    AUDIT_VIEWER = "audit_viewer"
    SYSTEM_INTEGRATION_USER = "system_integration_user"
    CONNECTOR_ADMIN = "connector_admin"
    DATA_STEWARD = "data_steward"


@dataclass(frozen=True)
class RoleDefinition:
    role: RoleKey
    label: str
    description: str
    service_only: bool = False
    allowed_scope_types: frozenset[str] = field(default_factory=frozenset)


_GLOBAL = frozenset({"global"})
_GLOBAL_OR_SECTOR = frozenset({"global", "sector"})
_ORG_SCOPES = frozenset({"global", "sector", "company", "channel"})

ROLE_DEFINITIONS: dict[RoleKey, RoleDefinition] = {
    RoleKey.SUPER_OWNER: RoleDefinition(
        RoleKey.SUPER_OWNER,
        "Super Owner",
        "Global break-glass owner with every platform and finance permission.",
        allowed_scope_types=_GLOBAL,
    ),
    RoleKey.CORPORATE_ADMIN: RoleDefinition(
        RoleKey.CORPORATE_ADMIN,
        "Corporate Admin",
        "Global platform administrator without default finance visibility.",
        allowed_scope_types=_GLOBAL,
    ),
    RoleKey.REVENUE_OPERATIONS_ADMIN: RoleDefinition(
        RoleKey.REVENUE_OPERATIONS_ADMIN,
        "Revenue Operations Admin",
        "Global operational admin for ingestion, registry quality, and analytics.",
        allowed_scope_types=_GLOBAL,
    ),
    RoleKey.FINANCE_ADMIN: RoleDefinition(
        RoleKey.FINANCE_ADMIN,
        "Finance Admin",
        "Finance owner for revenue, reconciliation, overrides, and month close.",
        allowed_scope_types=_GLOBAL,
    ),
    RoleKey.FINANCE_APPROVER: RoleDefinition(
        RoleKey.FINANCE_APPROVER,
        "Finance Approver",
        "Second-control approver for finance overrides and month unlocks.",
        allowed_scope_types=_GLOBAL,
    ),
    RoleKey.FINANCE_VIEWER: RoleDefinition(
        RoleKey.FINANCE_VIEWER,
        "Finance Viewer",
        "Read-only finance role for granted organization scopes.",
        allowed_scope_types=_ORG_SCOPES,
    ),
    RoleKey.TV_SECTOR_MANAGER: RoleDefinition(
        RoleKey.TV_SECTOR_MANAGER,
        "TV Sector Manager",
        "Sector-scoped management role for TV analytics and graph views.",
        allowed_scope_types=_GLOBAL_OR_SECTOR,
    ),
    RoleKey.NEWS_SECTOR_MANAGER: RoleDefinition(
        RoleKey.NEWS_SECTOR_MANAGER,
        "News Sector Manager",
        "Sector-scoped management role for News analytics and graph views.",
        allowed_scope_types=_GLOBAL_OR_SECTOR,
    ),
    RoleKey.COMPANY_MANAGER: RoleDefinition(
        RoleKey.COMPANY_MANAGER,
        "Company Manager",
        "Company-scoped manager for analytics and non-finance graph views.",
        allowed_scope_types=frozenset({"global", "sector", "company"}),
    ),
    RoleKey.CHANNEL_MANAGER: RoleDefinition(
        RoleKey.CHANNEL_MANAGER,
        "Channel Manager",
        "Channel-scoped manager for assigned channel analytics.",
        allowed_scope_types=_ORG_SCOPES,
    ),
    RoleKey.ASSISTANT_ANALYST: RoleDefinition(
        RoleKey.ASSISTANT_ANALYST,
        "Assistant Analyst",
        "Assigned-scope analyst for analytics without default finance access.",
        allowed_scope_types=_ORG_SCOPES,
    ),
    RoleKey.EXPORT_OPERATOR: RoleDefinition(
        RoleKey.EXPORT_OPERATOR,
        "Export Operator",
        "Scoped export operator for approved analytics or finance exports.",
        allowed_scope_types=frozenset({"global", "export"}),
    ),
    RoleKey.AUDIT_VIEWER: RoleDefinition(
        RoleKey.AUDIT_VIEWER,
        "Audit Viewer",
        "Read-only audit and compliance reviewer.",
        allowed_scope_types=_GLOBAL,
    ),
    RoleKey.SYSTEM_INTEGRATION_USER: RoleDefinition(
        RoleKey.SYSTEM_INTEGRATION_USER,
        "System Integration User",
        "Non-human role for scheduled connector jobs and backend service flows.",
        service_only=True,
        allowed_scope_types=frozenset({"global", "connector"}),
    ),
    RoleKey.CONNECTOR_ADMIN: RoleDefinition(
        RoleKey.CONNECTOR_ADMIN,
        "Connector Admin",
        "Technical owner for OAuth/API connector configuration.",
        allowed_scope_types=frozenset({"global", "connector"}),
    ),
    RoleKey.DATA_STEWARD: RoleDefinition(
        RoleKey.DATA_STEWARD,
        "Data Steward",
        "Scoped owner for channel registry, groups, and organization mapping.",
        allowed_scope_types=_ORG_SCOPES,
    ),
}

_missing_definitions = set(RoleKey) - set(ROLE_DEFINITIONS)
if _missing_definitions:
    missing = ", ".join(sorted(role.value for role in _missing_definitions))
    raise RuntimeError(f"Missing role definitions for: {missing}")
