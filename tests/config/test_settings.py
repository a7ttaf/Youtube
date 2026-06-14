"""Unit tests for ``AppSettings`` and ``load_app_settings``.

Covers the existing UMS_DATABASE_URL / UMS_TRUSTED_GATEWAY_TOKEN /
UMS_AUTHZ_SOURCE behaviour plus the T36 service-principal actor id:

* The cached loader honours ``cache_clear`` (the autouse conftest fixture
  already calls this between tests; we exercise it explicitly here).
* ``UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`` is parsed as a UUID and stored
  as its canonical string form so the value drops straight into
  ``UserPrincipal.user_id`` without a round-trip.
* A missing env value resolves to ``None`` (lazy: only emitters/orchestrator
  that actually need the identity raise on absence).
* A malformed value is rejected at load time so misconfigured deployments
  fail fast instead of silently producing garbled audit actor ids.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from ums_smart_revenue.config.settings import (
    CONNECTOR_JOB_EXECUTOR_ENABLED_ENV,
    CONNECTOR_JOB_MAX_WORKERS_ENV,
    CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV,
    GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
    AppSettings,
    load_app_settings,
)

_VALID_ACTOR_UUID = "11111111-2222-3333-4444-555555555555"

_EXECUTOR_ENVS = (
    CONNECTOR_JOB_EXECUTOR_ENABLED_ENV,
    CONNECTOR_JOB_MAX_WORKERS_ENV,
    CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV,
)


def _clear_executor_envs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every executor env var so defaults apply."""
    for name in _EXECUTOR_ENVS:
        monkeypatch.delenv(name, raising=False)


def test_load_app_settings_executor_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unset executor envs resolve to the fail-closed defaults."""
    _clear_executor_envs(monkeypatch)
    load_app_settings.cache_clear()
    settings = load_app_settings()
    assert settings.connector_job_executor_enabled is False
    assert settings.connector_job_max_workers == 1
    assert settings.connector_job_stale_running_hours == 6


def test_load_app_settings_executor_valid_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valid executor env values override the defaults."""
    _clear_executor_envs(monkeypatch)
    monkeypatch.setenv(CONNECTOR_JOB_EXECUTOR_ENABLED_ENV, "true")
    monkeypatch.setenv(CONNECTOR_JOB_MAX_WORKERS_ENV, "4")
    monkeypatch.setenv(CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV, "12")
    load_app_settings.cache_clear()
    settings = load_app_settings()
    assert settings.connector_job_executor_enabled is True
    assert settings.connector_job_max_workers == 4
    assert settings.connector_job_stale_running_hours == 12


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "  yes  ", "on"])
def test_load_app_settings_executor_enabled_truthy(
    monkeypatch: pytest.MonkeyPatch, truthy: str
) -> None:
    """Recognised truthy tokens enable the executor."""
    _clear_executor_envs(monkeypatch)
    monkeypatch.setenv(CONNECTOR_JOB_EXECUTOR_ENABLED_ENV, truthy)
    load_app_settings.cache_clear()
    assert load_app_settings().connector_job_executor_enabled is True


@pytest.mark.parametrize("falsy", ["0", "false", "FALSE", "  no  ", "off", ""])
def test_load_app_settings_executor_enabled_falsy(
    monkeypatch: pytest.MonkeyPatch, falsy: str
) -> None:
    """Recognised falsy/blank tokens leave the executor disabled."""
    _clear_executor_envs(monkeypatch)
    monkeypatch.setenv(CONNECTOR_JOB_EXECUTOR_ENABLED_ENV, falsy)
    load_app_settings.cache_clear()
    assert load_app_settings().connector_job_executor_enabled is False


def test_load_app_settings_rejects_malformed_max_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-integer max-workers value fails fast with the env name."""
    _clear_executor_envs(monkeypatch)
    monkeypatch.setenv(CONNECTOR_JOB_MAX_WORKERS_ENV, "not-a-number")
    load_app_settings.cache_clear()
    with pytest.raises(ValueError) as excinfo:
        load_app_settings()
    assert CONNECTOR_JOB_MAX_WORKERS_ENV in str(excinfo.value)


def test_load_app_settings_rejects_zero_max_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-positive max-workers value fails fast with the env name."""
    _clear_executor_envs(monkeypatch)
    monkeypatch.setenv(CONNECTOR_JOB_MAX_WORKERS_ENV, "0")
    load_app_settings.cache_clear()
    with pytest.raises(ValueError) as excinfo:
        load_app_settings()
    assert CONNECTOR_JOB_MAX_WORKERS_ENV in str(excinfo.value)


def test_load_app_settings_rejects_zero_stale_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-positive stale-running-hours value fails fast with the env name."""
    _clear_executor_envs(monkeypatch)
    monkeypatch.setenv(CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV, "-3")
    load_app_settings.cache_clear()
    with pytest.raises(ValueError) as excinfo:
        load_app_settings()
    assert CONNECTOR_JOB_STALE_RUNNING_HOURS_ENV in str(excinfo.value)


def test_load_app_settings_returns_none_for_unset_actor_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing service-actor env value leaves the field as None.

    The caller (T36 ``build_connector_service_principal``) is the one
    that decides whether None is acceptable for its context, so settings
    stays lazy here.
    """
    monkeypatch.delenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, raising=False)
    settings = load_app_settings()
    assert settings.google_connector_service_actor_id is None


def test_load_app_settings_parses_valid_service_actor_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid UUID env value is stored as its canonical string form."""
    monkeypatch.setenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, _VALID_ACTOR_UUID)
    settings = load_app_settings()
    assert isinstance(settings, AppSettings)
    assert settings.google_connector_service_actor_id == _VALID_ACTOR_UUID
    # Canonical str form: UUID parse succeeds.
    UUID(settings.google_connector_service_actor_id)


def test_load_app_settings_strips_whitespace_around_actor_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whitespace around the env value is tolerated; the result stays canonical."""
    monkeypatch.setenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, f"   {_VALID_ACTOR_UUID}\n")
    settings = load_app_settings()
    assert settings.google_connector_service_actor_id == _VALID_ACTOR_UUID


def test_load_app_settings_rejects_malformed_service_actor_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-UUID env value is rejected at load time with the env name in the message."""
    monkeypatch.setenv(GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, "not-a-uuid")
    with pytest.raises(ValueError) as excinfo:
        load_app_settings()
    assert GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV in str(excinfo.value)
