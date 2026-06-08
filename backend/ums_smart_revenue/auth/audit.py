"""Audit event types and structured audit record dataclasses."""
from dataclasses import dataclass
from enum import StrEnum

from ums_smart_revenue.auth.permissions import Permission


class AuditEventType(StrEnum):
    """Enumeration of audit event types used for logging and tracking system actions."""

    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    CHANNEL_CREATED = "CHANNEL_CREATED"
    CHANNEL_UPDATED = "CHANNEL_UPDATED"
    GROUP_UPDATED = "GROUP_UPDATED"
    REPORT_IMPORTED = "REPORT_IMPORTED"
    ADSENSE_PAYMENT_SYNCED = "ADSENSE_PAYMENT_SYNCED"
    DEDUCTION_COMPONENTS_INGESTED = "DEDUCTION_COMPONENTS_INGESTED"
    REVENUE_RECONCILED = "REVENUE_RECONCILED"
    REPORT_PURGED = "REPORT_PURGED"
    MONTH_CLOSE_UPDATED = "MONTH_CLOSE_UPDATED"
    MONTH_CLOSE_VIEWED = "MONTH_CLOSE_VIEWED"
    MONTH_LOCKED = "MONTH_LOCKED"
    MONTH_UNLOCKED = "MONTH_UNLOCKED"
    MANUAL_OVERRIDE_CREATED = "MANUAL_OVERRIDE_CREATED"
    MANUAL_OVERRIDE_APPROVED = "MANUAL_OVERRIDE_APPROVED"
    ALLOCATION_RULE_CHANGED = "ALLOCATION_RULE_CHANGED"
    ALLOCATION_COMMITTED = "ALLOCATION_COMMITTED"
    RECALCULATION_REQUESTED = "RECALCULATION_REQUESTED"
    EXPORT_CREATED = "EXPORT_CREATED"
    EXPORT_VIEWED = "EXPORT_VIEWED"
    EXPORT_DOWNLOADED = "EXPORT_DOWNLOADED"
    USER_ACCOUNT_CHANGED = "USER_ACCOUNT_CHANGED"
    USER_ROLE_CHANGED = "USER_ROLE_CHANGED"
    USER_PERMISSION_CHANGED = "USER_PERMISSION_CHANGED"
    CONNECTOR_JOB_RUN = "CONNECTOR_JOB_RUN"
    CONNECTOR_SETTINGS_CHANGED = "CONNECTOR_SETTINGS_CHANGED"
    CONNECTOR_TESTED = "CONNECTOR_TESTED"
    EXCHANGE_RATE_SYNCED = "EXCHANGE_RATE_SYNCED"
    RAW_FILE_VIEWED = "RAW_FILE_VIEWED"
    REVENUE_VIEWED = "REVENUE_VIEWED"
    PAYMENT_VIEWED = "PAYMENT_VIEWED"
    BANK_RECONCILIATION_RECORDED = "BANK_RECONCILIATION_RECORDED"
    BANK_RECONCILIATION_VIEWED = "BANK_RECONCILIATION_VIEWED"
    AUDIT_LOG_VIEWED = "AUDIT_LOG_VIEWED"
    CHANNEL_ACCOUNT_LINK_PROPOSED = "CHANNEL_ACCOUNT_LINK_PROPOSED"
    CHANNEL_ACCOUNT_LINK_VERIFIED = "CHANNEL_ACCOUNT_LINK_VERIFIED"
    CHANNEL_ACCOUNT_LINK_REJECTED = "CHANNEL_ACCOUNT_LINK_REJECTED"


@dataclass(frozen=True)
class AuditEventDefinition:
    """Define audit event type, reason, and permission requirements."""

    event_type: AuditEventType
    reason_required: bool = False
    permission: Permission | None = None


