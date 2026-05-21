from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_service import AuditRecord
from ums_smart_revenue.db.security_models import AuditLogORM, UserORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


class SqlAlchemyAuditSink:
    """Persist audit records through the request-scoped SQLAlchemy session."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind audit writes to an explicit or current request tenant."""
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def append(self, record: AuditRecord) -> None:
        """Append one audit log row and flush so failures happen before commit."""
        raw_actor_user_id = record.user_id
        user_id = _parse_uuid_or_none(raw_actor_user_id)
        details = dict(record.details or {})
        actor_exists = (
            user_id is not None
            and self._session.scalar(
                select(UserORM.id).where(
                    UserORM.id == user_id,
                    UserORM.tenant_id == self._tenant_id,
                )
            )
            is not None
        )
        if not actor_exists:
            details["actor_user_id"] = raw_actor_user_id
            user_id = None
        self._session.add(
            AuditLogORM(
                id=uuid4(),
                tenant_id=self._tenant_id,
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
        raise ValueError("tenant_id must be a valid UUID") from exc
