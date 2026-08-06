# ============================================================================
# Purpose: Pin that CMS group sync has its OWN summary audit event type with
#   the right permission and sensitivity, rather than borrowing another.
# Database/ORM: None. The audit event catalogue is a pure in-code definition.
# Standards: An event type is part of the audit contract — auditors filter on
#   it — so its permission binding is asserted explicitly rather than inherited
#   silently from a definition default.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/auth/audit.py -> subject.
#   - File: backend/ums_smart_revenue/org/channel_group_sync_apply.py -> emits
#     the per-group events this summary accompanies.
# ============================================================================
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
