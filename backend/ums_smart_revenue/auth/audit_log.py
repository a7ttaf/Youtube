"""Tenant-scoped audit log read models and SQL repository."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.db.security_models import AuditLogORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

MAX_AUDIT_LOG_PAGE_SIZE = 100
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


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
        self._tenant_id = _resolve_tenant_id(tenant_id)

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
            raise AuditLogValidationError(
                f"limit must be between 1 and {MAX_AUDIT_LOG_PAGE_SIZE}"
            )
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
                AuditLogORM.event_type
                == _normalize_required_string(event_type, "event_type")
            )
        if exclude_event_type is not None:
            statement = statement.where(
                AuditLogORM.event_type
                != _normalize_required_string(exclude_event_type, "exclude_event_type")
            )
        if entity_type is not None:
            statement = statement.where(
                AuditLogORM.entity_type
                == _normalize_required_string(entity_type, "entity_type")
            )
        if entity_id is not None:
            statement = statement.where(
                AuditLogORM.entity_id
                == _normalize_required_string(entity_id, "entity_id")
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
                    func.sum(
                        case((AuditLogORM.sensitive.is_(True), 1), else_=0)
                    ),
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


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve an explicit, request-scoped, or bootstrap audit tenant id."""
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Normalize tenant constructor input into a UUID object."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise AuditLogValidationError("tenant_id must be a valid UUID") from exc


def _next_cursor(items: list[AuditLogEntry]) -> dict[str, str] | None:
    """Build the cursor that continues after the final item in a page."""
    if not items:
        return None
    last_item = items[-1]
    return {
        "created_at": last_item.created_at.isoformat(),
        "id": last_item.id,
    }
