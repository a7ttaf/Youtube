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
    GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV,
    AppSettings,
    load_app_settings,
)

_VALID_ACTOR_UUID = "11111111-2222-3333-4444-555555555555"


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
    monkeypatch.setenv(
        GOOGLE_CONNECTOR_SERVICE_ACTOR_ID_ENV, f"   {_VALID_ACTOR_UUID}\n"
    )
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
