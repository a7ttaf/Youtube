# ============================================================================
# Purpose: SQL persistence for audit records — the platform-session sink used
#   app-wide, plus the platform-lane sink that lets a route commit audit rows
#   atomically with its tenant-session domain writes.
# Database/ORM: AuditLogORM (write; audit_logs is platform-only writable),
#   UserORM (actor-existence read).
# Standards: Flush-on-append so failures surface before commit; tenant scoping
#   resolved from context; the elevated sink pre-flushes tenant work under the
#   tenant role so nothing but the audit write executes as app_platform.
# Blast Radius: Audit trail persistence and atomicity. No RLS policy, grant,
#   or audit semantics change.
# Connections:
#   - File: backend/ums_smart_revenue/auth/audit_service.py -> record shape.
#   - File: backend/ums_smart_revenue/db/lane.py -> platform_lane elevation.
#   - File: backend/ums_smart_revenue/api/dependencies_audit.py -> atomic
#     sink wiring.
# ============================================================================
"""SQL audit sinks: platform-session default and same-transaction platform-lane."""

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_service import AuditRecord
from ums_smart_revenue.db.lane import platform_lane
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
        # PUBLIC unit-of-work identity — apply_channel_import validates that
        # every SQL adapter it composes shares ONE session through this
        # attribute (see SqlAlchemyChannelRegistry; PR #196 round 6, codex).
        self.sql_unit_of_work: object | None = session

    def append(self, record: AuditRecord) -> None:
        """Append one audit log row and flush so failures happen before commit."""
        raw_actor_user_id = record.user_id
        user_id = _parse_uuid_or_none(raw_actor_user_id)
        details = dict(record.details or {})
        # audit_logs has no permission column, so without this the effective
        # permission (including any permission_override, e.g. the import's
        # MANAGE_CHANNELS on CHANNEL_UPDATED) would exist only on the transient
        # in-memory record. Persist it in the durable details so permission-
        # based audit filtering works against the database rows too. This is
        # an unconditional overwrite, not setdefault: the durable key must be
        # the CANONICAL permission record_audit_event derived — a
        # caller-supplied details["permission"] must never shadow it.
        if record.permission is not None:
            details["permission"] = record.permission
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

    # ========================================================================
    # Purpose: The AuditSink.transaction() boundary — a SAVEPOINT on THIS
    #   sink's own session, so a failed batch discards its accepted prefix
    #   without any assumption about who else shares the session.
    # Database/ORM: SAVEPOINT via Session.begin_nested(); never commits the
    #   outer transaction. A pure delegation was almost enough — the import's
    #   flush also sits inside the store adapters' savepoints — but that
    #   protection holds only while every object shares ONE session, which
    #   neither AuditSink.transaction() nor apply_channel_import requires: a
    #   direct caller wiring a sink on a DIFFERENT session, catching the
    #   failure, and committing would persist the prefix (PR #196 round 3,
    #   codex). The savepoint makes the sink's own promise unconditional.
    # Standards: Exceptions propagate; nests harmlessly under the store
    #   savepoints when sessions are shared (savepoint stacks).
    # Blast Radius: Whether a failed multi-record audit batch can persist a
    #   prefix for a catching caller on a separate session. Request-path end
    #   state unchanged.
    # Connections:
    #   - File: backend/ums_smart_revenue/auth/audit_service.py -> the
    #     protocol contract and the in-memory truncating implementation.
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     wraps the buffered flush in this boundary.
    # ========================================================================
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Wrap the appends in a SAVEPOINT on this sink's session."""
        with self._session.begin_nested():
            yield


# ============================================================================
# Purpose: Audit sink that writes audit_logs through the CALLER'S tenant-lane
#   session, elevating to app_platform per append via db/lane.py:platform_lane.
#   Because the audit INSERT joins the caller's transaction, audit rows and the
#   domain writes they describe commit or roll back TOGETHER — closing the
#   two-session commit-order race where a separately committed platform audit
#   session records success for a tenant transaction that then fails to commit.
# Database/ORM: AuditLogORM (audit_logs is TENANT_PLATFORM_ONLY_WRITE, hence
#   the per-append elevation), UserORM (actor-existence read).
# Standards: Same append contract as SqlAlchemyAuditSink (flush inside the
#   elevated block so the INSERT executes as app_platform); platform_lane is a
#   no-op off Postgres, so SQLite behaves exactly as the shared-session wiring
#   already does. platform_lane is not nest-safe — appends are sequential and
#   never nested here.
# Blast Radius: Audit atomicity for routes that opt in (the bulk channel
#   import, the CMS group sync). No RLS policy, grant, or audit semantics
#   change.
# Connections:
#   - File: backend/ums_smart_revenue/db/lane.py -> sanctioned single-session
#     elevation precedent this sink builds on.
#   - File: backend/ums_smart_revenue/api/dependencies_audit.py ->
#     current_atomic_audit_sink wiring for the import and group-sync routes.
# ============================================================================
class PlatformLaneAuditSink:
    """Persist audit records on the caller's session via platform-lane elevation."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind audit writes to the caller's (tenant-lane) session."""
        self._session = session
        self._inner = SqlAlchemyAuditSink(session, tenant_id=tenant_id)
        # PUBLIC unit-of-work identity — same contract as the inner sink's;
        # the wrapper is what callers hand to apply_channel_import.
        self.sql_unit_of_work: object | None = session

    def append(self, record: AuditRecord) -> None:
        """Append one audit row inside the caller's transaction, elevated.

        Pending tenant-lane work is flushed FIRST, under the tenant role: the
        inner append issues a SELECT (which can autoflush) and then flushes,
        so without this pre-flush any pending domain writes on the shared
        session would execute while app_platform is active — widening the
        privilege surface those statements run under. After the pre-flush the
        elevated window executes only the actor-existence read and the audit
        INSERT itself.
        """
        self._session.flush()
        with platform_lane(self._session):
            self._inner.append(record)

    def rollback(self) -> None:
        """Rollback and detach pending objects after fail-closed audit errors."""
        self._inner.rollback()

    # ========================================================================
    # Purpose: The AuditSink.transaction() boundary on the platform-lane sink
    #   — a SAVEPOINT on the CALLER'S tenant-lane session, so a failed batch
    #   discards its accepted prefix without any assumption about who else
    #   shares the session (same promise as SqlAlchemyAuditSink.transaction,
    #   whose block carries the full rationale).
    # Database/ORM: SAVEPOINT via Session.begin_nested() on the same session
    #   the elevated appends join; never commits the outer transaction.
    # Standards: The per-append platform-lane elevation is ORTHOGONAL:
    #   elevation changes the ROLE a statement runs under, never which
    #   transaction or savepoint it belongs to, so a rollback to this
    #   savepoint discards the elevated INSERTs exactly as any others.
    #   Exceptions propagate; nests harmlessly under the store savepoints
    #   when sessions are shared.
    # Blast Radius: Whether a failed multi-record batch through the atomic
    #   route sink can persist a prefix for a catching caller. Request-path
    #   end state unchanged.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     wraps the buffered flush in this boundary.
    #   - File: backend/ums_smart_revenue/db/lane.py -> the elevation this
    #     boundary is orthogonal to.
    # ========================================================================
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Wrap the appends in a SAVEPOINT, like the inner sink's boundary."""
        with self._session.begin_nested():
            yield


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
