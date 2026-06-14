"""Defense-in-depth tenant assertion for write paths.

RLS blocks cross-tenant writes at the DB; this raises a clear typed error
*before* the round-trip so the route boundary can return 403 instead of a raw
DB exception. Pair with the route translation added in api boundaries.
"""

from __future__ import annotations

from uuid import UUID


class TenantIsolationError(Exception):
    """Raised when a write targets a tenant other than the principal's."""


def _coerce(value: object) -> UUID | None:
    """Coerce a UUID-or-str to UUID, returning None if absent/invalid."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value).strip())
    except ValueError:
        return None


def assert_tenant_match(row_tenant_id: object, principal_tenant_id: object) -> None:
    """Raise TenantIsolationError unless both ids resolve and are equal."""
    row = _coerce(row_tenant_id)
    principal = _coerce(principal_tenant_id)
    if row is None or principal is None or row != principal:
        raise TenantIsolationError("tenant mismatch between row and principal")
