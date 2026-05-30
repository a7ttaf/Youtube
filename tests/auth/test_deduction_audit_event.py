"""The deduction ingestion audit event must be registered and sensitive."""
from ums_smart_revenue.auth.audit import (
    AUDIT_EVENT_DEFINITIONS,
    AuditEventType,
)
from ums_smart_revenue.auth.permissions import SENSITIVE_PERMISSIONS, Permission


def test_deduction_components_ingested_event_exists():
    """Ensure the deduction ingestion audit event type is defined."""
    assert AuditEventType.DEDUCTION_COMPONENTS_INGESTED.value == "DEDUCTION_COMPONENTS_INGESTED"


def test_deduction_components_ingested_is_sensitive_via_run_connector_jobs():
    """Ensure deduction ingestion is sensitive and connector-scoped."""
    definition = AUDIT_EVENT_DEFINITIONS[AuditEventType.DEDUCTION_COMPONENTS_INGESTED]
    assert definition.reason_required is True
    assert definition.permission == Permission.RUN_CONNECTOR_JOBS
    assert definition.permission in SENSITIVE_PERMISSIONS
