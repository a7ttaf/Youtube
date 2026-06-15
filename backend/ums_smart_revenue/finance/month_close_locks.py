"""Transaction-scoped finance month advisory lock helpers."""

from hashlib import blake2b
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_FINANCE_MONTH_LOCK_KEY_PREFIX = "finance-month-close:"
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


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
