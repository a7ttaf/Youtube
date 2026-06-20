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
#   - File: backend/ums_smart_revenue/connectors/runs/normalization.py -> emits
#     PROJECTION_FAILED connector edges consumed here.
# ============================================================================
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.scopes import ScopeType
from ums_smart_revenue.db.security_models import AuditLogORM

_TERMINAL_LIFECYCLES = frozenset({"FINISHED", "PROJECTION_FAILED"})
_ADSENSE_ACCOUNT_RESOURCE_PREFIX = "accounts/"
_ADSENSE_ACCOUNT_ID_RESERVED_CHARS = frozenset("/?#%")
_CANONICAL_CONNECTOR_KEYS = {
    "youtube-reporting": "youtube_reporting",
    "youtube_reporting": "youtube_reporting",
    "youtube-analytics": "youtube_analytics",
    "youtube_analytics": "youtube_analytics",
    "adsense-management": "adsense_management",
    "adsense_management": "adsense_management",
}
ConnectorTerminalEdge = tuple[tuple[str, str], datetime, str]


# ============================================================================
# Purpose: Read connector-run terminal audit edges for a finance month and
#   derive the current failed-run smart-alert signal from the latest edge per
#   connector/account. Same-timestamp ties are treated conservatively: any tied
#   success or malformed status clears the failed signal to avoid surfacing
#   stale or ambiguous connector failures.
# Database/ORM: Bounded read-only SELECT on AuditLogORM / audit_logs; filters
#   tenant, event/scope, lifecycle, report_month, and allowed entity/action
#   shapes in SQL before applying per-connector latest-edge aggregation in
#   Python.
# Standards: Tenant id is explicit; malformed detail payloads are ignored
#   unless they identify the same connector/account latest edge, in which case
#   they clear older failures; deterministic status counts; no sensitive error
#   summaries returned.
# Blast Radius: Finance smart-alert read model only. No finance calculation,
#   authorization, export persistence, ingestion, or audit-write changes.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/audit.py -> emits
#     lifecycle=FINISHED with connector_key/account_id/report_month/status.
#   - File: backend/ums_smart_revenue/connectors/runs/normalization.py -> emits
#     lifecycle=PROJECTION_FAILED after a post-run projection failure.
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
        select(
            AuditLogORM.details,
            AuditLogORM.scope_id,
            AuditLogORM.entity_id,
            AuditLogORM.created_at,
        )
        .where(
            AuditLogORM.tenant_id == tenant_id,
            AuditLogORM.event_type == AuditEventType.CONNECTOR_JOB_RUN.value,
            AuditLogORM.scope_type == ScopeType.CONNECTOR.value,
            details_column["lifecycle"].as_string().in_(tuple(sorted(_TERMINAL_LIFECYCLES))),
            details_column["report_month"].as_string() == month,
            or_(
                AuditLogORM.entity_type == "connector_run",
                (
                    (AuditLogORM.entity_type == "api_connector")
                    & (details_column["action"].as_string() == "run_superseded")
                ),
            ),
        )
        .order_by(AuditLogORM.created_at.desc())
    ).all()
    latest_seen_at_by_connector_account: dict[tuple[str, str], datetime] = {}
    latest_statuses_by_connector_account: dict[tuple[str, str], set[str]] = {}
    for details, scope_id, entity_id, created_at in details_rows:
        terminal_edge = _connector_terminal_edge(
            details=details,
            scope_id=scope_id,
            entity_id=entity_id,
            created_at=created_at,
        )
        if terminal_edge is None:
            continue
        _record_latest_terminal_status(
            terminal_edge=terminal_edge,
            latest_seen_at_by_connector_account=latest_seen_at_by_connector_account,
            latest_statuses_by_connector_account=latest_statuses_by_connector_account,
        )

    status_counts = _failed_connector_status_counts(latest_statuses_by_connector_account.values())
    return sum(status_counts.values()), dict(sorted(status_counts.items()))


def _connector_terminal_edge(
    *,
    details: object,
    scope_id: object,
    entity_id: object,
    created_at: datetime,
) -> ConnectorTerminalEdge | None:
    """Normalize one audit-log row into a latest-run grouping key and status."""
    if not isinstance(details, dict):
        return None
    lifecycle = _connector_terminal_lifecycle(details.get("lifecycle"))
    if lifecycle is None:
        return None
    raw_connector_key = _non_blank_text(details.get("connector_key")) or _non_blank_text(scope_id)
    connector_key = _canonical_connector_key(raw_connector_key)
    raw_account_id = _non_blank_text(details.get("account_id")) or _connector_entity_account_id(
        entity_id=entity_id, connector_key=raw_connector_key
    )
    account_id = _canonical_connector_account_id(
        connector_key=connector_key,
        value=raw_account_id,
    )
    if connector_key is None or account_id is None:
        return None
    terminal_status = _connector_terminal_status(
        lifecycle=lifecycle,
        value=details.get("status"),
    )
    return (connector_key, account_id), created_at, terminal_status


