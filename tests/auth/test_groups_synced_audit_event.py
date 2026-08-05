"""The CMS group sync needs its own summary audit event type."""

from ums_smart_revenue.auth.audit import (
    AUDIT_EVENT_DEFINITIONS,
    AuditEventType,
)
from ums_smart_revenue.auth.permissions import Permission


def test_groups_synced_event_type_exists() -> None:
    assert AuditEventType.GROUPS_SYNCED.value == "GROUPS_SYNCED"


def test_groups_synced_requires_reason_and_manage_groups() -> None:
    definition = AUDIT_EVENT_DEFINITIONS[AuditEventType.GROUPS_SYNCED]
    assert definition.reason_required is True
    assert definition.permission is Permission.MANAGE_GROUPS
