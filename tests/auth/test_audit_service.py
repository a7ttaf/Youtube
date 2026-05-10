import pytest

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditRecord, InMemoryAuditSink, record_audit_event
from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope


def principal() -> UserPrincipal:
    return UserPrincipal(
        user_id="user-1",
        email="finance@example.com",
        role_assignments=[RoleAssignment(role=RoleKey.FINANCE_ADMIN, scope=AccessScope.global_scope())],
    )


def test_sensitive_audit_event_requires_reason_when_policy_requires_it():
    sink = InMemoryAuditSink()

    with pytest.raises(ValueError, match="requires a reason"):
        record_audit_event(
            sink=sink,
            actor=principal(),
            event_type=AuditEventType.MANUAL_OVERRIDE_CREATED,
            entity_type="channel",
            entity_id="channel-tv-a",
            scope=AccessScope.finance_month("2026-03"),
            details={"field_name": "manual_adjustment_usd"},
        )

    assert sink.records == []


def test_sensitive_audit_event_rejects_whitespace_reason():
    sink = InMemoryAuditSink()

    with pytest.raises(ValueError, match="requires a reason"):
        record_audit_event(
            sink=sink,
            actor=principal(),
            event_type=AuditEventType.MANUAL_OVERRIDE_CREATED,
            entity_type="channel",
            entity_id="channel-tv-a",
            scope=AccessScope.finance_month("2026-03"),
            reason="   ",
            details={"field_name": "manual_adjustment_usd"},
        )

    assert sink.records == []


def test_record_audit_event_stores_trimmed_reason():
    sink = InMemoryAuditSink()

    record = record_audit_event(
        sink=sink,
        actor=principal(),
        event_type=AuditEventType.CHANNEL_UPDATED,
        entity_type="youtube_channel",
        entity_id="channel-tv-a",
        scope=AccessScope.company("company-tv-a"),
        reason="  Correct ownership mapping  ",
        details={"field_name": "primary_company_id"},
    )

    assert record.reason == "Correct ownership mapping"
    assert sink.records == [record]


def test_record_audit_event_marks_sensitive_event_and_scope():
    sink = InMemoryAuditSink()

    record = record_audit_event(
        sink=sink,
        actor=principal(),
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="channel",
        entity_id="channel-tv-a",
        scope=AccessScope.channel("channel-tv-a"),
        details={"metric": "net_revenue"},
    )

    assert isinstance(record, AuditRecord)
    assert record.sensitive is True
    assert record.permission == "finance.view_revenue"
    assert record.scope_type == "channel"
    assert record.scope_id == "channel-tv-a"
    assert sink.records == [record]