def _record_latest_terminal_status(
    *,
    terminal_edge: ConnectorTerminalEdge,
    latest_seen_at_by_connector_account: dict[tuple[str, str], datetime],
    latest_statuses_by_connector_account: dict[tuple[str, str], set[str]],
) -> None:
    """Keep the latest terminal status set for one connector/account pair."""
    lookup_key, created_at, terminal_status = terminal_edge
    latest_seen_at = latest_seen_at_by_connector_account.get(lookup_key)
    if latest_seen_at is None:
        latest_seen_at_by_connector_account[lookup_key] = created_at
        latest_statuses_by_connector_account[lookup_key] = {terminal_status}
        return
    if created_at == latest_seen_at:
        latest_statuses_by_connector_account[lookup_key].add(terminal_status)


def _failed_connector_status_counts(
    latest_terminal_statuses: Iterable[set[str]],
) -> dict[str, int]:
    """Count latest failed/partial terminal status sets, ignoring cleared runs."""
    status_counts: dict[str, int] = {}
    for terminal_statuses in latest_terminal_statuses:
        status = _failed_connector_status(terminal_statuses)
        if status is not None:
            status_counts[status] = status_counts.get(status, 0) + 1
    return status_counts


def _failed_connector_status(terminal_statuses: set[str]) -> str | None:
    """Return the alertable status for one latest terminal status set."""
    if "" in terminal_statuses or "SUCCEEDED" in terminal_statuses:
        return None
    if "FAILED" in terminal_statuses:
        return "FAILED"
    if "PARTIAL" in terminal_statuses:
        return "PARTIAL"
    return None


def _non_blank_text(value: object) -> str | None:
    """Return stripped non-empty text for audit detail values."""
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return None


def _canonical_connector_key(value: str | None) -> str | None:
    """Collapse known public hyphen aliases to their stored source-system key."""
    if value is None:
        return None
    return _CANONICAL_CONNECTOR_KEYS.get(value, value)


def _connector_entity_account_id(*, entity_id: object, connector_key: str | None) -> str | None:
    """Extract the account suffix from api_connector audit entity ids."""
    entity_id_text = _non_blank_text(entity_id)
    if entity_id_text is None or connector_key is None:
        return None
    prefix = f"{connector_key}:"
    if not entity_id_text.startswith(prefix):
        return None
    return _non_blank_text(entity_id_text.removeprefix(prefix))


def _canonical_connector_account_id(
    *,
    connector_key: str | None,
    value: str | None,
) -> str | None:
    """Normalize connector account ids that have public resource-name aliases."""
    if value is None:
        return None
    if connector_key != "adsense_management" or not value.startswith(
        _ADSENSE_ACCOUNT_RESOURCE_PREFIX
    ):
        return value
    candidate = value.removeprefix(_ADSENSE_ACCOUNT_RESOURCE_PREFIX)
    if candidate and not any(char in candidate for char in _ADSENSE_ACCOUNT_ID_RESERVED_CHARS):
        return candidate
    return value


def _connector_terminal_lifecycle(value: object) -> str | None:
    """Normalize connector audit lifecycle values relevant to failed-run alerts."""
    if not isinstance(value, str):
        return None
    lifecycle_value = value.strip().upper()
    if lifecycle_value in _TERMINAL_LIFECYCLES:
        return lifecycle_value
    return None


def _connector_terminal_status(*, lifecycle: str, value: object) -> str:
    """Normalize terminal connector-run status values from audit JSON.

    Args:
        lifecycle: The audit edge lifecycle (FINISHED or PROJECTION_FAILED).
            PROJECTION_FAILED maps directly to FAILED without inspecting the
            status value.
        value: The raw status value from the audit details JSON.

    Returns:
        A canonical status string (SUCCEEDED, PARTIAL, FAILED) or "" when the
        lifecycle or value is unrecognized.
    """
    if lifecycle == "PROJECTION_FAILED":
        return "FAILED"
    if not isinstance(value, str):
        return ""
    status_value = value.strip().upper()
    if status_value in {"SUCCEEDED", "PARTIAL", "FAILED"}:
        return status_value
    return ""
