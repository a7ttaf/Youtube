# skipcq: PYL-R0401 -- DeepSource attributes pre-existing backend import cycles
# (api.allocation/channels/revenue; finance.month_close/month_close_readiness/
# reconciliation/revenue_facts) to this top-level module via whole-package import
# analysis. The cycles are not introduced here and resolve at runtime; they are
# tracked for a dedicated backend decoupling refactor (see PR #104 report).
import tomllib
from pathlib import Path

from ums_smart_revenue.config.version_baseline import STACK_VERSION_BASELINE

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_backend_dependencies_are_pinned_to_checked_latest_stable_versions():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = set(pyproject["project"]["dependencies"])
    test_dependencies = set(pyproject["project"]["optional-dependencies"]["test"])
    expected_dependencies = {
        "fastapi==0.141.1",
        "pydantic==2.13.4",
        "uvicorn[standard]==0.52.0",
        "sqlalchemy==2.0.51",
        "alembic==1.18.5",
        "psycopg[binary]==3.3.4",
        "httpx==0.28.1",
        "celery==5.6.3",
        "redis==8.1.0",
        "openpyxl==3.1.5",
        "reportlab==5.0.0",
        "python-pptx==1.0.2",
        "google-cloud-secret-manager==2.30.0",
        "google-cloud-storage==3.13.0",
        # Required by FastAPI to accept multipart/form-data. POST /channels/import
        # takes the channel roster as a CSV upload, and FastAPI raises at
        # route-registration time (not request time) without this installed.
        "python-multipart==0.0.20",
    }
    expected_test_dependencies = {
        "pytest==9.1.1",
        "httpx==0.28.1",
        "pypdf==6.14.2",
    }

    assert pyproject["project"]["requires-python"] == ">=3.14,<3.15"
    assert dependencies == expected_dependencies
    assert test_dependencies == expected_test_dependencies


def test_stack_version_baseline_records_runtime_and_frontend_targets():
    assert STACK_VERSION_BASELINE["runtime"]["python"] == "3.14.5"
    assert STACK_VERSION_BASELINE["runtime"]["node_lts"] == "24.15.0"
    assert STACK_VERSION_BASELINE["backend"]["fastapi"] == "0.141.1"
    assert STACK_VERSION_BASELINE["backend"]["pydantic"] == "2.13.4"
    assert STACK_VERSION_BASELINE["backend"]["sqlalchemy"] == "2.0.51"
    assert STACK_VERSION_BASELINE["backend"]["alembic"] == "1.18.5"
    assert STACK_VERSION_BASELINE["backend"]["psycopg"] == "3.3.4"
    assert STACK_VERSION_BASELINE["backend"]["openpyxl"] == "3.1.5"
    assert STACK_VERSION_BASELINE["backend"]["reportlab"] == "5.0.0"
    assert STACK_VERSION_BASELINE["backend"]["python_pptx"] == "1.0.2"
    assert STACK_VERSION_BASELINE["backend"]["google_cloud_secret_manager"] == "2.30.0"
    assert STACK_VERSION_BASELINE["backend"]["google_cloud_storage"] == "3.13.0"
    assert STACK_VERSION_BASELINE["backend"]["pypdf"] == "6.14.2"
    assert STACK_VERSION_BASELINE["datastores"]["postgresql"] == "18.3"
    assert STACK_VERSION_BASELINE["frontend"]["next"] == "16.2.6"
    assert STACK_VERSION_BASELINE["frontend"]["react"] == "19.2.6"
