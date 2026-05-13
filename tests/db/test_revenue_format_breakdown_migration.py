from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = (
    "backend/ums_smart_revenue/db/alembic/versions/"
    "20260513_0005_revenue_format_breakdown.py"
)


def test_revenue_format_breakdown_migration_adds_official_component_columns():
    migration = (PROJECT_ROOT / MIGRATION_PATH).read_text(encoding="utf-8")

    assert 'revision = "20260513_0005"' in migration
    assert 'down_revision = "20260513_0004"' in migration
    assert '"shorts_revenue_usd"' in migration
    assert '"longform_revenue_usd"' in migration
    assert '"subscription_revenue_usd"' in migration
    assert "ck_monthly_channel_revenue_facts_shorts_nonnegative" in migration
    assert "ck_monthly_channel_revenue_facts_longform_nonnegative" in migration
    assert "ck_monthly_channel_revenue_facts_subscription_nonnegative" in migration
    assert "ck_monthly_channel_revenue_facts_format_total" in migration
