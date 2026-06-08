"""Postgres-only tenant-isolation matrix exercised through the trusted context.

Proves the database boundary holds even when application-level tenant filters
are deliberately bypassed: bare ``SELECT``s with no ``WHERE tenant_id`` are
filtered by RLS, cross-tenant writes are rejected by ``WITH CHECK``, an unset
tenant context returns no rows, and the ``app_platform`` lane does not gain a
hidden tenant-table bypass. One representative table (``org_units``) is
sufficient — the migration test already proves every tenant table carries the
policy.
"""

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from tests.db._postgres_helpers import require_postgres_url

# Tenant A is seeded by the tenants-foundation migration; tenant B is seeded
# here as the table owner (RLS does not restrict the owner connection).
A = "00000000-0000-0000-0000-000000000001"
B = "00000000-0000-0000-0000-000000000002"

# Fixed ids (distinct from app data) keep reruns idempotent with ON CONFLICT.
ORG_A = "00000000-0000-0000-0000-0000000000a1"
ORG_B = "00000000-0000-0000-0000-0000000000b2"


def _set_trusted_tenant(conn: sa.Connection, tenant_id: str) -> None:
    """Populate the trusted tenant context through the platform lane."""
    conn.execute(sa.text('SET ROLE "app_platform"'))
    conn.execute(
        sa.text("SELECT set_app_current_tenant_id(:a)"),
        {"a": tenant_id},
    )
    conn.execute(sa.text('SET ROLE "app_tenant"'))


def _upgrade(url: str) -> None:
    """Apply the tenant-RLS migration to the test database."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


def _seed(engine: sa.Engine) -> None:
    """Seed tenant B and representative org-unit rows for isolation checks."""
    # Seed tenant B + an A-owned and a B-owned org_units root row (parent_id
    # NULL to sidestep the composite self-FK) as the owner connection.
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO tenants (id, slug, display_name, primary_currency) "
                "VALUES (:id, 'rotana', 'Rotana', 'USD') ON CONFLICT DO NOTHING"
            ),
            {"id": B},
        )
        conn.execute(
            sa.text(
                "INSERT INTO org_units (id, tenant_id, parent_id, type, name) "
                "VALUES (:id, :t, NULL, 'HOLDING', 'A Holding') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": ORG_A, "t": A},
        )
        conn.execute(
            sa.text(
                "INSERT INTO org_units (id, tenant_id, parent_id, type, name) "
                "VALUES (:id, :t, NULL, 'HOLDING', 'B Holding') "
                "ON CONFLICT DO NOTHING"
            ),
            {"id": ORG_B, "t": B},
        )


def test_app_tenant_cannot_read_other_tenant_rows():
    """Verify app_tenant reads only the rows for the configured tenant."""
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        _seed(engine)
        with engine.connect() as conn:
            _set_trusted_tenant(conn, A)
            # Bare select, NO WHERE tenant_id — RLS must filter to A only.
            rows = conn.execute(
                sa.text("SELECT tenant_id FROM org_units")
            ).scalars().all()
            assert rows, "expected at least the seeded A-owned row to be visible"
            assert all(str(t) == A for t in rows)
    finally:
        engine.dispose()


def test_with_check_blocks_cross_tenant_insert():
    """Verify WITH CHECK blocks inserting rows for another tenant."""
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        _seed(engine)
        with engine.connect() as conn:
            _set_trusted_tenant(conn, A)
            with pytest.raises(Exception):
                # Inserting a B-owned row while trusted context=A violates WITH CHECK.
                conn.execute(
                    sa.text(
                        "INSERT INTO org_units "
                        "(id, tenant_id, parent_id, type, name) "
                        "VALUES "
                        "(gen_random_uuid(), :b, NULL, 'HOLDING', 'X')"
                    ),
                    {"b": B},
                )
                conn.commit()
    finally:
        engine.dispose()


def test_missing_context_fails_closed():
    """Verify the tenant lane fails closed when the trusted context is absent."""
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text('SET ROLE "app_tenant"'))
            rows = conn.execute(sa.text("SELECT * FROM org_units")).all()
            assert rows == []
    finally:
        engine.dispose()


def test_app_platform_does_not_bypass_tenant_rls():
    """Verify app_platform still needs tenant context for tenant-scoped tables."""
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        _seed(engine)
        with engine.connect() as conn:
            conn.execute(sa.text('SET ROLE "app_platform"'))
            count = conn.execute(sa.text("SELECT COUNT(*) FROM org_units")).scalar()
            assert count == 0
    finally:
        engine.dispose()
