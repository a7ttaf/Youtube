from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_revenue_fact_migration_creates_monthly_fact_table():
    migration = (
        PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions/20260510_0004_revenue_facts.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "20260510_0004"' in migration
    assert 'down_revision = "20260510_0003"' in migration
    assert '"monthly_channel_revenue_facts"' in migration
    assert '"gross_revenue_usd"' in migration
    assert "fk_monthly_channel_revenue_facts_youtube_channel_id" in migration
    assert '"uq_monthly_channel_revenue_source"' in migration
