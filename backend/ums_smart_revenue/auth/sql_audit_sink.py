from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_service import AuditRecord
from ums_smart_revenue.db.security_models import AuditLogORM


class SqlAlchemyAuditSink:
    def __init__(self, session: Session):
        self._session = session

    def append(self, record: AuditRecord) -> None:
        user_id = _parse_uuid(record.user_id)
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
                details=record.details,
                sensitive=record.sensitive,
                created_at=record.created_at,
            )
        )
        self._session.flush()


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ValueError(f"Invalid audit user_id: {value!r}") from exc
