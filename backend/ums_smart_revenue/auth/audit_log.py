from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.security_models import AuditLogORM


MAX_AUDIT_LOG_PAGE_SIZE = 100


@dataclass(frozen=True)
class AuditLogEntry:
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
    items: list[AuditLogEntry]
    limit: int
    offset: int
    has_more: bool


class AuditLogError(ValueError):
    pass


class AuditLogValidationError(AuditLogError):
    pass


class SqlAlchemyAuditLogRepository:
    def __init__(self, session: Session):
        self._session = session

    def list_events(
        self,
        *,
        event_type: str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> AuditLogPage:
        if limit < 1 or limit > MAX_AUDIT_LOG_PAGE_SIZE:
            raise AuditLogValidationError(f"limit must be between 1 and {MAX_AUDIT_LOG_PAGE_SIZE}")
        if offset < 0:
            raise AuditLogValidationError("offset must be greater than or equal to 0")

        statement = select(AuditLogORM).order_by(AuditLogORM.created_at.desc(), AuditLogORM.id.desc())
        if event_type is not None:
            statement = statement.where(AuditLogORM.event_type == _normalize_required_string(event_type, "event_type"))
        if entity_type is not None:
            statement = statement.where(AuditLogORM.entity_type == _normalize_required_string(entity_type, "entity_type"))
        if entity_id is not None:
            statement = statement.where(AuditLogORM.entity_id == _normalize_required_string(entity_id, "entity_id"))

        rows = self._session.scalars(statement.limit(limit + 1).offset(offset)).all()
        return AuditLogPage(
            items=[self._to_entry(row) for row in rows[:limit]],
            limit=limit,
            offset=offset,
            has_more=len(rows) > limit,
        )

    @staticmethod
    def _to_entry(row: AuditLogORM) -> AuditLogEntry:
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
    normalized = value.strip()
    if not normalized:
        raise AuditLogValidationError(f"{field_name} must not be blank")
    return normalized
