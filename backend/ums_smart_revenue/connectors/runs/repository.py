import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.connector_models import (
    ConnectorRunORM,
    ConnectorRunRawFileORM,
)
from ums_smart_revenue.db.report_models import RawReportFileORM

CONNECTOR_RUN_COUNT_KEYS = (
    "reports_attempted",
    "reports_succeeded",
    "reports_failed",
    "rows_upserted_total",
    "rows_upserted_created",
    "rows_upserted_updated",
    "rows_upserted_unchanged",
)
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "PARTIAL", "FAILED"})
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
ERROR_SUMMARY_MAX_CHARS = 500


@dataclass(frozen=True)
class ConnectorRunEntry:
    id: str
    tenant_id: str
    connector_key: str
    account_id: str
    report_month: str
    triggered_by_user_id: str | None
    started_at: datetime
    finished_at: datetime | None
    status: str
    counts: dict[str, int]
    error_summary: str | None


class ConnectorRunError(ValueError):
    pass


class ConnectorRunValidationError(ConnectorRunError):
    pass


class ConnectorRunLinkConflictError(ConnectorRunError):
    pass


class ConnectorRunNotFoundError(LookupError):
    pass


# ============================================================================
# Purpose: Insert a tenant-scoped connector run in RUNNING state; later B2
#          orchestrator slices update it when report ingestion completes.
# Database/ORM: ConnectorRunORM.
# Standards: Validates month and fixed counts shape; no commit ownership.
# Blast Radius: Audit/operator run tracking only. Finance facts untouched.
# Connections:
#   - File: backend/ums_smart_revenue/db/connector_models.py -> ORM table.
#   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md -> B2.3 contract.
# ============================================================================
def start_run(
    session: Session,
    *,
    tenant_id: UUID,
    connector_key: str,
    account_id: str,
    report_month: str,
    triggered_by_user_id: UUID | None,
) -> ConnectorRunEntry:
    _validate_month(report_month)
    row = ConnectorRunORM(
        id=uuid4(),
        tenant_id=tenant_id,
        connector_key=_required_text(connector_key, "connector_key"),
        account_id=_required_text(account_id, "account_id"),
        report_month=report_month,
        triggered_by_user_id=triggered_by_user_id,
        started_at=datetime.now(UTC),
        status="RUNNING",
        counts_json=_zero_counts(),
    )
    session.add(row)
    session.flush()
    return _to_entry(row)


# ============================================================================
# Purpose: Record which persisted raw report files were consumed by a connector
#          run, preserving deterministic per-run report ordering.
# Database/ORM: ConnectorRunRawFileORM, ConnectorRunORM, RawReportFileORM.
# Standards: Tenant-scoped parent lookups prevent cross-tenant linkage before
#            database constraints are reached.
# Blast Radius: Raw evidence/run tracking only. Finance facts untouched.
# Connections:
#   - File: backend/ums_smart_revenue/db/report_models.py -> raw files parent.
#   - File: backend/ums_smart_revenue/db/connector_models.py -> join table.
# ============================================================================
def link_raw_file(
    session: Session,
    *,
    tenant_id: UUID,
    connector_run_id: UUID,
    raw_report_file_id: UUID,
    ordering_index: int,
) -> None:
    _validate_ordering_index(ordering_index)
    _get_run(session, tenant_id=tenant_id, connector_run_id=connector_run_id)
    _get_raw_file(session, tenant_id=tenant_id, raw_report_file_id=raw_report_file_id)
    try:
        with session.begin_nested():
            row = ConnectorRunRawFileORM(
                id=uuid4(),
                tenant_id=tenant_id,
                connector_run_id=connector_run_id,
                raw_report_file_id=raw_report_file_id,
                ordering_index=ordering_index,
            )
            session.add(row)
            session.flush()
    except sa.exc.IntegrityError as exc:
        raise ConnectorRunLinkConflictError(
            "raw report file is already linked to this connector run"
        ) from exc


# ============================================================================
# Purpose: Transition a RUNNING connector run to a terminal status with final
#          fixed-shape counts and a bounded operator-safe error summary.
# Database/ORM: ConnectorRunORM.
# Standards: Only terminal statuses accepted; no commit ownership.
# Blast Radius: Audit/operator run tracking only. Finance facts untouched.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py -> future caller.
#   - File: Docs/superpowers/specs/2026-05-26-spec-b2-google-live-connector-design.md -> B2.3 contract.
# ============================================================================
def finish_run(
    session: Session,
    *,
    tenant_id: UUID,
    connector_run_id: UUID,
    status: Literal["SUCCEEDED", "PARTIAL", "FAILED"],
    counts: dict[str, int],
    error_summary: str | None,
) -> ConnectorRunEntry:
    normalized_status = _validate_terminal_status(status)
    normalized_counts = _validate_counts(counts)
    row = _get_run(
        session,
        tenant_id=tenant_id,
        connector_run_id=connector_run_id,
        for_update=True,
    )
    if row.status != "RUNNING":
        raise ConnectorRunValidationError("connector run is already terminal")

    row.status = normalized_status
    row.finished_at = datetime.now(UTC)
    row.counts_json = normalized_counts
    row.error_summary = (
        error_summary[:ERROR_SUMMARY_MAX_CHARS] if error_summary is not None else None
    )
    session.flush()
    return _to_entry(row)


