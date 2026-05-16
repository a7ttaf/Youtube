from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_service import AuditRecord
from ums_smart_revenue.db.security_models import AuditLogORM, UserORM


class SqlAlchemyAuditSink:
    """Persist audit records through the request-scoped SQLAlchemy session."""

    def __init__(self, session: Session):
        """Bind audit writes to the same transaction as the guarded mutation."""
        self._session = session

    def append(self, record: AuditRecord) -> None:
        """Append one audit log row and flush so failures happen before commit."""
        raw_actor_user_id = record.user_id
        user_id = _parse_uuid_or_none(raw_actor_user_id)
        details = dict(record.details or {})
        if user_id is None or self._session.get(UserORM, user_id) is None:
            details["actor_user_id"] = raw_actor_user_id
            user_id = None
        self._session.add(
            AuditLogORM(
                id=uuid4(),
                user_id=user_id,
                event_type=record.event_type,
                entity_type=record.entity_type,
                entity_id=record.entity_id,
                scope_type=record.scope_type,
                scope_id=record.scope_id,
                request_id=record.request_id,
                reason=record.reason,
                details=details,
                sensitive=record.sensitive,
                created_at=record.created_at,
            )
        )
        self._session.flush()

    def rollback(self) -> None:
        """Rollback and detach pending objects after fail-closed audit errors."""
        self._session.rollback()
        self._session.expunge_all()


def _parse_uuid_or_none(value: str) -> UUID | None:
    """Parse audit actor ids when they can be represented as local user FKs."""
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        # ValueError covers malformed UUID strings; TypeError/AttributeError
        # cover non-string inputs (None, int, etc.) that violate the str
        # contract. All fall back to the fail-closed gateway-actor path.
        return None
