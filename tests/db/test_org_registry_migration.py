from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_org_registry_migration_exists():
    migration = (
        PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions/20260510_0002_org_registry.py"
    )

    assert migration.exists()


def test_org_registry_migration_creates_registry_tables():
    migration = (
        PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions/20260510_0002_org_registry.py"
    ).read_text(encoding="utf-8")

    assert '"org_units"' in migration
    assert '"youtube_channels"' in migration
    assert '"channel_groups"' in migration
    assert '"channel_group_members"' in migration
    assert "revenue_source_status" in migration
