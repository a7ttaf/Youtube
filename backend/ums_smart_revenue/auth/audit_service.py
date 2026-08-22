import threading
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
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
    # The sink's transactional backing, REQUIRED by the protocol: the session
    # object for SQL sinks, None for self-contained in-memory ones. A wrapper
    # must delegate its inner sink's declaration — apply_channel_import
    # refuses both a missing declaration and adapters whose declared sessions
    # differ, and an optional attribute would let a conforming wrapper hide
    # its inner session from that check (PR #196 rounds 6+8, codex).
    sql_unit_of_work: object | None

    def append(self, record: AuditRecord) -> None: ...

    # ========================================================================
    # Purpose: The sink's ATOMICITY contract for a BATCH of appends — a caller
    #   wraps a multi-record delivery so a raise mid-batch leaves the sink
    #   without the accepted prefix, never with audit rows describing an
    #   operation that then failed.
    # Database/ORM: The SQL sinks open a SAVEPOINT on their own session
    #   (Session.begin_nested — PR #196 round 3, codex; a pure delegation to
    #   the enclosing transaction held only while every object shared one
    #   session, which nothing enforced then), never a commit of the outer
    #   transaction. The in-memory sink records its length on enter and
    #   truncates back on raise, under its lock.
    # Standards: Mirrors the store protocols' transaction() (review #184, C2):
    #   never a commit, exceptions always propagate, undo of state not
    #   outcomes. Unlike the stores it NESTS harmlessly — length marks and
    #   savepoint stacks compose — so no nesting guard is declared.
    # Blast Radius: Whether a failed bulk import can leave a PARTIAL audit
    #   trail in a sink without a database transaction (direct/test/bootstrap
    #   callers), or a prefix a catching direct caller could commit on SQL.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     wraps the buffered flush in this boundary.
    #   - File: backend/ums_smart_revenue/auth/sql_audit_sink.py -> the
    #     SAVEPOINT implementations.
    # ========================================================================
    def transaction(self) -> AbstractContextManager[None]:
        """Return a context manager making the wrapped appends all-or-nothing.

        On a clean exit the appended records stand (durability still governed
        by whoever owns the session/request lifecycle). On an exception every
        record appended inside the boundary is removed from this sink, and
        the exception propagates unchanged.
        """
        ...


class InMemoryAuditSink:
    """List-backed sink for tests, bootstrap, and the no-database tier."""

    # No SQL backing: the truncate boundary is this sink's own unit of work.
    sql_unit_of_work: object | None = None

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []
        # Appends and batch boundaries serialize on one re-entrant lock: the
        # no-database tier can share this sink across threads, and without
        # the lock a failed batch's truncation would delete an unrelated
        # request's records appended past the mark (PR #196 round 3, codex).
        # Re-entrant so the boundary-holding thread's own appends pass.
        self._lock = threading.RLock()

    def append(self, record: AuditRecord) -> None:
        with self._lock:
            self.records.append(record)

    # ========================================================================
    # Purpose: The AuditSink.transaction() boundary in memory — mark the
    #   record count on enter, truncate back to the mark on raise, so a
    #   failed batch leaves no accepted prefix behind.
    # Database/ORM: None (list-backed sink).
    # Standards: HOLDS the sink's re-entrant lock for the boundary's whole
    #   duration: the no-database tier shares this sink across threads, and
    #   a foreign append landing past the mark would be deleted by the
    #   positional truncation — serialized behind the batch instead, it
    #   lands only after the boundary resolves, which is what makes the
    #   truncation remove exactly the batch's own records (PR #196 round 3,
    #   codex). Exceptions propagate unchanged.
    # Blast Radius: Whether a failed multi-record batch leaves partial audit
    #   records on the no-database tier. No SQL, no RLS.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py -> the
    #     import's buffered flush runs inside this boundary.
    #   - File: backend/ums_smart_revenue/auth/sql_audit_sink.py -> the
    #     SAVEPOINT implementations of the same protocol method.
    # ========================================================================
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Mark the length on enter; truncate back to the mark on raise."""
        with self._lock:
            marked = len(self.records)
            try:
                yield
            except BaseException:
                del self.records[marked:]
                raise


def _normalize_audit_reason(
    *,
    event_type: AuditEventType,
    reason: str | None,
    reason_required: bool,
) -> str | None:
    """Trim optional audit reasons and enforce event-level reason requirements."""
    normalized_reason = reason.strip() if reason is not None else None
    if normalized_reason == "":
        normalized_reason = None
    if reason_required and not normalized_reason:
        raise ValueError(f"Audit event {event_type.value} requires a reason")
    return normalized_reason


def _resolve_audit_permission(
    *,
    event_type: AuditEventType,
    definition_permission: Permission | None,
    permission_override: Permission | None,
) -> Permission | None:
    """Resolve the audit permission while preventing sensitive downgrade."""
    permission = permission_override if permission_override is not None else definition_permission
    if (
        permission_override is not None
        and definition_permission in SENSITIVE_PERMISSIONS
        and permission_override not in SENSITIVE_PERMISSIONS
    ):
        raise ValueError(
            f"permission_override cannot downgrade sensitive audit event {event_type.value}"
        )
    return permission


def _is_sensitive_audit_record(
    *,
    permission: Permission | None,
    definition_permission: Permission | None,
) -> bool:
    """Return whether either effective or definition permission is sensitive."""
    return bool(
        permission in SENSITIVE_PERMISSIONS or definition_permission in SENSITIVE_PERMISSIONS
    )


# ============================================================================
# Purpose: Create and persist one normalized audit record for a domain event.
# Database/ORM: None; persistence is delegated to the supplied AuditSink.
# Standards: Typed boundary, fail-closed sensitive permission handling, sink-owned write.
# Blast Radius: Authorization, finance-sensitive audit trails, exports.
# Connections:
#   - File: backend/ums_smart_revenue/auth/audit.py -> Event definitions and policy.
#   - File: backend/ums_smart_revenue/auth/sql_audit_sink.py -> Database-backed sink.
# ============================================================================
def record_audit_event(
    *,
    sink: AuditSink,
    actor: UserPrincipal,
    event_type: AuditEventType,
    entity_type: str | None = None,
    entity_id: str | None = None,
    scope: AccessScope | None = None,
    details: Mapping[str, object] | None = None,
    reason: str | None = None,
    request_id: str | None = None,
    permission_override: Permission | None = None,
) -> AuditRecord:
    """Create and persist one normalized audit record for a domain event.

    Persistence is delegated to the supplied audit sink. Details are copied
    into an owned dictionary so callers may pass read-only or typed mappings
    without exposing later source mutations to the persisted audit record.
    """
    definition = AUDIT_EVENT_DEFINITIONS.get(event_type)
    normalized_reason = _normalize_audit_reason(
        event_type=event_type,
        reason=reason,
        reason_required=bool(definition and definition.reason_required),
    )

    definition_permission = definition.permission if definition else None
    permission = _resolve_audit_permission(
        event_type=event_type,
        definition_permission=definition_permission,
        permission_override=permission_override,
    )
    normalized_details: dict[str, object] = deepcopy(dict(details)) if details is not None else {}
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
        sensitive=_is_sensitive_audit_record(
            permission=permission,
            definition_permission=definition_permission,
        ),
        permission=permission.value if permission else None,
    )
    sink.append(record)
    return record
