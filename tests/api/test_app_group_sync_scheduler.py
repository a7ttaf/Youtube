# ============================================================================
# Purpose: Pin create_app's GroupSyncScheduler boot wiring — the fail-fast
#   ValueErrors when the schedule flag is set without the executor flag or
#   without the service-actor id, the scheduler construction/start when both
#   are set, the lifespan teardown ORDER (scheduler first, then executor),
#   and the disabled-by-default app spawning no scheduler at all.
# Database/ORM: a real SQLite URL per test (tmp_path) so create_app builds
#   its real session factories; no tables are exercised.
# Standards: UMS_* env vars are set/cleared explicitly per test with
#   load_app_settings.cache_clear() between mutations; threads started by the
#   wiring are torn down through the app lifespan, never leaked.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> the boot wiring under test.
#   - File: backend/ums_smart_revenue/connectors/runs/scheduler.py -> the
#     scheduler whose start/close lifecycle is asserted.
# ============================================================================
"""Tests for GroupSyncScheduler wiring + lifespan teardown in create_app."""

from __future__ import annotations

import os
import threading

import pytest
from fastapi.testclient import TestClient

import ums_smart_revenue.app as app_module
from ums_smart_revenue.app import create_app
from ums_smart_revenue.config.logging_config import LoggingConfiguration
from ums_smart_revenue.config.settings import load_app_settings
from ums_smart_revenue.connectors.runs.scheduler import GroupSyncScheduler

_VALID_ACTOR_UUID = "11111111-2222-3333-4444-555555555555"

_SCHEDULE_ENV = "UMS_GROUP_SYNC_SCHEDULE_ENABLED"
_EXECUTOR_ENV = "UMS_CONNECTOR_JOB_EXECUTOR_ENABLED"
_ACTOR_ENV = "UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID"


def _sqlite_url(tmp_path) -> str:
    """Build a per-test SQLite URL under tmp_path."""
    return f"sqlite+pysqlite:///{(tmp_path / 'app.db').as_posix()}"


def _clear_envs() -> None:
    """Clear schedule/executor/actor env vars and settings cache."""
    os.environ.pop(_SCHEDULE_ENV, None)
    os.environ.pop(_EXECUTOR_ENV, None)
    os.environ.pop(_ACTOR_ENV, None)
    load_app_settings.cache_clear()


def test_schedule_enabled_without_executor_raises(tmp_path) -> None:
    """Schedule on + executor off refuses to build create_app, naming both env vars."""
    _clear_envs()
    os.environ[_SCHEDULE_ENV] = "true"
    load_app_settings.cache_clear()
    try:
        try:
            create_app(database_url=_sqlite_url(tmp_path))
        except ValueError as exc:
            message = str(exc)
            assert _SCHEDULE_ENV in message
            assert _EXECUTOR_ENV in message
        else:
            raise AssertionError("create_app should have raised ValueError")
    finally:
        _clear_envs()


def test_schedule_enabled_without_service_actor_raises(tmp_path) -> None:
    """Schedule + executor on but no service actor id refuses to build, naming the actor env var."""
    _clear_envs()
    os.environ[_SCHEDULE_ENV] = "true"
    os.environ[_EXECUTOR_ENV] = "true"
    load_app_settings.cache_clear()
    try:
        try:
            create_app(database_url=_sqlite_url(tmp_path))
        except ValueError as exc:
            assert _ACTOR_ENV in str(exc)
        else:
            raise AssertionError("create_app should have raised ValueError")
    finally:
        _clear_envs()


