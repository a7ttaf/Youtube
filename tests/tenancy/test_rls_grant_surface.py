"""Postgres-only proof that the RLS rollout grants app roles the full table
surface, so a restricted runtime login does not break on NON-tenant tables.

The application request lane runs as ``app_tenant`` whenever a tenant is in
context. Under the documented restricted login (non-owner/non-superuser),
``app_tenant`` only has privileges that the migration grants. These tables have
no tenant_id and are platform-shared, so they are reachable by privilege (not
RLS): authz catalogs (``permissions``), ``currencies``,
``currency_exchange_rates``, and the ``committed_allocation_*`` child tables.
Isolation of the 25 tenant tables is proved separately in test_isolation.py and
the migration test; here we only prove reachability.
"""

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from tests.db._postgres_helpers import require_postgres_url

# Representative NON-tenant tables the app lane touches; none carry tenant_id,
# so RLS does not apply and only the blanket GRANT keeps them reachable.
_NON_TENANT_READ_TABLES = (
    "permissions",
    "currencies",
    "currency_exchange_rates",
    "committed_allocation_lines",
)


def _upgrade(url: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


def test_app_tenant_can_reach_non_tenant_tables():
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text('SET ROLE "app_tenant"'))
            # A GUC is still set so any incidentally RLS-protected table would
            # not fail closed; these target tables are non-tenant though.
            conn.execute(
                sa.text(
                    "SELECT set_config('app.current_tenant_id', :a, false)"
                ),
                {"a": "00000000-0000-0000-0000-000000000001"},
            )
            for table in _NON_TENANT_READ_TABLES:
                # count(*) on possibly-empty tables: success == no
                # permission-denied; a privilege gap would raise here.
                count = conn.execute(
                    sa.text(f"SELECT count(*) FROM {table}")
                ).scalar()
                assert count is not None, f"{table} unreachable as app_tenant"
    finally:
        engine.dispose()
