from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_retire_graph_migration_removes_permissions_and_graph_scope():
    migration_path = (
        "backend/ums_smart_revenue/db/alembic/versions/20260513_0002_retire_graph_permissions.py"
    )
    migration = (PROJECT_ROOT / migration_path).read_text(encoding="utf-8")

    assert "DELETE FROM user_role_assignments" in migration
    assert "DELETE FROM user_permission_grants" in migration
    assert "DELETE FROM role_permission_assignments" in migration
    assert "DELETE FROM permissions WHERE key IN ('graph.view', 'graph.view_finance')" in migration
    assert "DELETE FROM access_scopes WHERE scope_type = 'graph-read'" in migration
    assert "'finance-month', 'export', 'connector')" in migration
    assert "Irreversible migration" in migration
    assert "graph-read scopes and grants" in migration
    assert "INSERT INTO permissions" not in migration
