from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    "backend/ums_smart_revenue/db/alembic/versions/"
    "20260513_0006_export_job_scope_channel_snapshot.py"
)


def test_export_scope_channel_snapshot_migration_adds_dialect_neutral_json_column():
    migration = (PROJECT_ROOT / MIGRATION_PATH).read_text(encoding="utf-8")

    assert 'revision = "20260513_0006"' in migration
    assert 'down_revision = "20260513_0005"' in migration
    assert '"scope_channel_ids"' in migration
    assert 'sa.JSON().with_variant(postgresql.JSONB(), "postgresql")' in migration
    assert "nullable=True" in migration
    assert "ck_export_jobs_scope_channel_ids_is_array" in migration
    assert "ix_export_jobs_scope_channel_ids" in migration
    assert "jsonb_typeof" in migration
    assert "jsonb_array_elements" in migration
    assert "jsonb_typeof(elem) <> 'string'" in migration
    assert "jsonb_array_length(scope_channel_ids) > 0" in migration
