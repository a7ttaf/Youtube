import tomllib
from pathlib import Path

from ums_smart_revenue.config.version_baseline import STACK_VERSION_BASELINE

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_backend_dependencies_are_pinned_to_checked_latest_stable_versions():
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = set(pyproject["project"]["dependencies"])
    test_dependencies = set(pyproject["project"]["optional-dependencies"]["test"])
    expected_dependencies = {
        "fastapi==0.136.3",
        "pydantic==2.13.4",
        "uvicorn[standard]==0.48.0",
        "SQLAlchemy==2.0.50",
        "alembic==1.18.4",
        "psycopg[binary]==3.3.4",
        "celery==5.6.3",
        "redis==7.4.0",
        "openpyxl==3.1.5",
        "reportlab==4.5.1",
        "python-pptx==1.0.2",
        "google-cloud-secret-manager==2.28.0",
    }
    expected_test_dependencies = {
        "pytest==9.0.3",
        "httpx==0.28.1",
        "pypdf==6.12.1",
    }

    assert pyproject["project"]["requires-python"] == ">=3.14,<3.15"
    assert dependencies == expected_dependencies
    assert test_dependencies == expected_test_dependencies


def test_stack_version_baseline_records_runtime_and_frontend_targets():
    assert STACK_VERSION_BASELINE["runtime"]["python"] == "3.14.5"
    assert STACK_VERSION_BASELINE["runtime"]["node_lts"] == "24.15.0"
    assert STACK_VERSION_BASELINE["backend"]["fastapi"] == "0.136.3"
    assert STACK_VERSION_BASELINE["backend"]["pydantic"] == "2.13.4"
    assert STACK_VERSION_BASELINE["backend"]["sqlalchemy"] == "2.0.50"
    assert STACK_VERSION_BASELINE["backend"]["alembic"] == "1.18.4"
    assert STACK_VERSION_BASELINE["backend"]["psycopg"] == "3.3.4"
    assert STACK_VERSION_BASELINE["backend"]["openpyxl"] == "3.1.5"
    assert STACK_VERSION_BASELINE["backend"]["reportlab"] == "4.5.1"
    assert STACK_VERSION_BASELINE["backend"]["python_pptx"] == "1.0.2"
    assert STACK_VERSION_BASELINE["backend"]["google_cloud_secret_manager"] == "2.28.0"
    assert STACK_VERSION_BASELINE["backend"]["pypdf"] == "6.12.1"
    assert STACK_VERSION_BASELINE["datastores"]["postgresql"] == "18.3"
    assert STACK_VERSION_BASELINE["frontend"]["next"] == "16.2.6"
    assert STACK_VERSION_BASELINE["frontend"]["react"] == "19.2.6"
