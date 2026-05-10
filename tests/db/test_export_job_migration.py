from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_export_job_migration_creates_export_jobs_table():
    migration = (
        PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions/20260510_0008_export_jobs.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "20260510_0008"' in migration
    assert 'down_revision = "20260510_0007"' in migration
    assert '"export_jobs"' in migration
    assert "ck_export_jobs_export_type" in migration
    assert "ck_export_jobs_scope" in migration
    assert "ix_export_jobs_requested_by_created" in migration
