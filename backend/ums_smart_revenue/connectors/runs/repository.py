"""Repository helpers for tenant-scoped connector run history."""
# pylint: disable=too-many-instance-attributes, too-many-arguments

import re
from collections.abc import Iterable
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
    "rows_deleted_stale",
)
TERMINAL_STATUSES = frozenset({"SUCCEEDED", "PARTIAL", "FAILED"})
MONTH_PATTERN = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
ERROR_SUMMARY_MAX_CHARS = 500
MAX_CONNECTOR_RUN_PAGE_SIZE = 100


@dataclass(frozen=True)
class ConnectorRunEntry:
    """Immutable connector run row projected into the read API payload."""

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

    def to_api(self) -> dict[str, object]:
        """Serialize the immutable run entry into the stable read API shape."""
        return {
            "id": self.id,
            "connector_key": self.connector_key,
            "account_id": self.account_id,
            "report_month": self.report_month,
            "triggered_by_user_id": self.triggered_by_user_id,
            "started_at": self.started_at.isoformat(),
            "finished_at": (
                self.finished_at.isoformat() if self.finished_at is not None else None
            ),
            "status": self.status,
            "counts": dict(self.counts),
            # FIX: Keep the raw DB value internal and redact locators before
            # exposing run-history summaries through the API boundary.
            "error_summary": _sanitize_error_summary(self.error_summary),
        }


@dataclass(frozen=True)
class ConnectorRunPage:
    """One page of tenant-scoped connector run history plus its cursor."""

    items: list[ConnectorRunEntry]
    limit: int
    next_cursor: dict[str, str] | None


class ConnectorRunError(ValueError):
    """Base validation error for connector run history operations."""


class ConnectorRunValidationError(ConnectorRunError):
    """Validation failure for connector run history queries or cursors."""


class ConnectorRunLinkConflictError(ConnectorRunError):
    """Raised when a run cannot be linked to the requested raw report file."""


class ConnectorRunNotFoundError(LookupError):
    """Raised when a requested connector run cannot be found for the tenant."""


# ============================================================================
# Purpose: Insert a tenant-scoped connector run in RUNNING state; later B2
#          orchestrator slices update it when report ingestion completes.
# Database/ORM: ConnectorRunORM.
# Standards: Validates month and fixed counts shape; no commit ownership.
# Blast Radius: Audit/operator run tracking only. Finance facts untouched.
# Connections:
#   - File: backend/ums_smart_revenue/db/connector_models.py -> ORM table.
#   - File: Docs/superpowers/specs/
#     2026-05-26-spec-b2-google-live-connector-design.md -> B2.3 contract.
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
    """Start a RUNNING connector run and return the created entry."""
    validate_report_month(report_month)
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
    """Link a persisted raw report file to a connector run."""
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
#   - File: Docs/superpowers/specs/
#     2026-05-26-spec-b2-google-live-connector-design.md -> B2.3 contract.
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
    """Finish a running connector run with terminal counts and an optional error summary."""
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
# Purpose: Re-open a terminal connector run to record a post-finish failure
#          from the post-run normalize stage. The run is rewritten to FAILED
#          with a "projection failed" error summary so run history reflects
#          the missing MonthlyChannelRevenueFactORM projection; the
#          connector_runs lifecycle audit row is left intact (FINISHED was
#          emitted at finish_run time; the projection failure is recorded
#          on the run row itself, not as a duplicate FINISHED edge).
# Database/ORM: ConnectorRunORM (UPDATE status/error_summary only).
# Standards: Tenant-scoped, run must be terminal, error_summary truncated to
#            the schema's 500-char check. Preserves finished_at, counts_json,
#            and the original FINISHED audit edge.
# Blast Radius: Connector run history (read surface only). Finance tables
#               untouched; this does NOT roll back any already-written
#               source rows. The downstream normalize stage will re-raise the
#               original error so the caller still sees a fail-loud exception.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     _normalize_ingested_source_rows calls this on non-lock normalize
#     failure so run history is not silently misleading.
# ============================================================================
def record_projection_failure(
    session: Session,
    *,
    tenant_id: UUID,
    connector_run_id: UUID,
    error_summary: str,
) -> ConnectorRunEntry:
    """Rewrite a terminal run to FAILED with a projection-failure error summary."""
    if not error_summary or not error_summary.strip():
        raise ConnectorRunValidationError("error_summary must not be blank")
    row = _get_run(
        session,
        tenant_id=tenant_id,
        connector_run_id=connector_run_id,
        for_update=True,
    )
    if row.status == "RUNNING":
        raise ConnectorRunValidationError(
            "connector run is still RUNNING; use finish_run instead"
        )
    row.status = "FAILED"
    row.error_summary = error_summary.strip()[:ERROR_SUMMARY_MAX_CHARS]
    session.flush()
    return _to_entry(row)


