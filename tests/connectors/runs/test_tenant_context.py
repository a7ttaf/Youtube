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
from ums_smart_revenue.tenancy.models import TenantStatus

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
    with pytest.raises(ValueError, match="boom"):  # skipcq: PTC-W0062
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


def _build_sqlite_session(*, rows: list[tuple[UUID, str, TenantStatus]]) -> Session:
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
                # TenantStatus is a StrEnum, so the value is the canonical
                # status string ("ACTIVE" / "SUSPENDED" / "ARCHIVED") that
                # the tenants.status CHECK constraint accepts.
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
    session = _build_sqlite_session(rows=[(tenant_id, f"t-{tenant_id}", TenantStatus.ACTIVE)])
    try:
        assert get_current_tenant() is None
        with connector_tenant_context(tenant_id, session=session):
            tenant = get_current_tenant()
            assert tenant is not None
            assert tenant.id == tenant_id
            assert tenant.status == TenantStatus.ACTIVE
        assert get_current_tenant() is None
    finally:
        session.close()


def test_session_path_rejects_suspended_tenant() -> None:
    """A SUSPENDED tenant raises TenantLifecycleError and does NOT set the contextvar."""
    tenant_id = uuid4()
    session = _build_sqlite_session(rows=[(tenant_id, f"t-{tenant_id}", TenantStatus.SUSPENDED)])
    try:
        with pytest.raises(TenantLifecycleError) as exc_info:  # skipcq: PTC-W0062
            with connector_tenant_context(tenant_id, session=session):
                pytest.fail("block body must not run for a SUSPENDED tenant")
        assert exc_info.value.tenant_id == tenant_id
        assert exc_info.value.status == TenantStatus.SUSPENDED.value
        # The contextvar is NOT set when the lookup rejects.
        assert get_current_tenant() is None
        # ChatGPT codex review (PR #94 P2, posted 2026-06-12 on commit
        # f07fb6d): the lookup transaction is rolled back on the
        # rejected-lookup path too, not only on the success path. A
        # worker that catches TenantLifecycleError and retries against
        # a different tenant must see a clean idle session, not an
        # open transaction pinning app_tenant with
        # app_current_tenant_id()=NULL.
        assert not session.in_transaction()
    finally:
        session.close()


def test_session_path_rejects_archived_tenant() -> None:
    """An ARCHIVED tenant raises TenantLifecycleError and does NOT set the contextvar."""
    tenant_id = uuid4()
    session = _build_sqlite_session(rows=[(tenant_id, f"t-{tenant_id}", TenantStatus.ARCHIVED)])
    try:
        with pytest.raises(TenantLifecycleError) as exc_info:  # skipcq: PTC-W0062
            with connector_tenant_context(tenant_id, session=session):
                pytest.fail("block body must not run for an ARCHIVED tenant")
        assert exc_info.value.tenant_id == tenant_id
        assert exc_info.value.status == TenantStatus.ARCHIVED.value
        assert get_current_tenant() is None
    finally:
        session.close()


def test_session_path_rejects_missing_tenant() -> None:
    """A UUID with no row in ``tenants`` raises TenantLifecycleError(status=None)."""
    tenant_id = uuid4()
    session = _build_sqlite_session(rows=[])  # no tenants seeded
    try:
        with pytest.raises(TenantLifecycleError) as exc_info:  # skipcq: PTC-W0062
            with connector_tenant_context(tenant_id, session=session):
                pytest.fail("block body must not run for a missing tenant")
        assert exc_info.value.tenant_id == tenant_id
        assert exc_info.value.status is None
        assert get_current_tenant() is None
        # Same rejected-lookup rollback contract as
        # test_session_path_rejects_suspended_tenant: a failed lookup
        # must leave the session idle, not pinned to an open tenant-lane
        # transaction with app_current_tenant_id()=NULL.
        assert not session.in_transaction()
    finally:
        session.close()


def test_session_path_resets_context_on_lookup_failure() -> None:
    """A failed lifecycle check resets the contextvar to its prior value (None here)."""
    tenant_id = uuid4()
    session = _build_sqlite_session(rows=[(tenant_id, f"t-{tenant_id}", TenantStatus.SUSPENDED)])
    try:
        prior = get_current_tenant()
        assert prior is None
        with pytest.raises(TenantLifecycleError):  # skipcq: PTC-W0062
            with connector_tenant_context(tenant_id, session=session):
                pass
        assert get_current_tenant() is prior
    finally:
        session.close()


