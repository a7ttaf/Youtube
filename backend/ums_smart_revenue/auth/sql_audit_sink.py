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
        user_id = _parse_uuid(raw_actor_user_id)
        details = dict(record.details or {})
        if self._session.get(UserORM, user_id) is None:
            details.setdefault("actor_user_id", raw_actor_user_id)
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


def _parse_uuid(value: str) -> UUID:
    """Parse audit actor ids before writing SQL foreign-key references."""
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValueError(f"Invalid audit user_id: {value!r}") from exc
