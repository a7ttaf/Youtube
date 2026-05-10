import tomllib
from pathlib import Path

from ums_smart_revenue.config.version_baseline import STACK_VERSION_BASELINE


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_backend_dependencies_are_pinned_to_checked_latest_stable_versions():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(pyproject["project"]["dependencies"])
    test_dependencies = set(pyproject["project"]["optional-dependencies"]["test"])

    assert pyproject["project"]["requires-python"] == ">=3.14,<3.15"
    assert "fastapi==0.136.1" in dependencies
    assert "pydantic==2.13.4" in dependencies
    assert "uvicorn[standard]==0.46.0" in dependencies
    assert "SQLAlchemy==2.0.49" in dependencies
    assert "alembic==1.18.4" in dependencies
    assert "asyncpg==0.31.0" in dependencies
    assert "neo4j==6.2.0" in dependencies
    assert "celery==5.6.3" in dependencies
    assert "redis==7.4.0" in dependencies
    assert "pytest==9.0.3" in test_dependencies
    assert "httpx==0.28.1" in test_dependencies


def test_stack_version_baseline_records_runtime_and_frontend_targets():
    assert STACK_VERSION_BASELINE["runtime"]["python"] == "3.14.4"
    assert STACK_VERSION_BASELINE["runtime"]["node_lts"] == "24.15.0"
    assert STACK_VERSION_BASELINE["datastores"]["postgresql"] == "18.3"
    assert STACK_VERSION_BASELINE["datastores"]["neo4j_enterprise"] == "2026.04.0"
    assert STACK_VERSION_BASELINE["frontend"]["next"] == "16.2.6"
    assert STACK_VERSION_BASELINE["frontend"]["react"] == "19.2.6"

