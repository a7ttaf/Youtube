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
#   - File: backend/ums_smart_revenue/api/channels.py -> atomic sink wiring.
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
    # Purpose: The AuditSink.transaction() boundary, delegated to the caller's
    #   enclosing Session transaction — this sink's appends already live
    #   inside it, so a raise reaching the session owner discards them there.
    # Database/ORM: None of its own; opens nothing, commits nothing, adds no
    #   SAVEPOINT. When the bulk import flushes through this sink the flush
    #   additionally sits INSIDE the store adapters' savepoints, which is
    #   what makes a mid-flush failure discard the accepted prefix even for a
    #   caller that catches the exception (review #184, C2).
    # Standards: Mirrors the SQL store adapters' delegation; exceptions
    #   propagate untouched.
    # Blast Radius: None — a documented no-op on this tier.
    # Connections:
    #   - File: backend/ums_smart_revenue/auth/audit_service.py -> the
    #     protocol contract and the in-memory truncating implementation.
    #   - File: backend/ums_smart_revenue/org/sql_channel_registry.py -> the
    #     SAVEPOINT that actually contains the import's flush.
    # ========================================================================
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Delegate batch atomicity to the enclosing Session transaction."""
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
#   - File: backend/ums_smart_revenue/api/channels.py -> current_atomic_audit_sink
#     wiring for the import and group-sync routes.
# ============================================================================
class PlatformLaneAuditSink:
    """Persist audit records on the caller's session via platform-lane elevation."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind audit writes to the caller's (tenant-lane) session."""
        self._session = session
        self._inner = SqlAlchemyAuditSink(session, tenant_id=tenant_id)

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

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Delegate batch atomicity to the caller's transaction, like the inner sink.

        Same delegation as ``SqlAlchemyAuditSink.transaction`` (see its block):
        the per-append platform-lane elevation is orthogonal — elevation
        changes the ROLE a statement runs under, never which transaction it
        belongs to, so the appended rows discard with the caller's rollback
        exactly as before.
        """
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