def test_session_path_ends_lookup_transaction_when_session_was_idle() -> None:
    """ChatGPT codex review (PR #94 P1, posted 2026-06-12 on commit 4a05480):
    the ``get_by_id`` SELECT autobegins a Postgres transaction whose
    after_begin hook runs with ``TENANT_CTX`` empty, pins ``app_tenant``
    with ``app_current_tenant_id()=NULL``, and leaves the transaction
    open. The caller's first DB call (e.g. ``run_one``'s
    ``_load_credential``) then runs in the SAME transaction with NULL
    tenant context -- RLS denies all tenant rows and the run recreates
    the fail-closed-empty CLI path this whole change is meant to fix.

    The fix: after the lookup, if the session was NOT already in a
    transaction (i.e. the CLI path where the helper opens the first
    transaction), end the lookup transaction via ``session.rollback()``
    so the caller's first DB call autobegins a fresh transaction whose
    after_begin re-fires WITH ``TENANT_CTX`` set. This test pins that
    contract by asserting the session is NOT in a transaction at the
    start of the body (the lookup transaction was ended) and that the
    body can still run a fresh DB call.

    PG-tier verification of the after_begin behaviour itself lives in
    ``tests/connectors/runs/test_run_one_rls_postgres.py`` (requires
    ``UMS_TEST_DATABASE_URL``). The SQLite tier exercises the same
    transaction-boundary contract.
    """
    tenant_id = uuid4()
    session = _build_sqlite_session(rows=[(tenant_id, f"t-{tenant_id}", TenantStatus.ACTIVE)])
    try:
        # Pre-condition: the session is idle (no transaction). The helper
        # will open the first transaction for the lookup and then end it.
        assert not session.in_transaction()
        with connector_tenant_context(tenant_id, session=session):
            # The lookup transaction was ended before the body ran, so the
            # session is back to idle. The caller's first DB call (which
            # we simulate with a session.get below) will autobegin a
            # fresh transaction on the now-set TENANT_CTX.
            assert not session.in_transaction()
            # Confirm the body can still run a DB call against the same
            # session after the lookup transaction was ended. The SELECT
            # returns the seeded row and the session opens a healthy new
            # transaction for it.
            row = session.get(TenantORM, tenant_id)
            assert row is not None
            assert row.id == tenant_id
        # Close the body's transaction so the test fixture can release the
        # in-memory engine cleanly. The helper did not need to do this --
        # the body owns the transaction it opened.
        if session.in_transaction():
            session.rollback()
        assert not session.in_transaction()
    finally:
        session.close()


def test_session_path_does_not_touch_caller_owned_outer_transaction() -> None:
    """When the caller already has a transaction in flight, the helper
    must not end it: the lookup joins the caller's transaction as a
    savepoint, and rolling back here would discard the caller's
    uncommitted work. The caller's outer transaction is the boundary
    the helper respects.

    PG-tier verification of the role/context pinning under a caller-owned
    transaction lives in
    ``tests/connectors/runs/test_run_one_rls_postgres.py``. This test
    pins the SQLite-tier invariant that an outer transaction survives
    the helper's exit.
    """
    from sqlalchemy import text

    tenant_id = uuid4()
    session = _build_sqlite_session(rows=[(tenant_id, f"t-{tenant_id}", TenantStatus.ACTIVE)])
    try:
        # Caller opens a transaction and writes a sentinel row that must
        # survive the helper's exit.
        session.execute(text("CREATE TABLE _outer_marker (id INTEGER)"))
        session.execute(text("INSERT INTO _outer_marker (id) VALUES (1)"))
        assert session.in_transaction()
        try:
            with connector_tenant_context(tenant_id, session=session):
                # Inside the helper, the session is still in the
                # caller's transaction (the lookup joined it).
                assert session.in_transaction()
            # Outside the helper, the caller's transaction must still be
            # alive (we did not roll it back).
            assert session.in_transaction()
            marker = session.execute(text("SELECT id FROM _outer_marker")).scalar()
            assert marker == 1
        finally:
            session.rollback()
    finally:
        session.close()
