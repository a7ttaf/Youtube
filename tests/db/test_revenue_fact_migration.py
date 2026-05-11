import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions/20260510_0004_revenue_facts.py"


def test_revenue_fact_migration_creates_monthly_fact_table():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    with engine.begin() as connection:
        _apply_migration(connection)
        inspector = inspect(connection)
        table_names = inspector.get_table_names()
        columns = {column["name"]: column for column in inspector.get_columns("monthly_channel_revenue_facts")}
        foreign_keys = {
            foreign_key["name"]: foreign_key
            for foreign_key in inspector.get_foreign_keys("monthly_channel_revenue_facts")
        }
        unique_constraints = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("monthly_channel_revenue_facts")
        }

    assert "monthly_channel_revenue_facts" in table_names
    assert columns["gross_revenue_usd"]["nullable"] is False
    assert foreign_keys["fk_monthly_channel_revenue_facts_youtube_channel_id"]["referred_table"] == "youtube_channels"
    assert foreign_keys["fk_monthly_channel_revenue_facts_youtube_channel_id"]["constrained_columns"] == [
        "youtube_channel_id"
    ]
    assert unique_constraints["uq_monthly_channel_revenue_source"] == ("month", "youtube_channel_id", "source_kind")


def _apply_migration(connection) -> None:
    spec = importlib.util.spec_from_file_location("revenue_fact_migration", MIGRATION_PATH)
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
