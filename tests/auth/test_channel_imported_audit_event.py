"""The bulk channel import needs its own classified summary audit event type."""

from ums_smart_revenue.auth.audit import AUDIT_EVENT_DEFINITIONS, AuditEventType
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission


def test_channel_imported_event_type_exists() -> None:
    assert AuditEventType.CHANNEL_IMPORTED.value == "CHANNEL_IMPORTED"


def test_channel_imported_is_a_defined_sensitive_event() -> None:
    """CHANNEL_IMPORTED must carry permission metadata, not default to non-sensitive."""
    definition = AUDIT_EVENT_DEFINITIONS[AuditEventType.CHANNEL_IMPORTED]
    assert definition.permission is Permission.MANAGE_CHANNELS
    assert definition.reason_required is True


def test_channel_imported_records_as_sensitive_with_permission() -> None:
    """A recorded CHANNEL_IMPORTED event is classified sensitive with its permission."""
    sink = InMemoryAuditSink()
    record = record_audit_event(
        sink=sink,
        actor=UserPrincipal(user_id="user-1", email="user@example.com"),
        event_type=AuditEventType.CHANNEL_IMPORTED,
        entity_type="youtube_channel_import",
        entity_id="owner-1",
        reason="Quarterly CMS roster load",
    )
    assert record.sensitive is True
    assert record.permission == Permission.MANAGE_CHANNELS.value
