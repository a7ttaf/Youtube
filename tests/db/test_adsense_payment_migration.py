import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    PROJECT_ROOT
    / "backend/ums_smart_revenue/db/alembic/versions/20260512_0002_adsense_payments.py"
)


def test_adsense_payment_migration_creates_payment_table():
    assert MIGRATION_PATH.exists()
    engine = create_engine("sqlite+pysqlite:///:memory:")

    with engine.begin() as connection:
        _apply_migration(connection)
        inspector = inspect(connection)
        table_names = inspector.get_table_names()
        columns = {
            column["name"]: column
            for column in inspector.get_columns("adsense_payments")
        }
        check_constraints = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints("adsense_payments")
        }
        indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes("adsense_payments")
        }
        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("adsense_payments")
        }

    assert "adsense_payments" in table_names
    assert columns["payment_amount"]["nullable"] is False
    assert columns["raw_payload"]["nullable"] is False
    assert "ck_adsense_payments_month_format" in check_constraints
    assert "ck_adsense_payments_payment_status" in check_constraints
    assert unique_constraints["uq_adsense_payments_month_name"] == (
        "month",
        "payment_name",
    )
    assert indexes["ix_adsense_payments_month_date"] == ("month", "payment_date")


def _apply_migration(connection) -> None:
    spec = importlib.util.spec_from_file_location(
        "adsense_payment_migration",
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
