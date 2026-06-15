"""Tests for ConnectorJobExecutor wiring + lifespan teardown in create_app."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from ums_smart_revenue.app import create_app
from ums_smart_revenue.config.settings import load_app_settings
from ums_smart_revenue.connectors.runs.executor import ConnectorJobExecutor


def _sqlite_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'app.db').as_posix()}"


def test_executor_attached_when_enabled(tmp_path) -> None:
    """With the flag on, app.state carries a ConnectorJobExecutor."""
    os.environ["UMS_CONNECTOR_JOB_EXECUTOR_ENABLED"] = "true"
    load_app_settings.cache_clear()
    try:
        app = create_app(database_url=_sqlite_url(tmp_path))
        executor = getattr(app.state, "connector_job_executor", None)
        assert isinstance(executor, ConnectorJobExecutor)
        executor.close()
    finally:
        os.environ.pop("UMS_CONNECTOR_JOB_EXECUTOR_ENABLED", None)
        load_app_settings.cache_clear()


def test_no_executor_when_disabled(tmp_path) -> None:
    """Default (disabled) leaves no executor attribute on app.state."""
    os.environ.pop("UMS_CONNECTOR_JOB_EXECUTOR_ENABLED", None)
    load_app_settings.cache_clear()
    app = create_app(database_url=_sqlite_url(tmp_path))
    assert getattr(app.state, "connector_job_executor", None) is None


def test_lifespan_shutdown_closes_executor(tmp_path) -> None:
    """Exiting the TestClient lifespan calls the executor's close()."""
    os.environ["UMS_CONNECTOR_JOB_EXECUTOR_ENABLED"] = "true"
    load_app_settings.cache_clear()
    try:
        app = create_app(database_url=_sqlite_url(tmp_path))
        closed = {"count": 0}
        # Capture the ORIGINAL bound close() before overwriting the attribute,
        # so the spy delegates to the real teardown instead of recursing into
        # itself (the attribute we are about to replace).
        real_close = app.state.connector_job_executor.close

        def _spy() -> None:
            closed["count"] += 1
            real_close()

        app.state.connector_job_executor.close = _spy  # type: ignore[method-assign]
        with TestClient(app):
            pass
        assert closed["count"] == 1
    finally:
        os.environ.pop("UMS_CONNECTOR_JOB_EXECUTOR_ENABLED", None)
        load_app_settings.cache_clear()