# Events omitted here intentionally have no permission marker or reason requirement.
# record_audit_event handles them via .get() and records them as non-sensitive.
AUDIT_EVENT_DEFINITIONS: dict[AuditEventType, AuditEventDefinition] = {
    AuditEventType.CHANNEL_CREATED: AuditEventDefinition(
        AuditEventType.CHANNEL_CREATED,
        permission=Permission.MANAGE_CHANNELS,
    ),
    AuditEventType.CHANNEL_UPDATED: AuditEventDefinition(
        AuditEventType.CHANNEL_UPDATED,
        reason_required=True,
        permission=Permission.MANAGE_ORG_MAPPING,
    ),
    AuditEventType.GROUP_UPDATED: AuditEventDefinition(
        AuditEventType.GROUP_UPDATED,
        reason_required=True,
        permission=Permission.MANAGE_GROUPS,
    ),
    AuditEventType.MONTH_CLOSE_VIEWED: AuditEventDefinition(
        AuditEventType.MONTH_CLOSE_VIEWED,
        permission=Permission.VIEW_REVENUE,
    ),
    AuditEventType.MONTH_LOCKED: AuditEventDefinition(
        AuditEventType.MONTH_LOCKED,
        reason_required=True,
        permission=Permission.LOCK_FINANCE_MONTH,
    ),
    AuditEventType.MONTH_UNLOCKED: AuditEventDefinition(
        AuditEventType.MONTH_UNLOCKED,
        reason_required=True,
        permission=Permission.UNLOCK_FINANCE_MONTH,
    ),
    AuditEventType.MANUAL_OVERRIDE_CREATED: AuditEventDefinition(
        AuditEventType.MANUAL_OVERRIDE_CREATED,
        reason_required=True,
        permission=Permission.CREATE_MANUAL_OVERRIDE,
    ),
    AuditEventType.MANUAL_OVERRIDE_APPROVED: AuditEventDefinition(
        AuditEventType.MANUAL_OVERRIDE_APPROVED,
        reason_required=True,
        permission=Permission.APPROVE_MANUAL_OVERRIDE,
    ),
    AuditEventType.ALLOCATION_RULE_CHANGED: AuditEventDefinition(
        AuditEventType.ALLOCATION_RULE_CHANGED,
        reason_required=True,
        permission=Permission.CHANGE_ALLOCATION_RULE,
    ),
    AuditEventType.ALLOCATION_COMMITTED: AuditEventDefinition(
        AuditEventType.ALLOCATION_COMMITTED,
        reason_required=True,
        permission=Permission.CHANGE_ALLOCATION_RULE,
    ),
    AuditEventType.RECALCULATION_REQUESTED: AuditEventDefinition(
        AuditEventType.RECALCULATION_REQUESTED,
        reason_required=True,
        permission=Permission.CHANGE_ALLOCATION_RULE,
    ),
    AuditEventType.EXPORT_CREATED: AuditEventDefinition(
        AuditEventType.EXPORT_CREATED,
        permission=Permission.EXPORT_REVENUE_REPORT,
    ),
    AuditEventType.EXPORT_VIEWED: AuditEventDefinition(
        AuditEventType.EXPORT_VIEWED,
        permission=Permission.EXPORT_REVENUE_REPORT,
    ),
    AuditEventType.EXPORT_DOWNLOADED: AuditEventDefinition(
        AuditEventType.EXPORT_DOWNLOADED,
        permission=Permission.EXPORT_REVENUE_REPORT,
    ),
    AuditEventType.USER_ACCOUNT_CHANGED: AuditEventDefinition(
        AuditEventType.USER_ACCOUNT_CHANGED,
        reason_required=True,
        permission=Permission.MANAGE_USERS,
    ),
    AuditEventType.USER_ROLE_CHANGED: AuditEventDefinition(
        AuditEventType.USER_ROLE_CHANGED,
        reason_required=True,
        permission=Permission.ASSIGN_ROLES,
    ),
    AuditEventType.USER_PERMISSION_CHANGED: AuditEventDefinition(
        AuditEventType.USER_PERMISSION_CHANGED,
        reason_required=True,
        permission=Permission.ASSIGN_ROLES,
    ),
    AuditEventType.CONNECTOR_JOB_RUN: AuditEventDefinition(
        AuditEventType.CONNECTOR_JOB_RUN,
        permission=Permission.RUN_CONNECTOR_JOBS,
    ),
    AuditEventType.REPORT_IMPORTED: AuditEventDefinition(
        AuditEventType.REPORT_IMPORTED,
        permission=Permission.RUN_CONNECTOR_JOBS,
    ),
    AuditEventType.ADSENSE_PAYMENT_SYNCED: AuditEventDefinition(
        AuditEventType.ADSENSE_PAYMENT_SYNCED,
        permission=Permission.RUN_CONNECTOR_JOBS,
    ),
    AuditEventType.DEDUCTION_COMPONENTS_INGESTED: AuditEventDefinition(
        AuditEventType.DEDUCTION_COMPONENTS_INGESTED,
        reason_required=True,
        permission=Permission.RUN_CONNECTOR_JOBS,
    ),
    AuditEventType.CONNECTOR_SETTINGS_CHANGED: AuditEventDefinition(
        AuditEventType.CONNECTOR_SETTINGS_CHANGED,
        reason_required=True,
        permission=Permission.MANAGE_CONNECTORS,
    ),
    AuditEventType.CONNECTOR_TESTED: AuditEventDefinition(
        AuditEventType.CONNECTOR_TESTED,
        reason_required=True,
        permission=Permission.MANAGE_CONNECTORS,
    ),
    AuditEventType.EXCHANGE_RATE_SYNCED: AuditEventDefinition(
        AuditEventType.EXCHANGE_RATE_SYNCED,
        reason_required=True,
        permission=Permission.RUN_CONNECTOR_JOBS,
    ),
    AuditEventType.RAW_FILE_VIEWED: AuditEventDefinition(
        AuditEventType.RAW_FILE_VIEWED,
        permission=Permission.VIEW_RAW_FILES,
    ),
    AuditEventType.REVENUE_VIEWED: AuditEventDefinition(
        AuditEventType.REVENUE_VIEWED,
        permission=Permission.VIEW_REVENUE,
    ),
    AuditEventType.PAYMENT_VIEWED: AuditEventDefinition(
        AuditEventType.PAYMENT_VIEWED,
        permission=Permission.VIEW_FINALIZED_PAYMENTS,
    ),
    AuditEventType.BANK_RECONCILIATION_RECORDED: AuditEventDefinition(
        AuditEventType.BANK_RECONCILIATION_RECORDED,
        reason_required=True,
        permission=Permission.MANAGE_BANK_RECONCILIATION,
    ),
    AuditEventType.BANK_RECONCILIATION_VIEWED: AuditEventDefinition(
        AuditEventType.BANK_RECONCILIATION_VIEWED,
        permission=Permission.VIEW_BANK_RECONCILIATION,
    ),
    AuditEventType.AUDIT_LOG_VIEWED: AuditEventDefinition(
        AuditEventType.AUDIT_LOG_VIEWED,
        permission=Permission.VIEW_AUDIT_LOG,
    ),
    AuditEventType.CHANNEL_ACCOUNT_LINK_PROPOSED: AuditEventDefinition(
        AuditEventType.CHANNEL_ACCOUNT_LINK_PROPOSED,
        reason_required=True,
        permission=Permission.MANAGE_ORG_MAPPING,
    ),
    AuditEventType.CHANNEL_ACCOUNT_LINK_VERIFIED: AuditEventDefinition(
        AuditEventType.CHANNEL_ACCOUNT_LINK_VERIFIED,
        reason_required=True,
        permission=Permission.CHANGE_ALLOCATION_RULE,
    ),
    AuditEventType.CHANNEL_ACCOUNT_LINK_REJECTED: AuditEventDefinition(
        AuditEventType.CHANNEL_ACCOUNT_LINK_REJECTED,
        reason_required=True,
        permission=Permission.CHANGE_ALLOCATION_RULE,
    ),
}
