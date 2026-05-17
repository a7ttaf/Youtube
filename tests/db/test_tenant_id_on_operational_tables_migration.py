"""Migration round-trip tests for ``20260517_0001_tenant_id_on_operational_tables``.

The migration adds a ``tenant_id`` column (NOT NULL, default UMS, FK to
tenants, indexed) to every tenant-scoped operational table.

Rather than chain the 13 ancestor migrations (some of which use PG-only
features like ``CREATE EXTENSION`` and ``JSONB`` that SQLite cannot
render), we materialise a *minimal* pre-S2.4a schema in-line: the
``tenants`` table with its one row, plus an empty stub for each of the
18 operational tables. The migration only does ``ALTER TABLE ADD COLUMN``
+ ``ADD CONSTRAINT`` + ``CREATE INDEX``, so the minimal stub is enough
to exercise every code path. End-to-end migration integrity against
the full chain stays the responsibility of CI's Postgres job once the
mirror workflow re-enables.
"""

import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, text


PROJECT_ROOT = Path(__file__).resolve().parents[2]
VERSIONS_DIR = PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions"
TARGET_MIGRATION = "20260517_0001_tenant_id_on_operational_tables"
UMS_TENANT_ID = "00000000-0000-0000-0000-000000000001"


# Source-of-truth list of tables the target migration should touch.
# Duplicated from the migration module so accidental drift between the
# two raises an assertion at test time rather than silently corrupting
# a real schema.
EXPECTED_TABLES: tuple[str, ...] = (
    "users",
    "access_scopes",
    "user_role_assignments",
    "user_permission_grants",
    "audit_logs",
    "api_connector_credentials",
    "org_units",
    "youtube_channels",
    "channel_groups",
    "channel_group_members",
    "finance_month_close",
    "monthly_channel_revenue_facts",
    "revenue_manual_overrides",
    "adsense_payments",
    "bank_reconciliation_entries",
    "raw_report_files",
    "number_explanations",
    "export_jobs",
)


def test_target_migration_module_lists_same_tables_as_test_fixture():
    """Guard against drift between the migration's table list and ours."""
    target = _load_migration(TARGET_MIGRATION)

    assert tuple(target.TENANT_SCOPED_TABLES) == EXPECTED_TABLES


def test_migration_adds_tenant_id_to_every_operational_table():
    engine = _build_engine()
    with engine.begin() as connection:
        _setup_minimal_pre_state(connection)
        _execute_migration(connection, _load_migration(TARGET_MIGRATION), "upgrade")

        inspector = inspect(connection)
        for table in EXPECTED_TABLES:
            columns = {c["name"]: c for c in inspector.get_columns(table)}
            assert "tenant_id" in columns, f"{table} missing tenant_id column"
            assert columns["tenant_id"]["nullable"] is False, (
                f"{table}.tenant_id should be NOT NULL"
            )

            index_names = {i["name"] for i in inspector.get_indexes(table)}
            assert f"ix_{table}_tenant_id" in index_names, (
                f"{table} missing ix_{table}_tenant_id index"
            )

            fk_names = {fk.get("name") for fk in inspector.get_foreign_keys(table)}
            assert f"fk_{table}_tenant_id" in fk_names, (
                f"{table} missing fk_{table}_tenant_id foreign key"
            )


def test_existing_rows_get_ums_tenant_id_via_default():
    """A row inserted *before* the migration should backfill to UMS."""
    engine = _build_engine()
    with engine.begin() as connection:
        _setup_minimal_pre_state(connection)
        # Pre-existing row with no tenant_id.
        connection.execute(text("INSERT INTO users (id) VALUES ('pre')"))

        _execute_migration(connection, _load_migration(TARGET_MIGRATION), "upgrade")

        row = connection.execute(
            text("SELECT tenant_id FROM users WHERE id = 'pre'")
        ).one()

    assert _strip_uuid(str(row.tenant_id)) == _strip_uuid(UMS_TENANT_ID)


def test_new_inserts_without_tenant_id_get_ums_default():
    """After the migration, an INSERT that omits tenant_id should default."""
    engine = _build_engine()
    with engine.begin() as connection:
        _setup_minimal_pre_state(connection)
        _execute_migration(connection, _load_migration(TARGET_MIGRATION), "upgrade")

        connection.execute(text("INSERT INTO users (id) VALUES ('post')"))
        row = connection.execute(
            text("SELECT tenant_id FROM users WHERE id = 'post'")
        ).one()

    assert _strip_uuid(str(row.tenant_id)) == _strip_uuid(UMS_TENANT_ID)


def test_downgrade_removes_tenant_id_from_every_table():
    engine = _build_engine()
    with engine.begin() as connection:
        _setup_minimal_pre_state(connection)
        target = _load_migration(TARGET_MIGRATION)

        _execute_migration(connection, target, "upgrade")
        _execute_migration(connection, target, "downgrade")

        inspector = inspect(connection)
        for table in EXPECTED_TABLES:
            columns = {c["name"] for c in inspector.get_columns(table)}
            assert "tenant_id" not in columns, (
                f"{table} still has tenant_id after downgrade"
            )


# ---------------------------------------------------------------------------
# Plumbing
# ---------------------------------------------------------------------------


def _setup_minimal_pre_state(connection) -> None:
    """Create the bare minimum schema the target migration needs.

    Just enough so ``ALTER TABLE … ADD COLUMN tenant_id`` plus the FK
    to ``tenants(id)`` will succeed:

    * ``tenants`` table with the UMS row at the deterministic UUID.
    * 18 empty operational tables, each with a single ``id`` column so
      ``op.batch_alter_table`` has something to alter.
    """
    connection.execute(text("CREATE TABLE tenants (id TEXT PRIMARY KEY)"))
    connection.execute(
        text(f"INSERT INTO tenants (id) VALUES ('{UMS_TENANT_ID}')")
    )
    for table in EXPECTED_TABLES:
        connection.execute(text(f"CREATE TABLE {table} (id TEXT)"))


def _build_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_uuid(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function(
            "gen_random_uuid", 0, lambda: str(uuid4())
        )

    return engine


def _load_migration(name: str):
    path = VERSIONS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _execute_migration(connection, migration, action: str) -> None:
    context = MigrationContext.configure(connection)
    operations = Operations(context)
    original_op = migration.op
    try:
        migration.op = operations
        getattr(migration, action)()
    finally:
        migration.op = original_op


def _strip_uuid(value: str) -> str:
    return value.replace("-", "")
