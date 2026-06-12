"""Unit tests for ``connector_tenant_context``.

The CLI (and, later, the executor worker) drives ``run_one`` outside any
FastAPI request, so the per-transaction RLS session hook
(``db/session.py:_apply_tenant_isolation``) finds no ``TENANT_CTX`` and pins the
restricted ``app_tenant`` lane with NO trusted tenant row -> every tenant-table
policy denies all rows and the run dies fail-closed at the credential read.

``connector_tenant_context(tenant_id, session=...)`` loads the tenant by id via
``SqlAlchemyTenantRepository`` and enforces the ACTIVE-only lifecycle gate the
``TenantResolverMiddleware`` applies on web requests. The fabrication path
(no session) is retained for unit tests that exercise the contextvar contract
without a live DB; the production CLI / executor always pass a real session.
These tests pin both contracts: the contextvar shape (no-DB path) and the
lifecycle gate (SQLite-backed path with a seeded ``tenants`` table).
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.connectors.google.errors import TenantLifecycleError
from ums_smart_revenue.connectors.runs.tenant_context import (
    connector_tenant_context,
)
from ums_smart_revenue.db.tenant_models import TenantORM
from ums_smart_revenue.tenancy.context import TENANT_CTX, get_current_tenant

_TENANT_ID = UUID("00000000-0000-0000-0000-0000009c0001")


def test_sets_current_tenant_id_inside_block() -> None:
    """Inside the block the active tenant carries the supplied id (no-session path)."""
    assert get_current_tenant() is None
    with connector_tenant_context(_TENANT_ID):
        tenant = get_current_tenant()
        assert tenant is not None
        assert tenant.id == _TENANT_ID
    assert get_current_tenant() is None


def test_resets_context_on_exception() -> None:
    """An exception inside the block still resets ``TENANT_CTX`` to its prior value."""
    assert get_current_tenant() is None
    with pytest.raises(ValueError, match="boom"):
        with connector_tenant_context(_TENANT_ID):
            assert get_current_tenant() is not None
            raise ValueError("boom")
    assert get_current_tenant() is None


def test_restores_prior_tenant_value() -> None:
    """The token reset restores a previously-set tenant, not just ``None``."""
    other_id = UUID("00000000-0000-0000-0000-0000009c0002")
    token = TENANT_CTX.set(
        # Reuse the helper to build a prior tenant so the test stays
        # independent of the full Tenant constructor surface.
        _build_prior_tenant(other_id)
    )
    try:
        with connector_tenant_context(_TENANT_ID):
            assert get_current_tenant().id == _TENANT_ID
        restored = get_current_tenant()
        assert restored is not None
        assert restored.id == other_id
    finally:
        TENANT_CTX.reset(token)


def _build_prior_tenant(tenant_id: UUID):
    """Build a stand-in prior tenant via the same helper under test."""
    with connector_tenant_context(tenant_id):
        return get_current_tenant()


# ---------------------------------------------------------------------------
# Lifecycle gate (session-backed path): the CLI / executor must load the
# tenant by id and reject SUSPENDED / ARCHIVED / missing rows before setting
# the contextvar. Tests below use SQLite with a seeded ``tenants`` table --
# the same gate the production CLI applies against the real Postgres registry.
# ---------------------------------------------------------------------------


def _build_sqlite_session(*, rows: list[tuple[UUID, str, str]]) -> Session:
    """Return a session bound to a fresh in-memory SQLite with a seeded tenants table."""
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    TenantORM.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    for tenant_id, slug, status in rows:
        session.add(
            TenantORM(
                id=tenant_id,
                slug=slug,
                display_name="seed",
                primary_currency="USD",
                status=status,
                onboarding_at=now,
                created_at=now,
                updated_at=now,
            )
        )
    session.commit()
    return session


def test_session_path_loads_active_tenant() -> None:
    """An ACTIVE tenant row is loaded and placed in TENANT_CTX."""
    tenant_id = uuid4()
    session = _build_sqlite_session(
        rows=[(tenant_id, f"t-{tenant_id}", "ACTIVE")]
    )
    try:
        assert get_current_tenant() is None
        with connector_tenant_context(tenant_id, session=session):
            tenant = get_current_tenant()
            assert tenant is not None
            assert tenant.id == tenant_id
            assert tenant.status.value == "ACTIVE"
        assert get_current_tenant() is None
    finally:
        session.close()


def test_session_path_rejects_suspended_tenant() -> None:
    """A SUSPENDED tenant raises TenantLifecycleError and does NOT set the contextvar."""
    tenant_id = uuid4()
    session = _build_sqlite_session(
        rows=[(tenant_id, f"t-{tenant_id}", "SUSPENDED")]
    )
    try:
        with pytest.raises(TenantLifecycleError) as exc_info:
            with connector_tenant_context(tenant_id, session=session):
                pytest.fail("block body must not run for a SUSPENDED tenant")
        assert exc_info.value.tenant_id == tenant_id
        assert exc_info.value.status == "SUSPENDED"
        # The contextvar is NOT set when the lookup rejects.
        assert get_current_tenant() is None
    finally:
        session.close()


def test_session_path_rejects_archived_tenant() -> None:
    """An ARCHIVED tenant raises TenantLifecycleError and does NOT set the contextvar."""
    tenant_id = uuid4()
    session = _build_sqlite_session(
        rows=[(tenant_id, f"t-{tenant_id}", "ARCHIVED")]
    )
    try:
        with pytest.raises(TenantLifecycleError) as exc_info:
            with connector_tenant_context(tenant_id, session=session):
                pytest.fail("block body must not run for an ARCHIVED tenant")
        assert exc_info.value.tenant_id == tenant_id
        assert exc_info.value.status == "ARCHIVED"
        assert get_current_tenant() is None
    finally:
        session.close()


def test_session_path_rejects_missing_tenant() -> None:
    """A UUID with no row in ``tenants`` raises TenantLifecycleError(status=None)."""
    tenant_id = uuid4()
    session = _build_sqlite_session(rows=[])  # no tenants seeded
    try:
        with pytest.raises(TenantLifecycleError) as exc_info:
            with connector_tenant_context(tenant_id, session=session):
                pytest.fail("block body must not run for a missing tenant")
        assert exc_info.value.tenant_id == tenant_id
        assert exc_info.value.status is None
        assert get_current_tenant() is None
    finally:
        session.close()


def test_session_path_resets_context_on_lookup_failure() -> None:
    """A failed lifecycle check resets the contextvar to its prior value (None here)."""
    tenant_id = uuid4()
    session = _build_sqlite_session(
        rows=[(tenant_id, f"t-{tenant_id}", "SUSPENDED")]
    )
    try:
        prior = get_current_tenant()
        assert prior is None
        with pytest.raises(TenantLifecycleError):
            with connector_tenant_context(tenant_id, session=session):
                pass
        assert get_current_tenant() is prior
    finally:
        session.close()
