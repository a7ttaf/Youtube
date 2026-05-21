from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_finance_close_migration_creates_month_close_table():
    migration = (
        PROJECT_ROOT
        / "backend/ums_smart_revenue/db/alembic/versions/20260510_0003_finance_close.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "20260510_0003"' in migration
    assert 'down_revision = "20260510_0002"' in migration
    assert '"finance_month_close"' in migration
    assert '"allocation_rule_payload"' in migration
    assert "substr(month, 6, 2) BETWEEN '01' AND '12'" in migration
