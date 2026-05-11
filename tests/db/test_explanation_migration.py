from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_explanation_migration_creates_number_explanations_table():
    migration = (
        PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions/20260510_0007_number_explanations.py"
    ).read_text(encoding="utf-8")

    assert 'revision = "20260510_0007"' in migration
    assert 'down_revision = "20260510_0006"' in migration
    assert '"number_explanations"' in migration
    assert '"components"' in migration
    assert '"warnings"' in migration
    assert "uq_number_explanations_entity_metric_month" in migration
