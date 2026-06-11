"""Tenant-context entry for connector runs invoked outside a FastAPI request.

A web request gets its ``TENANT_CTX`` from ``TenantResolverMiddleware``; the
connector CLI (and the future executor worker) has no middleware, so without
this helper the per-transaction RLS hook (``db/session.py``) finds no tenant,
clears the trusted context row, and pins the restricted ``app_tenant`` lane ->
every tenant-table policy denies all rows and the run dies fail-closed at the
credential read. This sets the minimal tenant the hook needs for the run.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

# The RLS session hook reads ONLY ``tenant.id`` (it writes the trusted tenant
# context row keyed on the id, never the slug/display/currency). These
# non-identifying fields are placeholders that are never persisted; building a
# full Tenant here avoids a DB round-trip on the connector hot path while
# satisfying the frozen-dataclass contract.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# ============================================================================
# Purpose: Set TENANT_CTX to the minimal Tenant the RLS session hook needs for
#   the duration of a connector run driven outside a FastAPI request, and reset
#   the contextvar token on exit (even when the body raises).
# Database/ORM: None directly; the value is read by the after_begin session hook
#   (db/session.py) which writes the trusted app_tenant_context row by id.
# Standards: contextvars token set/reset in a try/finally so the prior tenant
#   (or None) is always restored; no DB access, no secrets. The hook reads only
#   tenant.id so the other Tenant fields are deliberately non-identifying.
# Blast Radius: Authorization -- supplies the trusted tenant context that RLS
#   policies require; without it the run fails closed. No finance math, audit
#   semantics, Neo4j, or exports change.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/context.py -> TENANT_CTX contextvar.
#   - File: backend/ums_smart_revenue/db/session.py -> after_begin hook reads
#     get_current_tenant().id to set the trusted context row.
#   - File: scripts/run_google_connector.py -> wraps the run_one call.
# ============================================================================
@contextmanager
def connector_tenant_context(tenant_id: UUID) -> Iterator[None]:
    """Set ``TENANT_CTX`` to a minimal tenant for ``tenant_id`` for the block."""
    tenant = Tenant(
        id=tenant_id,
        slug=f"connector-run:{tenant_id}",
        display_name="connector run",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=_EPOCH,
        created_at=_EPOCH,
        updated_at=_EPOCH,
    )
    token = TENANT_CTX.set(tenant)
    try:
        yield
    finally:
        TENANT_CTX.reset(token)
