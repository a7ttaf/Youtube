from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_alembic_scaffold_exists_for_security_foundation():
    assert (PROJECT_ROOT / "alembic.ini").exists()
    assert (PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/env.py").exists()
    assert (PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions/20260510_0001_security_foundation.py").exists()


def test_initial_migration_creates_security_tables():
    migration = (
        PROJECT_ROOT / "backend/ums_smart_revenue/db/alembic/versions/20260510_0001_security_foundation.py"
    ).read_text(encoding="utf-8")

    assert '"users"' in migration
    assert '"roles"' in migration
    assert '"permissions"' in migration
    assert '"audit_logs"' in migration
    assert '"api_connector_credentials"' in migration
