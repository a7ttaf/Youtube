from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    "backend/ums_smart_revenue/db/alembic/versions/"
    "20260513_0003_export_artifact_metadata.py"
)


def test_export_artifact_migration_adds_generated_artifact_metadata():
    migration = (
        PROJECT_ROOT
        / MIGRATION_PATH
    ).read_text(encoding="utf-8")

    assert 'revision = "20260513_0003"' in migration
    assert 'down_revision = "20260513_0002"' in migration
    assert '"artifact_filename"' in migration
    assert '"artifact_content_type"' in migration
    assert '"artifact_byte_size"' in migration
    assert '"artifact_checksum_sha256"' in migration
    assert '"failure_reason"' in migration
    assert "ck_export_jobs_artifact_byte_size" in migration
    assert "ck_export_jobs_artifact_checksum_sha256" in migration
