"""The three channel-account-link audit events are defined, sensitive, reason-required."""
from ums_smart_revenue.auth.audit import (
    AUDIT_EVENT_DEFINITIONS,
    AuditEventType,
)
from ums_smart_revenue.auth.permissions import SENSITIVE_PERMISSIONS, Permission


def test_link_audit_events_exist_with_reason_and_permissions():
    proposed = AUDIT_EVENT_DEFINITIONS[AuditEventType.CHANNEL_ACCOUNT_LINK_PROPOSED]
    verified = AUDIT_EVENT_DEFINITIONS[AuditEventType.CHANNEL_ACCOUNT_LINK_VERIFIED]
    rejected = AUDIT_EVENT_DEFINITIONS[AuditEventType.CHANNEL_ACCOUNT_LINK_REJECTED]

    assert proposed.reason_required is True
    assert proposed.permission == Permission.MANAGE_ORG_MAPPING
    assert verified.reason_required is True
    assert verified.permission == Permission.CHANGE_ALLOCATION_RULE
    assert rejected.reason_required is True
    assert rejected.permission == Permission.CHANGE_ALLOCATION_RULE
    # All three are sensitive because their permissions are sensitive.
    assert Permission.MANAGE_ORG_MAPPING in SENSITIVE_PERMISSIONS
    assert Permission.CHANGE_ALLOCATION_RULE in SENSITIVE_PERMISSIONS
