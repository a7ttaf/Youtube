"""The bulk channel import needs its own summary audit event type."""

from ums_smart_revenue.auth.audit import AuditEventType


def test_channel_imported_event_type_exists() -> None:
    assert AuditEventType.CHANNEL_IMPORTED.value == "CHANNEL_IMPORTED"
