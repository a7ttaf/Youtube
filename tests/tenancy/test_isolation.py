"""Postgres-only tenant-isolation matrix exercised as ``app_tenant``.

Proves the database boundary holds even when application-level tenant filters
are deliberately bypassed: bare ``SELECT``s with no ``WHERE tenant_id`` are
filtered by RLS, cross-tenant writes are rejected by ``WITH CHECK``, an unset
GUC fails closed (errors, not empty), and the ``app_platform`` bypass lane can
read across tenants. One representative table (``org_units``) is sufficient —
the migration test already proves every tenant table carries the policy.
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
            conn.execute(sa.text('SET ROLE "app_tenant"'))
            # set_config(name, value, is_local=false) -> session-scoped GUC;
            # parameterized so the tenant id flows as data, not literal SQL.
            conn.execute(
                sa.text("SELECT set_config('app.current_tenant_id', :a, false)"),
                {"a": A},
            )
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
            conn.execute(sa.text('SET ROLE "app_tenant"'))
            conn.execute(
                sa.text("SELECT set_config('app.current_tenant_id', :a, false)"),
                {"a": A},
            )
            with pytest.raises(Exception):
                # Inserting a B-owned row while GUC=A violates WITH CHECK.
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


def test_missing_guc_fails_closed():
    """Verify the tenant lane fails closed when the tenant GUC is absent."""
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text('SET ROLE "app_tenant"'))
            # No GUC set: current_setting without missing_ok must error,
            # not silently return an empty result set.
            with pytest.raises(Exception):
                conn.execute(sa.text("SELECT * FROM org_units")).all()
    finally:
        engine.dispose()


def test_app_platform_reads_across_tenants():
    """Verify app_platform bypasses RLS and can read across tenants."""
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        _seed(engine)
        with engine.connect() as conn:
            conn.execute(sa.text('SET ROLE "app_platform"'))
            # BYPASSRLS: no GUC needed; can see all tenants.
            total = conn.execute(
                sa.text("SELECT COUNT(*) FROM org_units")
            ).scalar()
            assert total >= 2
    finally:
        engine.dispose()
