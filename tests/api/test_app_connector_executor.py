"""Tests for ConnectorJobExecutor wiring + lifespan teardown in create_app."""

from __future__ import annotations

import os
import threading

import pytest
from fastapi.testclient import TestClient

import ums_smart_revenue.app as app_module
from ums_smart_revenue.app import create_app
from ums_smart_revenue.config.logging_config import LoggingConfiguration
from ums_smart_revenue.config.settings import load_app_settings
from ums_smart_revenue.connectors.runs.executor import ConnectorJobExecutor


def _sqlite_url(tmp_path) -> str:
    """Build a per-test SQLite URL under tmp_path."""
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
        # This lifecycle-only scratch database is intentionally not migrated.
        # Recovery against real tenants/audit_logs rows is covered directly in
        # test_executor.py, so stub only that startup scan here.
        setattr(
            app.state.connector_job_executor,
            "recover_abandoned_submission_intents",
            lambda: 0,
        )

        def _spy() -> bool:
            """Count lifespan close() and delegate to the real executor close."""
            closed["count"] += 1
            # Preserve the real drain result so the lifespan also exercises its
            # clean/unclean diagnostic branch.
            return real_close()

        setattr(app.state.connector_job_executor, "close", _spy)
        with TestClient(app):
            pass
        assert closed["count"] == 1
    finally:
        os.environ.pop("UMS_CONNECTOR_JOB_EXECUTOR_ENABLED", None)
        load_app_settings.cache_clear()


def test_executor_survivor_defers_logging_restore_until_completion(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A False close result retains logging until the explicit completion edge."""
    os.environ["UMS_CONNECTOR_JOB_EXECUTOR_ENABLED"] = "true"
    load_app_settings.cache_clear()
    completion_allowed = threading.Event()
    wait_started = threading.Event()
    restored = threading.Event()
    restore_calls = {"count": 0}
    try:
        app = create_app(database_url=_sqlite_url(tmp_path))
        executor = app.state.connector_job_executor
        real_close = executor.close
        real_restore = app_module.restore_logging

        setattr(executor, "recover_abandoned_submission_intents", lambda: 0)

        def _bounded_close_with_survivor() -> bool:
            """Close the idle real pool but model one retained completion edge."""
            assert real_close() is True
            return False

        def _wait_for_completion() -> None:
            """Expose deterministic control of the eventual worker completion."""
            wait_started.set()
            assert completion_allowed.wait(timeout=5)

        def _restore_once(configuration: LoggingConfiguration) -> None:
            """Count and forward the real logging-lease release."""
            restore_calls["count"] += 1
            real_restore(configuration)
            restored.set()

        setattr(executor, "close", _bounded_close_with_survivor)
        setattr(executor, "wait_for_shutdown_completion", _wait_for_completion)
        monkeypatch.setattr(app_module, "restore_logging", _restore_once)

        with TestClient(app):
            pass

        assert wait_started.wait(timeout=2)
        assert restore_calls["count"] == 0
        assert not restored.is_set()

        completion_allowed.set()
        assert restored.wait(timeout=2)
        assert restore_calls["count"] == 1
    finally:
        completion_allowed.set()
        os.environ.pop("UMS_CONNECTOR_JOB_EXECUTOR_ENABLED", None)
        load_app_settings.cache_clear()


def test_completion_wait_failure_retains_redaction_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An observer error is not proof that a worker stopped logging."""
    failure_logged = threading.Event()
    output_calls = {"count": 0}
    restore_calls = {"count": 0}
    configuration = LoggingConfiguration(
        installed=False,
        handler=None,
        previous_root_level=0,
        previous_first_party_level=0,
    )

    class _BrokenWaitExecutor:
        """Executor stub whose completion observer always fails."""

        def wait_for_shutdown_completion(self) -> None:
            """Model a broken completion observer after bounded close."""
            raise RuntimeError("completion observer failed")

    def _restore_once(_configuration: LoggingConfiguration) -> None:
        """Record an unsafe redaction-safety release."""
        restore_calls["count"] += 1

    def _release_output_once(_configuration: LoggingConfiguration) -> None:
        """Output ownership should still end at bounded shutdown."""
        output_calls["count"] += 1

    def _record_failure(*_args, **_kwargs) -> None:
        """Expose the point after the watcher handled the observer failure."""
        failure_logged.set()

    monkeypatch.setattr(app_module, "release_logging_output", _release_output_once)
    monkeypatch.setattr(app_module, "restore_logging", _restore_once)
    monkeypatch.setattr(app_module.logger, "exception", _record_failure)
    app_module._defer_logging_restore_until_workers_finish(
        configuration=configuration,
        scheduler=None,
        executor=_BrokenWaitExecutor(),  # type: ignore[arg-type]
    )

    assert failure_logged.wait(timeout=2)
    assert output_calls["count"] == 1
    assert restore_calls["count"] == 0, (
        "redaction safety was released without a confirmed worker termination edge"
    )
