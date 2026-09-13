# ============================================================================
# Purpose: Add the index backing connector-job lifecycle lookups on
#   audit_logs. Dispatch claiming, activation-failure dedupe, and startup
#   recovery all filter on (tenant_id, event_type, request_id); without the
#   index every dispatch lock acquisition and every recovery anti-join scans
#   the tenant's full audit history, so recovery slows as audits grow.
# Database/ORM: audit_logs — one new non-unique btree index; no column,
#   constraint, or data changes. Backward-compatible; downgrade drops it.
# Standards: Plain op.create_index/op.drop_index; PostgreSQL takes a SHARE
#   lock for the build (short for an additive index; this table is
#   append-only and the deployment is single-writer at this phase).
# Blast Radius: Query-plan only. No authorization, finance, audit content,
#   or export behavior changes; RLS policies are untouched.
# Connections:
#   - File: backend/ums_smart_revenue/db/security_models.py -> AuditLogORM
#     __table_args__ declares ix_audit_logs_tenant_event_request.
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py ->
#     _lock_job_lifecycle_actions + recovery anti-join are the consumers.
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260911_0001_beta_operator_rollback_gate.py -> parent revision.
# ============================================================================
"""Index audit_logs on the connector-job lifecycle predicate.

Revision ID: 20260913_0001
Revises: 20260911_0001
Create Date: 2026-09-13

Why this revision exists
------------------------
``ConnectorJobExecutor._lock_job_lifecycle_actions`` and the startup
recovery anti-join both filter ``audit_logs`` on
``(tenant_id, event_type, request_id)``. The table previously carried only
user/event/entity/tenant indexes, so each dispatch's FOR UPDATE probe and
each per-intent terminal check degraded to a scan of the tenant's audit
history — recovery latency grows linearly with audit volume. The index keeps
both probes proportional to the matched lifecycle rows.
"""

from alembic import op

revision = "20260913_0001"
down_revision = "20260911_0001"
branch_labels = None
depends_on = None

_INDEX_NAME = "ix_audit_logs_tenant_event_request"
_INDEX_COLUMNS = ["tenant_id", "event_type", "request_id"]


def upgrade() -> None:
    """Create the lifecycle lookup index on audit_logs."""
    op.create_index(_INDEX_NAME, "audit_logs", _INDEX_COLUMNS)


def downgrade() -> None:
    """Drop the lifecycle lookup index; audit rows are not touched."""
    op.drop_index(_INDEX_NAME, table_name="audit_logs")
