# ============================================================================
# Purpose: The serialization primitives every month-scoped writer shares — the
#   transaction-scoped PostgreSQL advisory locks that order month-close
#   transitions against concurrent writers, and the single database clock that
#   makes their timestamps comparable.
# Database/ORM: PostgreSQL advisory locks (pg_advisory_xact_lock) and
#   clock_timestamp() only. This module never reads or mutates an ORM row.
# Standards: Lock keys are derived, never hand-picked — blake2b over
#   tenant+month, so two callers for the same month always collide and two
#   different months never do. LOCK ORDER IS A TOTAL ORDER and changing it
#   deadlocks: the revenue_required registry flip takes ONLY
#   REVENUE_REQUIREMENT_GUARD_MONTH; lock-time readiness takes the month key
#   THEN that sentinel; no path takes sentinel-then-month. Locks are
#   transaction-scoped (released at COMMIT/ROLLBACK, re-entrant within one
#   transaction) so no caller unlocks explicitly. Off PostgreSQL every helper
#   degrades to a no-op / app clock — SQLite is single-writer and single-host
#   in this codebase, so there is nothing to serialize.
# Blast Radius: Finance month locks, lock-time readiness, and the channel
#   revenue_required flip guard. No authorization, audit, revenue math,
#   allocation, or export behavior.
# Connections:
#   - File: backend/ums_smart_revenue/finance/month_close.py -> guards close-row
#     FOR UPDATE writes and stamps lifecycle timestamps.
#   - File: backend/ums_smart_revenue/finance/month_close_readiness.py -> guards
#     lock-time readiness rechecks.
#   - File: backend/ums_smart_revenue/org/sql_channel_registry.py -> holds the
#     sentinel guard across registry writes and stamps created_at.
# ============================================================================
"""Transaction-scoped finance month advisory locks and the shared DB clock."""

from datetime import UTC, datetime
from hashlib import blake2b
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_FINANCE_MONTH_LOCK_KEY_PREFIX = "finance-month-close:"
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)

# Sentinel "month" for the tenant-wide guard serializing revenue_required
# registry flips against lock-time readiness rechecks. Both sides acquire the
# advisory lock for this key (the registry flip alone; readiness AFTER its
# per-month key), so a flip and a month lock can never interleave their
# check/write pairs: whichever commits first is visible to the other's check.
# Deadlock-free: the flip takes ONLY this key; readiness takes month-then-this,
# and no path takes this-then-month.
REVENUE_REQUIREMENT_GUARD_MONTH = "registry-revenue-required-guard"


# ============================================================================
# Purpose: Acquire the PostgreSQL transaction-scoped guard shared by finance
#   month close checks and month-scoped writer paths.
# Database/ORM: PostgreSQL advisory lock only; no ORM row mutation.
# Standards: Tenant-aware, fail-fast UUID parsing, no route/service coupling.
# Blast Radius: Finance month locks; no authorization, audit, Neo4j, or export
#   behavior changes.
# Connections:
#   - File: backend/ums_smart_revenue/finance/month_close.py -> Uses the guard
#     before close-row FOR UPDATE writes and re-exports the helper.
#   - File: backend/ums_smart_revenue/finance/month_close_readiness.py -> Uses
#     the same guard before lock-time readiness rechecks.
# ============================================================================
def acquire_finance_month_advisory_lock(
    session: Session,
    month: str,
    *,
    tenant_id: UUID | str | None = None,
) -> None:
    """Acquire the transaction-scoped month guard used by close and writer paths."""
    if session.get_bind().dialect.name != "postgresql":
        return
    resolved_tenant_id = _resolve_tenant_id(tenant_id)
    session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_key)"),
        {"lock_key": finance_month_advisory_lock_key(month, resolved_tenant_id)},
    )


# ============================================================================
# Purpose: Produce the single timestamp source that orders a channel's
#   creation against a finance month's lock — the comparison the LOCKED-month
#   effective-dating cutoff (created_at <= locked_at) depends on.
# Database/ORM: PostgreSQL clock_timestamp() via a scalar SELECT. No ORM rows.
# Standards: BOTH sides of the comparison must call this. Independent
#   application-host wall clocks are not comparable — skew (or a host clock
#   stepping backward) can place a post-lock create before locked_at and
#   recreate the exact race the advisory guard prevents. clock_timestamp() is
#   the database's CURRENT time and, unlike now()/transaction_timestamp(),
#   advances within a transaction, so a value read after the guard wait
#   genuinely reflects post-guard ordering. Falls back to the app clock off
#   Postgres (SQLite is single-writer and single-host in this codebase).
# Blast Radius: Month-close effective dating and the revenue_required flip
#   guard. No revenue math, no allocation, no audit.
# Connections:
#   - File: backend/ums_smart_revenue/finance/month_close.py -> stamps
#     locked_at/unlocked_at with this.
#   - File: backend/ums_smart_revenue/org/sql_channel_registry.py -> stamps
#     created_at with this, after acquiring the guard.
# ============================================================================
def serialization_timestamp(session: Session) -> datetime:
    """Return the shared database clock used to order creates against locks."""
    if session.get_bind().dialect.name != "postgresql":
        return datetime.now(UTC)
    stamped = session.scalar(select(func.clock_timestamp()))
    if stamped is None:
        # clock_timestamp() cannot return NULL; this narrows Session.scalar's
        # Optional type without a cast and fails safe if it ever did.
        raise RuntimeError("clock_timestamp() returned no value")
    return stamped


def _resolve_tenant_id(tenant_id: UUID | str | None, *, use_context: bool = True) -> UUID:
    """Resolve tenant id from an explicit value, context, or default."""
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    if use_context:
        current_tenant = get_current_tenant()
        if current_tenant is not None:
            return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Parse a tenant UUID value or raise ValueError."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError("tenant_id must be a valid UUID") from exc


def finance_month_advisory_lock_key(month: str, tenant_id: UUID) -> int:
    """Return a stable signed 64-bit advisory-lock key for a finance month."""
    digest = blake2b(
        f"{_FINANCE_MONTH_LOCK_KEY_PREFIX}{tenant_id}:{month}".encode(),
        digest_size=8,
    ).digest()
    return int.from_bytes(digest, byteorder="big", signed=True)
