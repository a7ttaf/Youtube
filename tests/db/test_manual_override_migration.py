from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_manual_override_migration_creates_revenue_override_table():
    migration = (
        PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions/20260510_0005_manual_overrides.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "20260510_0005"' in migration
    assert 'down_revision = "20260510_0004"' in migration
    assert '"revenue_manual_overrides"' in migration
    assert '"adjustment_revenue_usd"' in migration
    assert "fk_revenue_manual_overrides_youtube_channel_id" in migration
    assert "month ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'" in migration
    assert "ck_revenue_manual_overrides_status" in migration