def test_schedule_enabled_builds_scheduler_and_closes_in_order(tmp_path) -> None:
    """Enabled + executor + actor: scheduler runs through lifespan, closes before the executor."""
    _clear_envs()
    os.environ[_SCHEDULE_ENV] = "true"
    os.environ[_EXECUTOR_ENV] = "true"
    os.environ[_ACTOR_ENV] = _VALID_ACTOR_UUID
    load_app_settings.cache_clear()
    try:
        app = create_app(database_url=_sqlite_url(tmp_path))
        scheduler = getattr(app.state, "group_sync_scheduler", None)
        assert isinstance(scheduler, GroupSyncScheduler)

        close_order: list[str] = []
        # Capture the ORIGINAL bound close() methods before overwriting the
        # attributes, so each spy delegates to the real teardown instead of
        # recursing into itself (the attribute it is about to replace) --
        # mirrors test_app_connector_executor.py's spy pattern.
        real_scheduler_close = scheduler.close
        real_executor_close = app.state.connector_job_executor.close
        # This lifecycle-only scratch database is intentionally not migrated.
        # Recovery against real tenants/audit_logs rows is covered directly in
        # test_executor.py, so stub only that startup scan here.
        setattr(
            app.state.connector_job_executor,
            "recover_abandoned_submission_intents",
            lambda: 0,
        )

        def _spy_scheduler_close() -> bool:
            """Record scheduler close order and delegate to the real close()."""
            close_order.append("scheduler")
            return real_scheduler_close()

        def _spy_executor_close() -> bool:
            """Record executor close order and delegate to the real close()."""
            close_order.append("executor")
            # Preserve the real drain result so the lifespan also exercises its
            # clean/unclean diagnostic branch.
            return real_executor_close()

        setattr(scheduler, "close", _spy_scheduler_close)
        setattr(app.state.connector_job_executor, "close", _spy_executor_close)

        with TestClient(app):
            # Startup recovery runs before the scheduler begins ticking; by the
            # time TestClient enters, the deferred thread must be alive.
            assert scheduler._thread is not None
            assert scheduler._thread.is_alive()

        assert close_order == ["scheduler", "executor"]
    finally:
        _clear_envs()


def test_no_scheduler_when_disabled(tmp_path) -> None:
    """Default (flag unset) leaves no scheduler attribute on app.state and no thread."""
    _clear_envs()
    app = create_app(database_url=_sqlite_url(tmp_path))
    assert getattr(app.state, "group_sync_scheduler", None) is None
    assert not any(t.name == "ums-group-sync-scheduler" for t in threading.enumerate())


def test_startup_recovers_durable_intents_before_scheduler_start(tmp_path) -> None:
    """No scheduler tick can race the prior process's intent reconciliation."""
    _clear_envs()
    os.environ[_SCHEDULE_ENV] = "true"
    os.environ[_EXECUTOR_ENV] = "true"
    os.environ[_ACTOR_ENV] = _VALID_ACTOR_UUID
    load_app_settings.cache_clear()
    try:
        app = create_app(database_url=_sqlite_url(tmp_path))
        scheduler = app.state.group_sync_scheduler
        executor = app.state.connector_job_executor
        real_executor_close = executor.close
        startup_order: list[str] = []

        setattr(
            executor,
            "recover_abandoned_submission_intents",
            lambda: startup_order.append("recover"),
        )
        setattr(scheduler, "start", lambda: startup_order.append("scheduler"))
        setattr(scheduler, "close", lambda: True)
        setattr(executor, "close", real_executor_close)

        with TestClient(app):
            assert startup_order == ["recover", "scheduler"]
    finally:
        _clear_envs()