# ============================================================================
# Purpose: Return a tenant-scoped, newest-first page of connector runs for the
#          read-only run-history API, with optional connector/account filters
#          and a both-or-neither (started_at, id) keyset cursor.
# Database/ORM: ConnectorRunORM (read only).
# Standards: Tenant filter always applied; limit validated 1..MAX page size;
#            fetch limit+1 to derive has_more/next_cursor; typed validation
#            errors surface as ConnectorRunValidationError at the boundary.
# Blast Radius: Connector run read surface only. Finance/auth/audit untouched.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py -> GET /connectors/runs.
#   - File: backend/ums_smart_revenue/auth/audit_log.py -> mirrored cursor shape.
# ============================================================================
# ============================================================================
# Purpose: List tenant-scoped connector run history, newest-first, with
#          optional connector_key/connectors/account_id filters and
#          (started_at, id) cursor pagination. Read-only: never mutates run
#          state.
# Database/ORM: ConnectorRunORM (read only).
# Standards: Tenant filter always applied; both-or-neither cursor and limit
#            bounds raise ConnectorRunValidationError (translated to 422 at the
#            route). Fetches limit+1 to compute has_more / next_cursor.
# Blast Radius: Connector operational metadata read only. No finance, auth,
#               audit, or graph projection impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py -> GET /connectors/runs.
# ============================================================================
def list_runs(
    session: Session,
    *,
    tenant_id: UUID,
    connector_key: str | None = None,
    connector_keys: Iterable[str] | None = None,
    account_id: str | None = None,
    cursor_started_at: datetime | None = None,
    cursor_id: str | None = None,
    limit: int,
) -> ConnectorRunPage:
    """List tenant-scoped connector runs newest-first with keyset pagination.

    Raises:
        ConnectorRunValidationError: If limit is out of range or cursor params
            are not provided together (both-or-neither).

    """
    if limit < 1 or limit > MAX_CONNECTOR_RUN_PAGE_SIZE:
        raise ConnectorRunValidationError(
            f"limit must be between 1 and {MAX_CONNECTOR_RUN_PAGE_SIZE}"
        )
    if (cursor_started_at is None) != (cursor_id is None):
        raise ConnectorRunValidationError(
            "cursor_started_at and cursor_id must be provided together"
        )

    stmt = (
        select(ConnectorRunORM)
        .where(ConnectorRunORM.tenant_id == tenant_id)
        .order_by(ConnectorRunORM.started_at.desc(), ConnectorRunORM.id.desc())
    )
    if connector_key is not None:
        stmt = stmt.where(ConnectorRunORM.connector_key == connector_key)
    if connector_keys is not None:
        connector_key_values = tuple(connector_keys)
        if connector_key_values:
            stmt = stmt.where(ConnectorRunORM.connector_key.in_(connector_key_values))
        else:
            return ConnectorRunPage(items=[], limit=limit, next_cursor=None)
    if account_id is not None:
        stmt = stmt.where(ConnectorRunORM.account_id == account_id)
    if cursor_started_at is not None and cursor_id is not None:
        cursor_uuid = _parse_cursor_uuid(cursor_id)
        stmt = stmt.where(
            sa.or_(
                ConnectorRunORM.started_at < cursor_started_at,
                sa.and_(
                    ConnectorRunORM.started_at == cursor_started_at,
                    ConnectorRunORM.id < cursor_uuid,
                ),
            )
        )

    rows = session.scalars(stmt.limit(limit + 1)).all()
    items = [_to_entry(row) for row in rows[:limit]]
    has_more = len(rows) > limit
    return ConnectorRunPage(
        items=items,
        limit=limit,
        next_cursor=_next_cursor(items) if has_more else None,
    )


