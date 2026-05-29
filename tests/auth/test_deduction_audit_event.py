"""The deduction ingestion audit event must be registered and sensitive."""
from ums_smart_revenue.auth.audit import (
    AUDIT_EVENT_DEFINITIONS,
    AuditEventType,
)
from ums_smart_revenue.auth.permissions import SENSITIVE_PERMISSIONS


def test_deduction_components_ingested_event_exists():
    assert AuditEventType.DEDUCTION_COMPONENTS_INGESTED.value == "DEDUCTION_COMPONENTS_INGESTED"


def test_deduction_components_ingested_is_sensitive_via_run_connector_jobs():
    definition = AUDIT_EVENT_DEFINITIONS[AuditEventType.DEDUCTION_COMPONENTS_INGESTED]
    assert definition.permission is not None
    assert definition.permission in SENSITIVE_PERMISSIONS
