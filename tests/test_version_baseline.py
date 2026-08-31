# ============================================================================
# Purpose: Assert pyproject/uv.lock pins match the checked version baseline.
# Database/ORM: None.
# Standards: Reads pyproject.toml and STACK_VERSION_BASELINE via imports.
# Blast Radius: Version drift detection tests only.
# Connections: expected pins and the documented baseline.
#   - File: backend/ums_smart_revenue/config/version_baseline.py -> Expected pins.
#   - File: Docs/implementation/TECH_VERSION_BASELINE.md -> Documented baseline.
# ============================================================================
import tomllib
from pathlib import Path

from ums_smart_revenue.config.version_baseline import STACK_VERSION_BASELINE

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_backend_dependencies_are_pinned_to_checked_latest_stable_versions():
    """Verify the dependency pins, lockfile, and optional extras stay aligned."""
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lockfile = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    dependencies = set(pyproject["project"]["dependencies"])
    test_dependencies = set(pyproject["project"]["optional-dependencies"]["test"])
    lint_dependencies = set(pyproject["project"]["optional-dependencies"]["lint"])
    dev_dependencies = set(pyproject["project"]["optional-dependencies"]["dev"])
    locked_versions = {package["name"]: package["version"] for package in lockfile["package"]}
    expected_dependencies = {
        "fastapi==0.141.1",
        "pydantic==2.13.5",
        "uvicorn[standard]==0.52.4",
        "sqlalchemy==2.0.52",
        "alembic==1.19.1",
        "psycopg[binary]==3.3.4",
        # httpx2 is the successor of httpx (starlette 1.3+ deprecates the old
        # package for its testclient); migrated 2026-08-21 (backlog item 2/3).
        "httpx2==2.12.0",
        "celery==5.6.3",
        "redis==8.1.0",
        "openpyxl==3.1.5",
        "reportlab==5.0.1",
        "python-pptx==1.0.2",
        "google-cloud-secret-manager==2.30.0",
        "google-cloud-storage==3.13.1",
        # Required by FastAPI to accept multipart/form-data. POST /channels/import
        # takes the channel roster as a CSV upload, and FastAPI raises at
        # route-registration time (not request time) without this installed.
        "python-multipart==0.0.32",
    }
    expected_test_dependencies = {
        "pytest==9.1.1",
        "httpx2==2.12.0",
        "pypdf==6.16.2",
    }
    expected_lint_dependencies = {
        "mypy==2.3.1",
        "ruff==0.16.5",
    }
    expected_dev_dependencies = {
        "httpx2==2.12.0",
        "mypy==2.3.1",
        "pypdf==6.16.2",
        "pytest==9.1.1",
        "ruff==0.16.5",
    }

    assert pyproject["project"]["requires-python"] == ">=3.14,<3.15"
    assert dependencies == expected_dependencies
    assert test_dependencies == expected_test_dependencies
    assert lint_dependencies == expected_lint_dependencies
    assert dev_dependencies == expected_dev_dependencies
    assert locked_versions["pydantic"] == "2.13.5"
    assert locked_versions["pydantic-core"] == "2.46.5"
    assert locked_versions["pypdf"] == "6.16.2"
    assert locked_versions["ruff"] == "0.16.5"


def test_stack_version_baseline_records_runtime_and_frontend_targets():
    """Verify the machine-readable stack baseline reports current targets."""
    assert STACK_VERSION_BASELINE["runtime"]["python"] == "3.14.5"
    assert STACK_VERSION_BASELINE["runtime"]["node_lts"] == "24.15.0"
    assert STACK_VERSION_BASELINE["backend"]["fastapi"] == "0.141.1"
    assert STACK_VERSION_BASELINE["backend"]["pydantic"] == "2.13.5"
    assert STACK_VERSION_BASELINE["backend"]["sqlalchemy"] == "2.0.52"
    assert STACK_VERSION_BASELINE["backend"]["alembic"] == "1.19.1"
    assert STACK_VERSION_BASELINE["backend"]["psycopg"] == "3.3.4"
    assert STACK_VERSION_BASELINE["backend"]["openpyxl"] == "3.1.5"
    assert STACK_VERSION_BASELINE["backend"]["reportlab"] == "5.0.1"
    assert STACK_VERSION_BASELINE["backend"]["python_pptx"] == "1.0.2"
    assert STACK_VERSION_BASELINE["backend"]["google_cloud_secret_manager"] == "2.30.0"
    assert STACK_VERSION_BASELINE["backend"]["google_cloud_storage"] == "3.13.1"
    assert STACK_VERSION_BASELINE["backend"]["pypdf"] == "6.16.2"
    assert STACK_VERSION_BASELINE["backend"]["ruff"] == "0.16.5"
    assert STACK_VERSION_BASELINE["datastores"]["postgresql"] == "18.3"
    assert STACK_VERSION_BASELINE["frontend"]["next"] == "16.2.6"
    assert STACK_VERSION_BASELINE["frontend"]["react"] == "19.2.6"
