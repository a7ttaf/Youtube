from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from ums_smart_revenue.auth.audit import AUDIT_EVENT_DEFINITIONS, AuditEventType
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import SENSITIVE_PERMISSIONS, Permission
from ums_smart_revenue.auth.scopes import AccessScope


@dataclass(frozen=True)
class AuditRecord:
    user_id: str
    event_type: str
    entity_type: str | None
    entity_id: str | None
    scope_type: str | None
    scope_id: str | None
    request_id: str | None
    reason: str | None
    details: dict[str, object]
    sensitive: bool
    permission: str | None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


class AuditSink(Protocol):
    def append(self, record: AuditRecord) -> None: ...


@dataclass
class InMemoryAuditSink:
    records: list[AuditRecord] = field(default_factory=list)

    def append(self, record: AuditRecord) -> None:
        self.records.append(record)


def record_audit_event(
    *,
    sink: AuditSink,
    actor: UserPrincipal,
    event_type: AuditEventType,
    entity_type: str | None = None,
    entity_id: str | None = None,
    scope: AccessScope | None = None,
    details: dict[str, object] | None = None,
    reason: str | None = None,
    request_id: str | None = None,
    permission_override: Permission | None = None,
) -> AuditRecord:
    definition = AUDIT_EVENT_DEFINITIONS.get(event_type)
    normalized_reason = reason.strip() if reason is not None else None
    if normalized_reason == "":
        normalized_reason = None
    if definition and definition.reason_required and not normalized_reason:
        raise ValueError(f"Audit event {event_type.value} requires a reason")

    definition_permission = definition.permission if definition else None
    permission = permission_override if permission_override is not None else definition_permission
    if (
        permission_override is not None
        and definition_permission in SENSITIVE_PERMISSIONS
        and permission_override not in SENSITIVE_PERMISSIONS
    ):
        raise ValueError(
            f"permission_override cannot downgrade sensitive audit event {event_type.value}"
        )
    normalized_details = deepcopy(details) if details is not None else {}
    record = AuditRecord(
        user_id=actor.user_id,
        event_type=event_type.value,
        entity_type=entity_type,
        entity_id=entity_id,
        scope_type=scope.type.value if scope else None,
        scope_id=scope.id if scope else None,
        request_id=request_id,
        reason=normalized_reason,
        details=normalized_details,
        sensitive=bool(
            permission in SENSITIVE_PERMISSIONS or definition_permission in SENSITIVE_PERMISSIONS
        ),
        permission=permission.value if permission else None,
    )
    sink.append(record)
    return record
