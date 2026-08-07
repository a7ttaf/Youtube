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

from fastapi.testclient import TestClient

from ums_smart_revenue.app import create_app
from ums_smart_revenue.config.settings import load_app_settings
from ums_smart_revenue.connectors.runs.scheduler import GroupSyncScheduler

_VALID_ACTOR_UUID = "11111111-2222-3333-4444-555555555555"

_SCHEDULE_ENV = "UMS_GROUP_SYNC_SCHEDULE_ENABLED"
_EXECUTOR_ENV = "UMS_CONNECTOR_JOB_EXECUTOR_ENABLED"
_ACTOR_ENV = "UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID"


def _sqlite_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'app.db').as_posix()}"


def _clear_envs() -> None:
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

        def _spy_scheduler_close() -> None:
            close_order.append("scheduler")
            real_scheduler_close()

        def _spy_executor_close() -> None:
            close_order.append("executor")
            real_executor_close()

        scheduler.close = _spy_scheduler_close  # type: ignore[method-assign]
        app.state.connector_job_executor.close = _spy_executor_close  # type: ignore[method-assign]

        with TestClient(app):
            # The scheduler is started inside create_app (not deferred to the
            # lifespan startup event, same precedent as the executor's own
            # construction), so by the time lifespan startup has run the tick
            # thread is already alive.
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
