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
    / "20260513_0004_currency_exchange_rates.py"
)


def test_currency_exchange_rate_migration_creates_rate_table():
    assert MIGRATION_PATH.exists()
    engine = create_engine("sqlite+pysqlite:///:memory:")
    _register_sqlite_uuid_function(engine)

    with engine.begin() as connection:
        _apply_migration(connection)
        generated_id = connection.execute(
            text(
                """
                INSERT INTO currency_exchange_rates (
                    rate_date,
                    base_currency,
                    quote_currency,
                    rate,
                    provider_key,
                    raw_payload
                )
                VALUES (
                    '2026-04-22',
                    'EUR',
                    'USD',
                    1.0845,
                    'ecb',
                    '{}'
                )
                RETURNING id
                """
            )
        ).scalar_one()
        inspector = inspect(connection)
        table_names = inspector.get_table_names()
        columns = {
            column["name"]: column for column in inspector.get_columns("currency_exchange_rates")
        }
        check_constraints = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints("currency_exchange_rates")
        }
        indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes("currency_exchange_rates")
        }
        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("currency_exchange_rates")
        }

    assert "currency_exchange_rates" in table_names
    assert columns["rate"]["nullable"] is False
    assert columns["raw_payload"]["nullable"] is False
    assert "ck_currency_exchange_rates_rate_positive" in check_constraints
    assert "ck_currency_exchange_rates_currency_pair" in check_constraints
    assert unique_constraints["uq_currency_exchange_rate_provider_date_pair"] == (
        "rate_date",
        "base_currency",
        "quote_currency",
        "provider_key",
    )
    assert indexes["ix_currency_exchange_rates_pair_date"] == (
        "base_currency",
        "quote_currency",
        "rate_date",
    )
    assert generated_id


def _register_sqlite_uuid_function(engine) -> None:
    @event.listens_for(engine, "connect")
    def connect(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: str(uuid4()))


def _apply_migration(connection) -> None:
    spec = importlib.util.spec_from_file_location(
        "currency_exchange_rate_migration",
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
