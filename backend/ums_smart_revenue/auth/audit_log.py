"""Tenant-scoped audit log read models and SQL repository."""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.scopes import ScopeType
from ums_smart_revenue.connectors.keys import canonical_connector_source_system
from ums_smart_revenue.db.security_models import AuditLogORM
from ums_smart_revenue.tenancy.ids import resolve_repository_tenant_id

MAX_AUDIT_LOG_PAGE_SIZE = 100
_CONNECTOR_TERMINAL_LIFECYCLES = frozenset({"FINISHED", "PROJECTION_FAILED"})
_ADSENSE_ACCOUNT_RESOURCE_PREFIX = "accounts/"
_ADSENSE_ACCOUNT_ID_RESERVED_CHARS = frozenset("/?#%")
ConnectorTerminalEdge = tuple[tuple[str, str], datetime, str]


@dataclass(frozen=True)
class AuditLogEntry:
    """Immutable audit log row returned by the read repository."""

    id: str
    user_id: str | None
    event_type: str
    entity_type: str | None
    entity_id: str | None
    scope_type: str | None
    scope_id: str | None
    request_id: str | None
    reason: str | None
    details: dict[str, object]
    sensitive: bool
    created_at: datetime

    def to_api(self, *, include_sensitive_details: bool) -> dict[str, object]:
        """Serialize the entry while enforcing sensitive-detail redaction."""
        details_redacted = self.sensitive and not include_sensitive_details
        return {
            "id": self.id,
            "user_id": self.user_id,
            "event_type": self.event_type,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "request_id": self.request_id,
            "reason": self.reason,
            "details": {} if details_redacted else self.details,
            "details_redacted": details_redacted,
            "sensitive": self.sensitive,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class AuditLogPage:
    """Cursor-paginated audit log response from the repository."""

    items: list[AuditLogEntry]
    limit: int
    has_more: bool
    next_cursor: dict[str, str] | None


@dataclass(frozen=True)
class AuditSummaryCounts:
    """Immutable tenant-scoped audit aggregate returned by ``count_summary``."""

    total_events: int
    sensitive_events: int
    recent_count: int


@dataclass(frozen=True)
class ConnectorRunFailureSummary:
    """Tenant-scoped failed connector-run aggregate for monthly smart alerts."""

    count: int
    by_status: dict[str, int]


class AuditLogError(ValueError):
    """Base error for audit log read validation failures."""

    pass


class AuditLogValidationError(AuditLogError):
    """Raised when audit log query parameters fail validation."""

    pass


class SqlAlchemyAuditLogRepository:
    """Read audit events from the SQL store inside one tenant boundary."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind this repository instance to an explicit or current request tenant."""
        self._session = session
        try:
            self._tenant_id = resolve_repository_tenant_id(tenant_id)
        except ValueError as exc:
            raise AuditLogValidationError(str(exc)) from exc

    def list_events(
        self,
        *,
        event_type: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        exclude_event_type: str | None = None,
        cursor_created_at: datetime | None = None,
        cursor_id: str | None = None,
        limit: int = 50,
    ) -> AuditLogPage:
        """Return a bounded page of audit events visible to this tenant."""
        if limit < 1 or limit > MAX_AUDIT_LOG_PAGE_SIZE:
            raise AuditLogValidationError(f"limit must be between 1 and {MAX_AUDIT_LOG_PAGE_SIZE}")
        if (cursor_created_at is None) != (cursor_id is None):
            raise AuditLogValidationError(
                "cursor_created_at and cursor_id must be provided together"
            )

        statement = (
            select(AuditLogORM)
            .where(AuditLogORM.tenant_id == self._tenant_id)
            .order_by(AuditLogORM.created_at.desc(), AuditLogORM.id.desc())
        )
        if event_type is not None:
            statement = statement.where(
                AuditLogORM.event_type == _normalize_required_string(event_type, "event_type")
            )
        if exclude_event_type is not None:
            statement = statement.where(
                AuditLogORM.event_type
                != _normalize_required_string(exclude_event_type, "exclude_event_type")
            )
        if entity_type is not None:
            statement = statement.where(
                AuditLogORM.entity_type == _normalize_required_string(entity_type, "entity_type")
            )
        if entity_id is not None:
            statement = statement.where(
                AuditLogORM.entity_id == _normalize_required_string(entity_id, "entity_id")
            )
        if cursor_created_at is not None and cursor_id is not None:
            cursor_uuid = _parse_uuid(cursor_id, "cursor_id")
            statement = statement.where(
                or_(
                    AuditLogORM.created_at < cursor_created_at,
                    and_(
                        AuditLogORM.created_at == cursor_created_at,
                        AuditLogORM.id < cursor_uuid,
                    ),
                )
            )

        rows = self._session.scalars(statement.limit(limit + 1)).all()
        items = [self._to_entry(row) for row in rows[:limit]]
        return AuditLogPage(
            items=items,
            limit=limit,
            has_more=len(rows) > limit,
            next_cursor=_next_cursor(items) if len(rows) > limit else None,
        )

    def count_summary(self, *, window_hours: int) -> AuditSummaryCounts:
        """Return tenant-scoped audit counts for the configured recent window."""
        # ====================================================================
        # Purpose: Compute tenant-scoped audit aggregate counts (lifetime
        #   total, lifetime sensitive, and a recent count within the supplied
        #   window) for the GET /audit/summary tile endpoint.
        # Database/ORM: AuditLogORM / audit_logs (read-only COUNT aggregates;
        #   reuses ix_audit_logs_tenant_id + ix_audit_logs_event_created).
        # Standards: Typed AuditSummaryCounts result; same tenant scoping and
        #   AUDIT_LOG_VIEWED exclusion as list_events; raises
        #   AuditLogValidationError on invalid window bounds; all three counts
        #   are derived from a SINGLE SELECT so they share one MVCC snapshot
        #   on PostgreSQL's default READ COMMITTED isolation.
        # Blast Radius: Audit read-only aggregate; no write, no schema change,
        #   no migration. Counts disclose no per-row payload.
        # Connections:
        #   - File: backend/ums_smart_revenue/api/audit.py -> count_summary is
        #     called by the GET /audit/summary handler.
        # ====================================================================
        if window_hours < 1:
            raise AuditLogValidationError("window_hours must be at least 1")

        # Single snapshot for all three aggregates: under READ COMMITTED, a
        # concurrent appender can commit between two independent SELECTs and
        # leave one count including the new row while another does not. Folding
        # total / sensitive / recent into one SELECT guarantees the three
        # numbers are always mutually consistent (no "recent row that total
        # did not see" or "sensitive row the total missed").
        cutoff = datetime.now(UTC) - timedelta(hours=window_hours)
        total_events, sensitive_events, recent_count = self._session.execute(
            select(
                func.count().label("total_events"),
                func.coalesce(
                    func.sum(case((AuditLogORM.sensitive.is_(True), 1), else_=0)),
                    0,
                ).label("sensitive_events"),
                func.coalesce(
                    func.sum(case((AuditLogORM.created_at >= cutoff, 1), else_=0)),
                    0,
                ).label("recent_count"),
            )
            .select_from(AuditLogORM)
            .where(
                AuditLogORM.tenant_id == self._tenant_id,
                AuditLogORM.event_type != AuditEventType.AUDIT_LOG_VIEWED.value,
            )
        ).one()
        return AuditSummaryCounts(
            total_events=int(total_events or 0),
            sensitive_events=int(sensitive_events or 0),
            recent_count=int(recent_count or 0),
        )

    def connector_run_failure_summary(self, *, month: str) -> ConnectorRunFailureSummary:
        """Return latest failed connector-run counts for one finance month."""
        # ====================================================================
        # Purpose: Read connector-run terminal audit edges for a finance month
        #   and derive the current failed-run smart-alert signal from the
        #   latest edge per connector/account.
        # Database/ORM: Bounded read-only SELECT on AuditLogORM / audit_logs;
        #   filters tenant, connector scope, report_month, lifecycle, and
        #   allowed entity/action shapes before deterministic Python grouping.
        # Standards: Repository owns the audit-log SQL; callers own auth gates.
        #   Returned payload is aggregate-only and excludes sensitive connector
        #   error details.
        # Blast Radius: Finance dashboard/export read model only. No audit
        #   writes, connector execution, authorization, or finance mutation.
        # Connections:
        #   - File: backend/ums_smart_revenue/api/revenue.py -> dashboard
        #     smart-alert signal composition.
        #   - File: backend/ums_smart_revenue/api/exports.py -> export
        #     smart-alert signal parity with the dashboard.
        #   - File: backend/ums_smart_revenue/connectors/google/audit.py ->
        #     emits FINISHED connector audit edges consumed here.
        #   - File: backend/ums_smart_revenue/connectors/runs/normalization.py
        #     -> emits PROJECTION_FAILED connector edges consumed here.
        # ====================================================================
        details_column = cast(Any, AuditLogORM.details)
        details_rows = self._session.execute(
            select(
                AuditLogORM.details,
                AuditLogORM.scope_id,
                AuditLogORM.entity_id,
                AuditLogORM.created_at,
            )
            .where(
                AuditLogORM.tenant_id == self._tenant_id,
                AuditLogORM.event_type == AuditEventType.CONNECTOR_JOB_RUN.value,
                AuditLogORM.scope_type == ScopeType.CONNECTOR.value,
                details_column["lifecycle"]
                .as_string()
                .in_(tuple(sorted(_CONNECTOR_TERMINAL_LIFECYCLES))),
                details_column["report_month"].as_string() == month,
                or_(
                    AuditLogORM.entity_type == "connector_run",
                    (
                        (AuditLogORM.entity_type == "api_connector")
                        & (details_column["action"].as_string() == "run_superseded")
                    ),
                ),
            )
            .order_by(
                AuditLogORM.created_at.desc(),
            ),
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

        status_counts = _failed_connector_status_counts(
            latest_statuses_by_connector_account.values()
        )
        return ConnectorRunFailureSummary(
            count=sum(status_counts.values()),
            by_status=dict(sorted(status_counts.items())),
        )

    def connector_run_details_for_finance_month(self, month: str) -> list[object]:
        """Return CONNECTOR_JOB_RUN audit detail payloads for one finance month, newest first.

        Ordered by created_at DESC then id DESC so the first row is the newest
        connector-run edge. Repository owns the audit-log SQL; interpreting the
        JSON lifecycle payloads (e.g. ROWS_SKIPPED supersession) stays with the
        caller.
        """
        return list(
            self._session.scalars(
                select(AuditLogORM.details)
                .where(
                    AuditLogORM.tenant_id == self._tenant_id,
                    AuditLogORM.event_type == AuditEventType.CONNECTOR_JOB_RUN.value,
                    AuditLogORM.scope_type == ScopeType.FINANCE_MONTH.value,
                    AuditLogORM.scope_id == month,
                )
                .order_by(AuditLogORM.created_at.desc(), AuditLogORM.id.desc())
            ).all()
        )

    @staticmethod
    def _to_entry(row: AuditLogORM) -> AuditLogEntry:
        """Convert a SQLAlchemy row into the API-facing immutable entry."""
        return AuditLogEntry(
            id=str(row.id),
            user_id=str(row.user_id) if row.user_id else None,
            event_type=row.event_type,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            scope_type=row.scope_type,
            scope_id=row.scope_id,
            request_id=row.request_id,
            reason=row.reason,
            details=row.details or {},
            sensitive=row.sensitive,
            created_at=row.created_at,
        )


def _normalize_required_string(value: str, field_name: str) -> str:
    """Trim a required string filter and reject blank values."""
    normalized = value.strip()
    if not normalized:
        raise AuditLogValidationError(f"{field_name} must not be blank")
    return normalized


def _parse_uuid(value: str, field_name: str) -> UUID:
    """Parse a UUID-valued cursor field into its canonical object form."""
    try:
        return UUID(value)
    except ValueError as exc:
        raise AuditLogValidationError(f"{field_name} must be a valid UUID") from exc


def _connector_terminal_edge(
    *,
    details: object,
    scope_id: object,
    entity_id: object,
    created_at: datetime,
) -> ConnectorTerminalEdge | None:
    """Normalize one connector audit-log row into a grouping key and status."""
    if not isinstance(details, dict):
        return None
    lifecycle = _connector_terminal_lifecycle(details.get("lifecycle"))
    if lifecycle is None:
        return None
    raw_connector_key = _non_blank_text(details.get("connector_key")) or _non_blank_text(scope_id)
    connector_key = canonical_connector_source_system(raw_connector_key)
    raw_account_id = _non_blank_text(details.get("account_id")) or _connector_entity_account_id(
        entity_id=entity_id,
        connector_key=raw_connector_key,
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
        _ADSENSE_ACCOUNT_RESOURCE_PREFIX,
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
    if lifecycle_value in _CONNECTOR_TERMINAL_LIFECYCLES:
        return lifecycle_value
    return None


def _connector_terminal_status(*, lifecycle: str, value: object) -> str:
    """Normalize terminal connector-run status values from audit JSON."""
    if lifecycle == "PROJECTION_FAILED":
        return "FAILED"
    if not isinstance(value, str):
        return ""
    status_value = value.strip().upper()
    if status_value in {"SUCCEEDED", "PARTIAL", "FAILED"}:
        return status_value
    return ""


def _next_cursor(items: list[AuditLogEntry]) -> dict[str, str] | None:
    """Build the cursor that continues after the final item in a page."""
    if not items:
        return None
    last_item = items[-1]
    return {
        "created_at": last_item.created_at.isoformat(),
        "id": last_item.id,
    }