def test_scheduler_close_exception_still_closes_executor_and_restores_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scheduler close error cannot skip executor close or double-release logging."""
    _clear_envs()
    os.environ[_SCHEDULE_ENV] = "true"
    os.environ[_EXECUTOR_ENV] = "true"
    os.environ[_ACTOR_ENV] = _VALID_ACTOR_UUID
    load_app_settings.cache_clear()
    try:
        app = create_app(database_url=_sqlite_url(tmp_path))
        scheduler = app.state.group_sync_scheduler
        executor = app.state.connector_job_executor
        real_scheduler_close = scheduler.close
        real_executor_close = executor.close
        real_restore = app_module.restore_logging
        close_order: list[str] = []
        restore_calls = {"count": 0}
        restored = threading.Event()

        setattr(executor, "recover_abandoned_submission_intents", lambda: 0)

        def _raising_scheduler_close() -> bool:
            """Model a post-stop close failure with no surviving thread."""
            close_order.append("scheduler")
            assert real_scheduler_close() is True
            raise RuntimeError("scheduler close failed")

        def _executor_close() -> bool:
            """Prove executor teardown still runs after scheduler failure."""
            close_order.append("executor")
            return real_executor_close()

        def _restore_once(configuration: LoggingConfiguration) -> None:
            """Count and forward the one logging-lease release."""
            restore_calls["count"] += 1
            real_restore(configuration)
            restored.set()

        setattr(scheduler, "close", _raising_scheduler_close)
        setattr(executor, "close", _executor_close)
        monkeypatch.setattr(app_module, "restore_logging", _restore_once)

        with pytest.raises(ExceptionGroup, match="background worker shutdown failed"):
            with TestClient(app):
                pass

        assert close_order == ["scheduler", "executor"]
        assert restored.wait(timeout=2)
        assert restore_calls["count"] == 1
    finally:
        _clear_envs()


def test_scheduler_survivor_defers_logging_restore_until_completion(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A False scheduler close result retains logging until its thread exits."""
    _clear_envs()
    os.environ[_SCHEDULE_ENV] = "true"
    os.environ[_EXECUTOR_ENV] = "true"
    os.environ[_ACTOR_ENV] = _VALID_ACTOR_UUID
    load_app_settings.cache_clear()
    wait_started = threading.Event()
    completion_allowed = threading.Event()
    restored = threading.Event()
    restore_calls = {"count": 0}
    try:
        app = create_app(database_url=_sqlite_url(tmp_path))
        scheduler = app.state.group_sync_scheduler
        executor = app.state.connector_job_executor
        real_executor_close = executor.close
        real_restore = app_module.restore_logging

        setattr(executor, "recover_abandoned_submission_intents", lambda: 0)
        setattr(scheduler, "start", lambda: None)
        setattr(scheduler, "close", lambda: False)

        def _wait_for_scheduler() -> None:
            """Expose deterministic control of the scheduler completion edge."""
            wait_started.set()
            assert completion_allowed.wait(timeout=5)

        def _restore_once(configuration: LoggingConfiguration) -> None:
            """Count and forward the real logging-lease release."""
            restore_calls["count"] += 1
            real_restore(configuration)
            restored.set()

        setattr(scheduler, "wait_for_shutdown_completion", _wait_for_scheduler)
        setattr(executor, "close", real_executor_close)
        monkeypatch.setattr(app_module, "restore_logging", _restore_once)

        with TestClient(app):
            pass

        assert wait_started.wait(timeout=2)
        assert restore_calls["count"] == 0
        completion_allowed.set()
        assert restored.wait(timeout=2)
        assert restore_calls["count"] == 1
    finally:
        completion_allowed.set()
        _clear_envs()


def test_scheduler_start_failure_closes_both_workers_and_restores_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A startup Thread.start failure still runs complete lifespan cleanup."""
    _clear_envs()
    os.environ[_SCHEDULE_ENV] = "true"
    os.environ[_EXECUTOR_ENV] = "true"
    os.environ[_ACTOR_ENV] = _VALID_ACTOR_UUID
    load_app_settings.cache_clear()
    try:
        app = create_app(database_url=_sqlite_url(tmp_path))
        scheduler = app.state.group_sync_scheduler
        executor = app.state.connector_job_executor
        real_scheduler_close = scheduler.close
        real_executor_close = executor.close
        real_restore = app_module.restore_logging
        close_order: list[str] = []
        restore_calls = {"count": 0}

        setattr(executor, "recover_abandoned_submission_intents", lambda: 0)

        def _start_failure() -> None:
            """Model Thread.start() rejecting scheduler startup."""
            raise RuntimeError("thread start failed")

        def _scheduler_close() -> bool:
            close_order.append("scheduler")
            return real_scheduler_close()

        def _executor_close() -> bool:
            close_order.append("executor")
            return real_executor_close()

        def _restore_once(configuration: LoggingConfiguration) -> None:
            restore_calls["count"] += 1
            real_restore(configuration)

        setattr(scheduler, "start", _start_failure)
        setattr(scheduler, "close", _scheduler_close)
        setattr(executor, "close", _executor_close)
        monkeypatch.setattr(app_module, "restore_logging", _restore_once)

        with pytest.raises(RuntimeError, match="thread start failed"):
            with TestClient(app):
                pass

        assert close_order == ["scheduler", "executor"]
        assert restore_calls["count"] == 1
    finally:
        _clear_envs()
