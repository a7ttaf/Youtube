from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_manual_override_migration_creates_revenue_override_table():
    migration = (
        PROJECT_ROOT
        / "backend/ums_smart_revenue/db/alembic/versions"
        / "20260510_0005_manual_overrides.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "20260510_0005"' in migration
    assert 'down_revision = "20260510_0004"' in migration
    assert '"revenue_manual_overrides"' in migration
    assert '"adjustment_revenue_usd"' in migration
    assert "fk_revenue_manual_overrides_youtube_channel_id" in migration
    # The month-format CHECK was rewritten from the PostgreSQL-only `~` regex
    # to a dialect-agnostic length/substr form so SQLite-backed migration
    # harnesses can execute upgrade() without a syntax error. Positions 6 and
    # 7 each get an explicit digit check ahead of the range check because
    # lexicographic BETWEEN would otherwise admit values like "2026-0A".
    assert "ck_revenue_manual_overrides_month_format" in migration
    assert "substr(month, 6, 1) BETWEEN '0' AND '9'" in migration
    assert "substr(month, 7, 1) BETWEEN '0' AND '9'" in migration
    assert "substr(month, 6, 2) BETWEEN '01' AND '12'" in migration
    assert "approval_reason IS NOT NULL" in migration
    assert "approval_reason IS NULL" in migration
    assert "ck_revenue_manual_overrides_status" in migration
