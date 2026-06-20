# ============================================================================
# Purpose: Provide connector-run audit read models used by finance smart alerts
#   without putting SQLAlchemy audit-log aggregation inside API route modules.
# Database/ORM: Read-only SELECTs against AuditLogORM / audit_logs; PostgreSQL
#   audit_logs JSONB remains the source of truth for connector observability.
# Standards: Callers provide the already-resolved tenant id and permission
#   checks; this module performs no authorization decisions and returns only
#   aggregate counts, never sensitive connector error payloads.
# Blast Radius: Finance dashboard/export read surfaces for connector health
#   alerts. No finance writes, connector execution, authz, or audit writes.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py -> dashboard smart alerts.
#   - File: backend/ums_smart_revenue/api/exports.py -> export smart alerts.
#   - File: backend/ums_smart_revenue/connectors/google/audit.py -> emits
#     FINISHED connector audit edges consumed here.
# ============================================================================
from __future__ import annotations

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.scopes import ScopeType
from ums_smart_revenue.db.security_models import AuditLogORM


# ============================================================================
# Purpose: Read connector-run FINISHED audit edges for a finance month and
#   derive the current failed-run smart-alert signal from the latest terminal
#   edge per connector/account. Same-timestamp ties are treated conservatively:
#   any tied success or malformed status clears the failed signal to avoid
#   surfacing stale or ambiguous connector failures.
# Database/ORM: Bounded read-only SELECT on AuditLogORM / audit_logs; filters
#   tenant, event/entity/scope, lifecycle, and report_month in SQL before
#   applying per-connector latest-edge aggregation in Python.
# Standards: Tenant id is explicit; malformed detail payloads are ignored
#   unless they identify the same connector/account latest edge, in which case
#   they clear older failures; deterministic status counts; no sensitive error
#   summaries returned.
# Blast Radius: Finance smart-alert read model only. No finance calculation,
#   authorization, export persistence, ingestion, or audit-write changes.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/audit.py -> emits
#     lifecycle=FINISHED with connector_key/account_id/report_month/status.
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> consumes the
#     aggregate as CONNECTOR_RUNS_FAILED.
# ============================================================================
def failed_connector_run_count_and_statuses(
    session: Session,
    *,
    tenant_id: UUID,
    month: str,
) -> tuple[int, dict[str, int]]:
    """Return latest failed/partial connector-run counts for one finance month."""
    details_column = cast(Any, AuditLogORM.details)
    details_rows = session.execute(
        select(AuditLogORM.details, AuditLogORM.scope_id, AuditLogORM.created_at)
        .where(
            AuditLogORM.tenant_id == tenant_id,
            AuditLogORM.event_type == AuditEventType.CONNECTOR_JOB_RUN.value,
            AuditLogORM.entity_type == "connector_run",
            AuditLogORM.scope_type == ScopeType.CONNECTOR.value,
            details_column["lifecycle"].as_string() == "FINISHED",
            details_column["report_month"].as_string() == month,
        )
        .order_by(AuditLogORM.created_at.desc())
    ).all()
    latest_seen_at_by_connector_account: dict[tuple[str, str], datetime] = {}
    latest_statuses_by_connector_account: dict[tuple[str, str], set[str]] = {}
    for details, scope_id, created_at in details_rows:
        if not isinstance(details, dict):
            continue
        connector_key = _non_blank_text(details.get("connector_key")) or _non_blank_text(scope_id)
        account_id = _non_blank_text(details.get("account_id"))
        if connector_key is None or account_id is None:
            continue
        lookup_key = (connector_key, account_id)
        terminal_status = _connector_terminal_status(details.get("status")) or ""
        latest_seen_at = latest_seen_at_by_connector_account.get(lookup_key)
        if latest_seen_at is None:
            latest_seen_at_by_connector_account[lookup_key] = created_at
            latest_statuses_by_connector_account[lookup_key] = {terminal_status}
            continue
        if created_at == latest_seen_at:
            latest_statuses_by_connector_account[lookup_key].add(terminal_status)

    status_counts: dict[str, int] = {}
    for terminal_statuses in latest_statuses_by_connector_account.values():
        if "" in terminal_statuses or "SUCCEEDED" in terminal_statuses:
            continue
        if "FAILED" in terminal_statuses:
            status_counts["FAILED"] = status_counts.get("FAILED", 0) + 1
        elif "PARTIAL" in terminal_statuses:
            status_counts["PARTIAL"] = status_counts.get("PARTIAL", 0) + 1
    return sum(status_counts.values()), dict(sorted(status_counts.items()))


def _non_blank_text(value: object) -> str | None:
    """Return stripped non-empty text for audit detail values."""
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _connector_terminal_status(value: object) -> str | None:
    """Normalize terminal connector-run status values from audit JSON."""
    if not isinstance(value, str):
        return None
    status_value = value.strip().upper()
    if status_value in {"SUCCEEDED", "PARTIAL", "FAILED"}:
        return status_value
    return None