# ============================================================================
# Purpose: Return the tenant-scoped RUNNING connector runs for one exact scope
#   (tenant, connector_keys, account_id, report_month), newest started_at
#   first. Powers the POST /connectors/jobs secondary duplicate guard +
#   orphan-supersede decision (a stale RUNNING row from a dead process).
# Database/ORM: ConnectorRunORM (read only).
# Standards: tenant filter + status='RUNNING' + the full scope tuple always
#   applied; ordered started_at desc so the youngest RUNNING row is first.
# Blast Radius: Connector run read surface only. No finance, auth, audit, or
#   graph projection impact.
# Connections:
#   - File: backend/ums_smart_revenue/api/connectors.py ->
#     request_connector_job duplicate/orphan-supersede logic.
#   - Function: finish_run -> reused to rewrite a superseded orphan to FAILED.
# ============================================================================
def find_active_runs_for_scope(
    session: Session,
    *,
    tenant_id: UUID,
    connector_keys: tuple[str, ...],
    account_id: str,
    report_month: str,
) -> list[ConnectorRunEntry]:
    """List RUNNING runs for the scope, newest started_at first.

    ``connector_keys`` is a tuple of one-or-more alias candidates (e.g.
    ``("youtube-reporting", "youtube_reporting")``) so a RUNNING row opened
    under a public hyphen key is correctly matched when the request
    arrives with the source-system underscore key, and vice versa. The
    caller is responsible for expanding the submitted key via
    ``_credential_key_candidates`` (same helper the credential lookup +
    authorise-aliases paths use) so this read stays symmetric with the
    rest of the connector-job preflight.
    """
    if not connector_keys:
        raise ConnectorRunValidationError(
            "connector_keys must contain at least one candidate"
        )
    stmt = (
        select(ConnectorRunORM)
        .where(
            ConnectorRunORM.tenant_id == tenant_id,
            ConnectorRunORM.connector_key.in_(connector_keys),
            ConnectorRunORM.account_id == account_id,
            ConnectorRunORM.report_month == report_month,
            ConnectorRunORM.status == "RUNNING",
        )
        .order_by(ConnectorRunORM.started_at.desc(), ConnectorRunORM.id.desc())
    )
    return [_to_entry(row) for row in session.scalars(stmt).all()]


def _parse_cursor_uuid(value: str) -> UUID:
    """Parse the keyset cursor UUID or raise a typed validation error."""
    try:
        return UUID(value)
    except ValueError as exc:
        raise ConnectorRunValidationError("cursor_id must be a valid UUID") from exc


def _next_cursor(items: list[ConnectorRunEntry]) -> dict[str, str] | None:
    """Build the next-page cursor from the last item in a non-empty page."""
    if not items:
        return None
    last_item = items[-1]
    return {
        "started_at": last_item.started_at.isoformat(),
        "id": last_item.id,
    }


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
    """Fetch a tenant-scoped connector run with optional locking for terminal status transition."""
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
    """Validate that ordering_index is a non-negative integer."""
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
    """Retrieve a raw report file scoped to a tenant or raise if not found."""
    row = session.scalars(
        select(RawReportFileORM).where(
            RawReportFileORM.tenant_id == tenant_id,
            RawReportFileORM.id == raw_report_file_id,
        )
    ).one_or_none()
    if row is None:
        raise ConnectorRunNotFoundError("Raw report file not found")
    return row


