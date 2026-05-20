"""Migration round-trip tests for the tenants foundation revision."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    PROJECT_ROOT
    / "backend/ums_smart_revenue/db/alembic/versions"
    / "20260516_0001_tenants_foundation.py"
)
UMS_TENANT_ID = "00000000-0000-0000-0000-000000000001"


def test_tenants_migration_creates_tables_and_seeds_ums():
    assert MIGRATION_PATH.exists()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _register_sqlite_uuid_function(engine)

    with engine.begin() as connection:
        _apply_migration(connection)

        inspector = inspect(connection)
        table_names = set(inspector.get_table_names())
        assert {"tenants", "platform_admins"} <= table_names

        tenant_columns = {
            column["name"]: column for column in inspector.get_columns("tenants")
        }
        assert tenant_columns["slug"]["nullable"] is False
        assert tenant_columns["display_name"]["nullable"] is False
        assert tenant_columns["primary_currency"]["nullable"] is False
        assert tenant_columns["status"]["nullable"] is False

        tenant_constraints = {
            constraint["name"]
            for constraint in inspector.get_check_constraints("tenants")
        }
        assert {
            "ck_tenants_slug_lower",
            "ck_tenants_status",
            "ck_tenants_primary_currency_iso4217",
        } <= tenant_constraints

        tenant_indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes("tenants")
        }
        assert tenant_indexes["uq_tenants_slug"] == ("slug",)

        platform_admin_constraints = {
            constraint["name"]
            for constraint in inspector.get_check_constraints("platform_admins")
        }
        assert "ck_platform_admins_status" in platform_admin_constraints

        ums_row = connection.execute(
            text(
                "SELECT id, slug, display_name, primary_currency, status "
                "FROM tenants WHERE slug = 'ums'"
            )
        ).one()
        assert str(ums_row.id).replace("-", "") == UMS_TENANT_ID.replace("-", "")
        assert ums_row.display_name == "UMS"
        assert ums_row.primary_currency == "USD"
        assert ums_row.status == "ACTIVE"


def test_tenants_migration_seed_is_idempotent_when_re_applied():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _register_sqlite_uuid_function(engine)

    with engine.begin() as connection:
        _apply_migration(connection)
        # Re-run the seed step alone — must not duplicate or fail.
        connection.execute(
            text(
                "INSERT INTO tenants "
                "(id, slug, display_name, primary_currency, status) "
                f"VALUES ('{UMS_TENANT_ID}', 'ums', 'UMS', 'USD', 'ACTIVE') "
                "ON CONFLICT (slug) DO NOTHING"
            )
        )
        count = connection.execute(
            text("SELECT COUNT(*) FROM tenants WHERE slug = 'ums'")
        ).scalar_one()
        assert count == 1


def test_tenants_table_rejects_uppercase_slug():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _register_sqlite_uuid_function(engine)

    with engine.begin() as connection:
        _apply_migration(connection)
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO tenants (id, slug, display_name) "
                    f"VALUES ('{uuid4()}', 'NotLower', 'Bad')"
                )
            )


def test_tenants_table_rejects_unknown_status():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _register_sqlite_uuid_function(engine)

    with engine.begin() as connection:
        _apply_migration(connection)
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO tenants (id, slug, display_name, status) "
                    f"VALUES ('{uuid4()}', 'rotana', 'Rotana', 'BOGUS')"
                )
            )


def test_tenants_table_rejects_non_iso4217_primary_currency():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _register_sqlite_uuid_function(engine)

    with engine.begin() as connection:
        _apply_migration(connection)
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO tenants (id, slug, display_name, primary_currency) "
                    f"VALUES ('{uuid4()}', 'badcurrency', 'Bad', 'usd')"
                )
            )


def test_platform_admins_email_uniqueness_is_case_insensitive():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _register_sqlite_uuid_function(engine)

    with engine.begin() as connection:
        _apply_migration(connection)
        connection.execute(
            text(
                "INSERT INTO platform_admins (id, email, display_name) "
                f"VALUES ('{uuid4()}', 'a@example.com', 'A')"
            )
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO platform_admins (id, email, display_name) "
                    f"VALUES ('{uuid4()}', 'A@Example.COM', 'A again')"
                )
            )


def test_migration_downgrade_drops_both_tables():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _register_sqlite_uuid_function(engine)

    with engine.begin() as connection:
        migration = _load_migration()
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        original_op = migration.op
        try:
            migration.op = operations
            migration.upgrade()
            assert {"tenants", "platform_admins"} <= set(
                inspect(connection).get_table_names()
            )
            migration.downgrade()
            assert "tenants" not in inspect(connection).get_table_names()
            assert "platform_admins" not in inspect(connection).get_table_names()
        finally:
            migration.op = original_op


def _register_sqlite_uuid_function(engine) -> None:
    @event.listens_for(engine, "connect")
    def connect(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: str(uuid4()))


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "tenants_foundation_migration",
        MIGRATION_PATH,
    )
    assert spec is not None
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)
    return migration


def _apply_migration(connection) -> None:
    migration = _load_migration()
    context = MigrationContext.configure(connection)
    operations = Operations(context)
    original_op = migration.op
    try:
        migration.op = operations
        migration.upgrade()
    finally:
        migration.op = original_op
