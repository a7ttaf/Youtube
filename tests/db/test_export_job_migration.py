import importlib.util
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions/20260510_0008_export_jobs.py"


def test_export_job_migration_creates_export_jobs_table():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    with engine.begin() as connection:
        _apply_migration(connection)
        inspector = inspect(connection)
        check_constraints = {
            constraint["name"]: constraint["sqltext"]
            for constraint in inspector.get_check_constraints("export_jobs")
        }
        indexes = {index["name"]: tuple(index["column_names"]) for index in inspector.get_indexes("export_jobs")}
        table_names = inspector.get_table_names()

    assert "export_jobs" in table_names
    assert "ck_export_jobs_export_type" in check_constraints
    assert "FINANCE_EXCEL" in check_constraints["ck_export_jobs_export_type"]
    assert "ck_export_jobs_scope" in check_constraints
    assert "scope_id IS NULL" in check_constraints["ck_export_jobs_scope"]
    assert indexes["ix_export_jobs_requested_by_created"] == ("requested_by", "created_at")
    assert indexes["ix_export_jobs_scope_month"] == ("scope_type", "scope_id", "month")


def _apply_migration(connection) -> None:
    spec = importlib.util.spec_from_file_location("export_job_migration", MIGRATION_PATH)
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