# ============================================================================
# Purpose: Fetch a tenant-scoped connector run, optionally locking it for the
#          terminal status transition.
# Database/ORM: ConnectorRunORM.
# Standards: Tenant filter always applied; with_for_update protects
#            finish_run from double terminal writes where the DB supports it.
# Blast Radius: Connector run lifecycle only. Finance facts untouched.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     Calls finish_run from success, partial, and failure paths.
# ============================================================================
def _get_run(
    session: Session,
    *,
    tenant_id: UUID,
    connector_run_id: UUID,
    for_update: bool = False,
) -> ConnectorRunORM:
    stmt = select(ConnectorRunORM).where(
        ConnectorRunORM.tenant_id == tenant_id,
        ConnectorRunORM.id == connector_run_id,
    )
    if for_update:
        stmt = stmt.with_for_update()
    row = session.scalars(stmt).one_or_none()
    if row is None:
        raise ConnectorRunNotFoundError("Connector run not found")
    return row


# ============================================================================
# Purpose: Validate deterministic raw-file ordering before join-row insert.
# Database/ORM: ConnectorRunRawFileORM.
# Standards: Reject bools, non-integers, and negative indexes fail closed.
# Blast Radius: Connector run evidence ordering only. Finance facts untouched.
# Connections:
#   - File: backend/ums_smart_revenue/db/connector_models.py ->
#     ConnectorRunRawFileORM.ordering_index.
# ============================================================================
def _validate_ordering_index(ordering_index: int) -> None:
    if (
        isinstance(ordering_index, bool)
        or not isinstance(ordering_index, int)
        or ordering_index < 0
    ):
        raise ConnectorRunValidationError(
            "ordering_index must be a non-negative integer"
        )


def _get_raw_file(
    session: Session, *, tenant_id: UUID, raw_report_file_id: UUID
) -> RawReportFileORM:
    row = session.scalars(
        select(RawReportFileORM).where(
            RawReportFileORM.tenant_id == tenant_id,
            RawReportFileORM.id == raw_report_file_id,
        )
    ).one_or_none()
    if row is None:
        raise ConnectorRunNotFoundError("Raw report file not found")
    return row


def _validate_month(report_month: str) -> None:
    if not MONTH_PATTERN.fullmatch(report_month):
        raise ConnectorRunValidationError(
            "report_month must use YYYY-MM with a calendar month from 01 to 12"
        )


def _required_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ConnectorRunValidationError(f"{field_name} must not be blank")
    return normalized


def _validate_terminal_status(status: str) -> str:
    if status not in TERMINAL_STATUSES:
        raise ConnectorRunValidationError(
            "connector run status must be terminal: SUCCEEDED, PARTIAL, or FAILED"
        )
    return status


def _validate_counts(counts: dict[str, int]) -> dict[str, int]:
    expected = set(CONNECTOR_RUN_COUNT_KEYS)
    actual = set(counts)
    if actual != expected:
        raise ConnectorRunValidationError(
            "counts_json must contain the fixed B2.3 connector run key set"
        )
    normalized: dict[str, int] = {}
    for key in CONNECTOR_RUN_COUNT_KEYS:
        value = counts[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConnectorRunValidationError(
                "counts_json values must be non-negative integer values"
            )
        normalized[key] = value
    return normalized


def _zero_counts() -> dict[str, int]:
    return dict.fromkeys(CONNECTOR_RUN_COUNT_KEYS, 0)


def _to_entry(row: ConnectorRunORM) -> ConnectorRunEntry:
    return ConnectorRunEntry(
        id=str(row.id),
        tenant_id=str(row.tenant_id),
        connector_key=row.connector_key,
        account_id=row.account_id,
        report_month=row.report_month,
        triggered_by_user_id=(
            str(row.triggered_by_user_id) if row.triggered_by_user_id else None
        ),
        started_at=row.started_at,
        finished_at=row.finished_at,
        status=row.status,
        counts=dict(row.counts_json),
        error_summary=row.error_summary,
    )