# ============================================================================
# Purpose: Validate the connector run month before any run-row write or
#          orchestrator dispatch.
# Database/ORM: None.
# Standards: ASCII-only YYYY-MM guard shared by the repository and orchestrator.
# Blast Radius: Connector run identity only; finance, audit, Neo4j, and exports
#               are unaffected until a valid month reaches ingestion.
# Connections:
#   - Function: start_run -> validates before inserting ConnectorRunORM.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py ->
#     validates dry-run and live-run inputs before credential/network work.
# ============================================================================
def validate_report_month(report_month: str) -> None:
    """Validate report_month as a YYYY-MM month."""
    if not MONTH_PATTERN.fullmatch(report_month):
        raise ConnectorRunValidationError(
            "report_month must use YYYY-MM with a calendar month from 01 to 12"
        )


def _validate_month(report_month: str) -> None:
    """Alias validate_report_month for repository callers."""
    validate_report_month(report_month)


def _required_text(value: str, field_name: str) -> str:
    """Strip and validate a required text field."""
    normalized = value.strip()
    if not normalized:
        raise ConnectorRunValidationError(f"{field_name} must not be blank")
    return normalized


def _validate_terminal_status(status: str) -> str:
    """Validate that status is one of the terminal values."""
    if status not in TERMINAL_STATUSES:
        raise ConnectorRunValidationError(
            "connector run status must be terminal: SUCCEEDED, PARTIAL, or FAILED"
        )
    return status


def _validate_counts(counts: dict[str, int]) -> dict[str, int]:
    """Validate the fixed connector-run counts payload."""
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
    """Return zeroed connector-run counts."""
    return dict.fromkeys(CONNECTOR_RUN_COUNT_KEYS, 0)


def _normalize_counts_for_read(counts_json: object) -> dict[str, int]:
    """Normalize a stored ``counts_json`` payload into the fixed B2.3 read shape.

    Historical ``connector_runs`` rows written before a key was added to
    ``CONNECTOR_RUN_COUNT_KEYS`` (for example ``rows_deleted_stale``) still
    carry the old JSON shape on disk. The B2.3 spec promises that every
    read-side payload contains the full key set with defaults of 0, so this
    helper fills in any missing keys with 0 instead of letting the API emit
    mixed-shape payloads. Non-dict payloads and unexpected keys are tolerated
    defensively (extra keys are dropped, non-int values for expected keys
    are coerced to 0) so a corrupted row cannot raise out of the read path
    and mask the operator-facing run history. Negative integers are also
    clamped to 0 since row counts are non-negative by definition; a negative
    value is data corruption and must never surface to the operator-facing
    read API.
    """
    raw = counts_json if isinstance(counts_json, dict) else {}
    normalized: dict[str, int] = {}
    for key in CONNECTOR_RUN_COUNT_KEYS:
        value = raw.get(key)
        if (
            isinstance(value, int)
            and not isinstance(value, bool)
            and value >= 0
        ):
            normalized[key] = value
        else:
            normalized[key] = 0
    return normalized


def _to_entry(row: ConnectorRunORM) -> ConnectorRunEntry:
    """Convert a ConnectorRunORM row into a ConnectorRunEntry."""
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
        counts=_normalize_counts_for_read(row.counts_json),
        error_summary=row.error_summary,
    )


def _sanitize_error_summary(error_summary: str | None) -> str | None:
    """Redact internal locators from operator-facing connector error summaries."""
    if error_summary is None:
        return None

    sanitized = re.sub(
        r"\bstorage_uri(?:=|: )\S+",
        "storage_uri=[redacted]",
        error_summary,
    )
    sanitized = re.sub(
        r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\"<>]+",
        "[redacted-uri]",
        sanitized,
    )
    sanitized = re.sub(
        r"\b[A-Za-z]:\\[^\s'\"<>]+",
        "[redacted-path]",
        sanitized,
    )
    sanitized = re.sub(
        r"(?<!\S)/[^\s'\"<>]+",
        "[redacted-path]",
        sanitized,
    )
    return sanitized[:ERROR_SUMMARY_MAX_CHARS]
