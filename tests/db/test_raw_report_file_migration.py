from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_raw_report_file_migration_creates_metadata_table():
    migration = (
        PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions/20260510_0006_raw_report_files.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "20260510_0006"' in migration
    assert 'down_revision = "20260510_0005"' in migration
    assert '"raw_report_files"' in migration
    assert '"file_url"' in migration
    assert "ck_raw_report_files_parse_status" in migration
