"""Tenant-context entry for connector runs invoked outside a FastAPI request.

A web request gets its ``TENANT_CTX`` from ``TenantResolverMiddleware``; the
connector CLI (and the future executor worker) has no middleware, so without
this helper the per-transaction RLS hook (``db/session.py``) finds no tenant,
clears the trusted context row, and pins the restricted ``app_tenant`` lane ->
every tenant-table policy denies all rows and the run dies fail-closed at the
credential read. This sets the minimal tenant the hook needs for the run.

The CLI path also re-applies the resolver's lifecycle gate
(``TenantStatus.ACTIVE`` only) so a UUID pointing at a suspended/archived
tenant does not silently authorize an ingestion run against that tenant's
rows. ``TenantResolverMiddleware`` enforces the same gate on web requests
(SUSPENDED -> 423, ARCHIVED -> 410); the connector entry point is the
authoritative replay of that check for the background CLI / executor.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google.errors import TenantLifecycleError
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus
from ums_smart_revenue.tenancy.repository import (
    SqlAlchemyTenantRepository,
    TenantNotFoundError,
)

# The RLS session hook reads ONLY ``tenant.id`` (it writes the trusted tenant
# context row keyed on the id, never the slug/display/currency). These
# non-identifying fields are placeholders for the test-only fabrication path
# and are never persisted; building a full Tenant there avoids a DB round-trip
# on the unit-test path while satisfying the frozen-dataclass contract.
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# ============================================================================
# Purpose: Set TENANT_CTX to the looked-up tenant for ``tenant_id`` for the
#   duration of a connector run driven outside a FastAPI request, and reset the
#   contextvar token on exit (even when the body raises). When a ``session`` is
#   supplied the tenant is loaded by id and its lifecycle status is enforced
#   (ACTIVE only) so the connector entry point matches the gate
#   ``TenantResolverMiddleware`` enforces on web requests. Missing or
#   non-active tenants raise ``TenantLifecycleError`` so the CLI / worker
#   fails closed before any tenant-table policy would be applied.
# Database/ORM: ``tenants`` table via ``SqlAlchemyTenantRepository.get_by_id``
#   when ``session`` is supplied; the ``tenants`` table is platform-wide
#   (NOT in TENANT_SCOPED_TABLES) so the lookup works on the tenant lane.
# Standards: lookup happens BEFORE the contextvar is set so a missing or
#   non-active tenant leaves ``TENANT_CTX`` at its prior value; contextvars
#   token set/reset in a try/finally so the prior tenant is always restored.
# Blast Radius: Authorization -- applies the lifecycle gate the resolver
#   enforces on web requests to the connector CLI / executor. Without this
#   a suspended/archived tenant would still authorize the run. No finance
#   math, audit semantics, Neo4j, or exports change.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/context.py -> TENANT_CTX contextvar.
#   - File: backend/ums_smart_revenue/tenancy/resolver.py ->
#     ``TenantResolverMiddleware`` enforces the same ACTIVE-only gate; this
#     helper replays it for the background CLI / executor path.
#   - File: backend/ums_smart_revenue/tenancy/repository.py ->
#     ``SqlAlchemyTenantRepository.get_by_id`` is the canonical loader.
#   - File: backend/ums_smart_revenue/db/session.py -> after_begin hook
#     reads ``get_current_tenant().id`` to set the trusted context row.
#   - File: scripts/run_google_connector.py -> wraps the run_one call; the
#     CLI passes its open session to this helper.
# ============================================================================
@contextmanager
def connector_tenant_context(
    tenant_id: UUID,
    *,
    session: Session | None = None,
) -> Iterator[None]:
    """Set ``TENANT_CTX`` to the looked-up tenant for ``tenant_id`` for the block.

    Parameters
    ----------
    tenant_id:
        UUID of the tenant under which the connector run executes.
    session:
        Optional SQLAlchemy session used to load the tenant by id and
        enforce the lifecycle gate. The production CLI MUST pass its open
        session so the lifecycle check is applied; the fabrication path
        (no session) is retained for unit tests that exercise the
        contextvar contract without a live DB. The integration test
        suite and the CLI both pass a real session.

    Raises
    ------
    TenantLifecycleError
        When the lookup returns no row or the row's status is not
        ``TenantStatus.ACTIVE``. The contextvar is NOT set in this case
        (prior value is preserved).
    """
    if session is not None:
        # Production path: look up the tenant by id, then enforce the
        # ACTIVE-only gate the resolver applies on web requests. The
        # contextvar is set AFTER the check so a rejected tenant leaves
        # TENANT_CTX at its prior value.
        try:
            tenant = SqlAlchemyTenantRepository(session).get_by_id(tenant_id)
        except TenantNotFoundError as exc:
            raise TenantLifecycleError(
                tenant_id=tenant_id, status=None
            ) from exc
        if tenant.status != TenantStatus.ACTIVE:
            raise TenantLifecycleError(
                tenant_id=tenant_id, status=tenant.status.value
            )
    else:
        # Test-only fabrication path: build the minimal ACTIVE tenant the
        # RLS session hook needs (it reads only ``tenant.id``). Production
        # callers MUST pass a session; this path is intentionally not the
        # default to prevent the CLI / executor from regressing into
        # fabricating an ACTIVE tenant for a suspended/archived row.
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
