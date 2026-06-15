"""Postgres-only proof of the LEAST-PRIVILEGE grant surface for the app roles.

The application request lane runs as ``app_tenant`` whenever a tenant is in
context. Under the documented restricted login (non-owner/non-superuser),
``app_tenant`` only holds privileges the migration grants. The narrowed model
is:

  * **Broad SELECT** across the whole ``public`` schema (so the app can read
    platform-shared catalogs that carry no ``tenant_id`` and thus no RLS:
    ``permissions``, ``currencies``, ``currency_exchange_rates``, the
    ``committed_allocation_*`` child tables, ...).
  * **DML (INSERT/UPDATE/DELETE) ONLY** on the enumerated
    ``NON_TENANT_WRITE_TABLES`` plus the 25 RLS-isolated tenant tables. DML is
    NOT granted on platform catalogs (e.g. ``permissions``).

This test proves both halves: reads are broad, writes are narrow. Isolation of
the 25 tenant tables is proved separately in test_isolation.py.
"""

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from tests.db._postgres_helpers import require_postgres_url

# Catalog/non-tenant tables the app lane reads; none carry tenant_id, so RLS
# does not apply and only the broad SELECT grant keeps them reachable.
_NON_TENANT_READ_TABLES = (
    "permissions",
    "currencies",
    "currency_exchange_rates",
    "committed_allocation_lines",
)

_PLATFORM_ONLY_WRITE_TABLES = (
    "audit_logs",
    "finance_month_close",
    "monthly_channel_revenue_facts",
    "committed_allocation_lines",
    "committed_allocation_notes",
    "committed_allocation_unallocated",
)


def _upgrade(url: str) -> None:
    """Apply the tenant-RLS migration to the test database."""
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


def test_app_tenant_can_reach_non_tenant_tables():
    """Verify app_tenant can read the non-tenant public catalog tables."""
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text('SET ROLE "app_tenant"'))
            for table in _NON_TENANT_READ_TABLES:
                # count(*) on possibly-empty tables: success == no
                # permission-denied; a privilege gap would raise here.
                count = conn.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar()
                assert count is not None, f"{table} unreachable as app_tenant"
    finally:
        engine.dispose()


def test_app_tenant_has_write_grant_on_non_tenant_write_table():
    """Verify app_tenant retains INSERT on the exchange-rate write table."""
    # currency_exchange_rates is a NON_TENANT_WRITE_TABLES member: the app
    # writes exchange rates at runtime, so INSERT must be granted.
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            granted = conn.execute(
                sa.text(
                    "SELECT has_table_privilege('app_tenant', 'currency_exchange_rates', 'INSERT')"
                )
            ).scalar()
            assert granted is True, (
                "app_tenant lost INSERT on currency_exchange_rates (NON_TENANT_WRITE_TABLES member)"
            )
    finally:
        engine.dispose()


def test_app_tenant_is_denied_dml_on_committed_allocation_children():
    """Verify app_tenant cannot mutate committed-allocation evidence tables."""
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            for table in _PLATFORM_ONLY_WRITE_TABLES:
                granted = conn.execute(
                    sa.text("SELECT has_table_privilege('app_tenant', :table, 'INSERT')"),
                    {"table": table},
                ).scalar()
                assert granted is False, (
                    f"app_tenant unexpectedly holds INSERT on {table}; "
                    "committed-allocation child writes must stay platform-only"
                )
    finally:
        engine.dispose()


def test_app_tenant_is_denied_dml_on_tenant_platform_only_write_tables():
    """Verify app_tenant cannot mutate system-owned finance/audit write tables."""
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            for table in _PLATFORM_ONLY_WRITE_TABLES[:3]:
                granted = conn.execute(
                    sa.text("SELECT has_table_privilege('app_tenant', :table, 'INSERT')"),
                    {"table": table},
                ).scalar()
                assert granted is False, (
                    f"app_tenant unexpectedly holds INSERT on {table}; "
                    "system-owned finance/audit writes must stay platform-only"
                )
    finally:
        engine.dispose()


def test_app_platform_has_write_grant_on_committed_allocation_children():
    """Verify app_platform retains INSERT on committed-allocation evidence tables."""
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            for table in _PLATFORM_ONLY_WRITE_TABLES:
                granted = conn.execute(
                    sa.text("SELECT has_table_privilege('app_platform', :table, 'INSERT')"),
                    {"table": table},
                ).scalar()
                assert granted is True, (
                    f"app_platform lost INSERT on {table}; "
                    "commit path needs the privileged child-table write lane"
                )
    finally:
        engine.dispose()


def test_app_tenant_is_denied_dml_on_platform_catalog():
    """Verify app_tenant cannot mutate the platform permissions catalog."""
    # permissions is a platform definition catalog. The narrowed model grants
    # SELECT broadly but NOT DML, so INSERT must NOT be granted. A regression to
    # the old blanket DML grant would flip this to True.
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            insert_granted = conn.execute(
                sa.text("SELECT has_table_privilege('app_tenant', 'permissions', 'INSERT')")
            ).scalar()
            assert insert_granted is False, (
                "app_tenant unexpectedly holds INSERT on the permissions "
                "catalog; DML must be narrowed to NON_TENANT_WRITE_TABLES"
            )
            # Sanity: SELECT on the same catalog IS granted (broad reads).
            select_granted = conn.execute(
                sa.text("SELECT has_table_privilege('app_tenant', 'permissions', 'SELECT')")
            ).scalar()
            assert select_granted is True, "app_tenant lost broad SELECT on the permissions catalog"
    finally:
        engine.dispose()


def test_app_platform_has_write_grant_on_system_owned_write_tables():
    """Verify app_platform retains INSERT on audit/finance system-owned tables."""
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            for table in _PLATFORM_ONLY_WRITE_TABLES[:3]:
                granted = conn.execute(
                    sa.text("SELECT has_table_privilege('app_platform', :table, 'INSERT')"),
                    {"table": table},
                ).scalar()
                assert granted is True, (
                    f"app_platform lost INSERT on {table}; "
                    "system-owned finance/audit writes need the platform lane"
                )
    finally:
        engine.dispose()
