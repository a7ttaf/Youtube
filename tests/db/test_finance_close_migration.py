from pathlib import Path


def test_finance_close_migration_creates_month_close_table():
    migration = Path(
        "backend/ums_smart_revenue/db/alembic/versions/20260510_0003_finance_close.py"
    ).read_text()

    assert 'revision = "20260510_0003"' in migration
    assert 'down_revision = "20260510_0002"' in migration
    assert '"finance_month_close"' in migration
    assert '"allocation_rule_payload"' in migration
