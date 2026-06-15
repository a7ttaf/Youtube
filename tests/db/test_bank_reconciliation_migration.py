import importlib.util
from pathlib import Path
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, event, inspect, text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    PROJECT_ROOT
    / "backend/ums_smart_revenue/db/alembic/versions/"
    / "20260513_0001_bank_reconciliation.py"
)


def test_bank_reconciliation_migration_creates_bank_reconciliation_table():
    assert MIGRATION_PATH.exists()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _register_sqlite_uuid_function(engine)

    with engine.begin() as connection:
        _apply_migration(connection)
        generated_id = connection.execute(
            text(
                """
                INSERT INTO bank_reconciliation_entries (
                    month,
                    bank_reference,
                    bank_received_date,
                    bank_received_amount,
                    bank_received_currency,
                    bank_received_amount_usd,
                    recorded_by
                )
                VALUES (
                    '2026-03',
                    'WIRE-2026-03',
                    '2026-04-22',
                    930.00,
                    'USD',
                    930.00,
                    '00000000-0000-0000-0000-000000009901'
                )
                RETURNING id
                """
            )
        ).scalar_one()
        inspector = inspect(connection)
        table_names = inspector.get_table_names()
        columns = {
            column["name"]: column
            for column in inspector.get_columns("bank_reconciliation_entries")
        }
        check_constraints = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints("bank_reconciliation_entries")
        }
        indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes("bank_reconciliation_entries")
        }
        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("bank_reconciliation_entries")
        }

    assert "bank_reconciliation_entries" in table_names
    assert columns["bank_received_amount_usd"]["nullable"] is False
    assert columns["recorded_by"]["nullable"] is False
    assert "ck_bank_reconciliation_month_format" in check_constraints
    assert "ck_bank_reconciliation_received_nonnegative" in check_constraints
    assert unique_constraints["uq_bank_reconciliation_month_reference"] == (
        "month",
        "bank_reference",
    )
    assert indexes["ix_bank_reconciliation_entries_month"] == ("month",)
    assert generated_id


def _register_sqlite_uuid_function(engine) -> None:
    @event.listens_for(engine, "connect")
    def connect(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: str(uuid4()))


def _apply_migration(connection) -> None:
    spec = importlib.util.spec_from_file_location(
        "bank_reconciliation_migration",
        MIGRATION_PATH,
    )
    assert spec is not None
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)

    context = MigrationContext.configure(connection)
    operations = Operations(context)
    original_op = migration.op
    try:
        migration.op = operations
        migration.upgrade()
    finally:
        migration.op = original_op
