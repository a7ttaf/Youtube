"""Tenancy domain model — frozen dataclasses for in-process use.

These are separate from the SQLAlchemy ORM in
``ums_smart_revenue.db.tenant_models`` on purpose:

* ORM objects are tied to a session lifetime and lazy-load attributes;
  passing them across request scopes or into background workers is a
  classic source of detached-instance bugs.
* The domain dataclass is immutable, easy to compare, safe to log, and
  trivial to hold inside a ``contextvars.ContextVar``.

Conversion happens at the repository boundary (see ``repository.py``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

# Sentinel timestamp used by placeholder tenant fabrications. The RLS session
# hook in ``db/session.py`` reads ONLY ``Tenant.id`` when it writes the trusted
# tenant-context row; the other fields on the fabricated placeholder are
# therefore never persisted, never validated against the ``tenants`` table,
# and never compared against another timestamp. A fixed UTC epoch keeps the
# fabrication deterministic for tests and makes the placeholder obviously
# synthetic if it ever leaks into a log.
PLACEHOLDER_TENANT_EPOCH: datetime = datetime(1970, 1, 1, tzinfo=UTC)

# Default currency for the placeholder tenant. Mirrors the ``tenants.primary_currency``
# schema default so the fabricated dataclass stays valid without the caller
# having to know the schema default. Promoted to a module constant so a future
# schema-default change (or a test that needs a non-USD placeholder) does not
# have to edit the factory body.
#
# SOURCE OF TRUTH, and what this constant is NOT: this mirrors the SQL
# ``server_default`` only. It is deliberately NOT the platform's declared
# primary currency, which has two real sources depending on the auth mode:
#   * database mode  -> the ``tenants.primary_currency`` column, read per
#     request by the resolver;
#   * headers mode   -> ``AppSettings.tenant_primary_currency``
#     (``UMS_TENANT_PRIMARY_CURRENCY``), stamped by ``app._bootstrap_tenant``.
# Placeholder tenants are never persisted and the RLS hook reads only
# ``Tenant.id``, so this value must keep tracking the schema default rather
# than following an operator's currency decision. Read the active tenant's
# currency through ``tenancy.currency.get_tenant_primary_currency()``.
DEFAULT_PRIMARY_CURRENCY: str = "USD"


class TenantStatus(StrEnum):
    """Lifecycle states for a tenant — mirrors the SQL CHECK constraint."""

    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    ARCHIVED = "ARCHIVED"


@dataclass(frozen=True)
class Tenant:
    """Immutable view of a row in the ``tenants`` table.

    All datetime fields are timezone-aware (the schema declares them with
    ``DateTime(timezone=True)``). Currency codes are uppercase three-letter
    ISO 4217 strings.
    """

    id: UUID
    slug: str
    display_name: str
    primary_currency: str
    status: TenantStatus
    onboarding_at: datetime
    created_at: datetime
    updated_at: datetime

    @property
    def is_active(self) -> bool:
        """Convenience flag for the most common decision the API will make."""
        return self.status == TenantStatus.ACTIVE


# ============================================================================
# Purpose: Build a minimal ``Tenant`` for the in-process RLS tenant-context
#   hook. Used by code paths that need to set ``TENANT_CTX`` outside the
#   resolver middleware without round-tripping the ``tenants`` table (the
#   test-only branch of ``connectors/runs/tenant_context.py`` and the
#   Bucket-A ``_audit_failed_before_start`` audit path in
#   ``connectors/runs/executor.py``). The hook only reads ``Tenant.id`` to
#   write the trusted context row; the remaining fields are placeholders that
#   are never persisted.
# Database/ORM: None — in-memory domain dataclass only.
# Standards: Stable factory name, deterministic timestamp, ``ACTIVE`` status
#   so any caller that accidentally relies on the lifecycle gate gets the
#   same answer as a real ACTIVE tenant. ``primary_currency`` is the schema
#   NOT NULL default so the dataclass contract stays satisfied.
# Blast Radius: Authorization / RLS — centralized so the placeholder shape
#   never drifts between call sites. A drift here would silently make the
#   RLS hook see a different ``Tenant.id`` and could lock out an entire
#   tenant.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/context.py -> reads
#     ``Tenant.id`` from the contextvar.
#   - File: backend/ums_smart_revenue/db/session.py -> uses that id to set
#     the trusted ``app_current_tenant_id()`` row.
#   - File: backend/ums_smart_revenue/connectors/runs/tenant_context.py ->
#     test-only fabrication branch now calls this factory.
#   - File: backend/ums_smart_revenue/connectors/runs/executor.py ->
#     ``_audit_failed_before_start`` now calls this factory.
# ============================================================================
def make_placeholder_tenant(
    tenant_id: UUID,
    *,
    slug: str,
    display_name: str,
    primary_currency: str = DEFAULT_PRIMARY_CURRENCY,
) -> Tenant:
    """Return a minimal ``Tenant`` carrying only the id needed by the RLS hook.

    Parameters
    ----------
    tenant_id:
        The tenant id the RLS context row will be pinned to.
    slug, display_name:
        Caller-supplied placeholders so the audit / test branch can
        self-identify the fabrication in logs.
    primary_currency:
        Defaults to ``DEFAULT_PRIMARY_CURRENCY`` (mirrors the
        ``tenants.primary_currency`` schema default) so the dataclass
        stays valid without the caller having to know the schema default.
    """
    return Tenant(
        id=tenant_id,
        slug=slug,
        display_name=display_name,
        primary_currency=primary_currency,
        status=TenantStatus.ACTIVE,
        onboarding_at=PLACEHOLDER_TENANT_EPOCH,
        created_at=PLACEHOLDER_TENANT_EPOCH,
        updated_at=PLACEHOLDER_TENANT_EPOCH,
    )
