# ============================================================================
# Purpose: Validate the export-template Alembic migration contract.
# Database/ORM: export_templates and export_jobs schema in SQLite plus recorded
# PostgreSQL RLS statements.
# Standards: Migration-level assertions without weakening production DDL.
# Blast Radius: Export template schema, nullable job reference, and RLS setup.
# Connections:
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260620_0001_export_templates.py -> Migration under test.
#   - File: backend/ums_smart_revenue/db/rls.py -> Expected policy constants.
# ============================================================================
import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions/20260620_0001_export_templates.py"
)


def test_export_template_migration_creates_template_table_and_job_reference():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    with engine.begin() as connection:
        _create_prior_export_jobs_table(connection)
        _apply_migration(connection)
        inspector = inspect(connection)
        template_columns = {
            column["name"]: column for column in inspector.get_columns("export_templates")
        }
        template_indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes("export_templates")
        }
        template_uniques = {
            constraint["name"]: tuple(constraint["column_names"])
            for constraint in inspector.get_unique_constraints("export_templates")
        }
        job_columns = {column["name"] for column in inspector.get_columns("export_jobs")}
        job_indexes = {
            index["name"]: tuple(index["column_names"])
            for index in inspector.get_indexes("export_jobs")
        }
        job_foreign_keys = inspector.get_foreign_keys("export_jobs")
        table_names = inspector.get_table_names()

    assert "export_templates" in table_names
    assert template_columns["id"]["nullable"] is False
    assert template_columns["layout_config"]["nullable"] is False
    assert template_columns["is_active"]["nullable"] is False
    assert template_uniques["uq_export_templates_tenant_id_name"] == ("tenant_id", "name")
    # Composite (tenant_id, id) unique key is the explicit target of the
    # export_jobs (tenant_id, template_id) tenant-scoped foreign key.
    assert template_uniques["uq_export_templates_tenant_id_id"] == ("tenant_id", "id")
    assert template_indexes["ix_export_templates_tenant_type_active"] == (
        "tenant_id",
        "export_type",
        "is_active",
    )
    assert template_indexes["ix_export_templates_tenant_created"] == ("tenant_id", "created_at")
    assert "template_id" in job_columns
    assert job_indexes["ix_export_jobs_template_id"] == ("template_id",)
    # The export_jobs -> export_templates foreign key is tenant-scoped
    # (tenant_id, template_id) -> (tenant_id, id) so a template reference can
    # never cross tenants at the DB level.
    assert any(
        foreign_key["referred_table"] == "export_templates"
        and tuple(foreign_key["constrained_columns"]) == ("tenant_id", "template_id")
        and tuple(foreign_key["referred_columns"]) == ("tenant_id", "id")
        for foreign_key in job_foreign_keys
    )


def test_export_template_migration_configures_postgres_rls():
    migration = _load_migration()
    bind = _RecordingBind()

    migration._configure_export_templates_rls(bind)

    statements = "\n".join(bind.statements)
    assert 'ALTER TABLE public."export_templates" ENABLE ROW LEVEL SECURITY' in statements
    assert (
        'CREATE POLICY export_templates_tenant_isolation ON public."export_templates"' in statements
    )
    assert "tenant_id = app_current_tenant_id()" in statements
    assert 'ALTER TABLE public."export_templates" FORCE ROW LEVEL SECURITY' in statements
    assert 'TO "app_tenant"' in statements
    assert 'TO "app_platform"' in statements


def _create_prior_export_jobs_table(connection) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "export_jobs",
        metadata,
        sa.Column("id", sa.Uuid(as_uuid=True), primary_key=True),
        # tenant_id is part of the export_jobs schema added in migration
        # 20260517_0001; it must pre-exist here because the export-template
        # migration adds a composite (tenant_id, template_id) -> export_templates
        # foreign key that references it.
        sa.Column("tenant_id", sa.Uuid(as_uuid=True), nullable=False),
    )
    metadata.create_all(connection)


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


def _load_migration():
    spec = importlib.util.spec_from_file_location("export_template_migration", MIGRATION_PATH)
    assert spec is not None
    migration = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(migration)
    return migration


class _RecordingDialect:
    name = "postgresql"


class _RecordingBind:
    dialect = _RecordingDialect()

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement) -> None:
        self.statements.append(str(statement))
