# ============================================================================
# Purpose: Coverage for the credential-API secret-ref prefix gate that opts a
#   deployment into local-secret:// refs while UMS_CONNECTOR_LOCAL_SECRETS_FILE
#   is configured: acceptance/rejection per env state, blank-value
#   normalization to disabled, settings-cache invalidation after removal, and
#   the deferred tenant-currency validation contract for mode-independent
#   consumers.
# Database/ORM: None — pure validation-function unit coverage.
# Standards: Settings reload per test via the conftest reset_app_settings_cache
#   fixture; the production frozen SECRET_REF_PREFIXES tuple stays untouched.
# Blast Radius: Test suite only — the production default (env unset) keeps
#   local-secret:// rejected at the API boundary.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/credentials.py ->
#     allowed_secret_ref_prefixes + is_external_secret_ref under test.
#   - File: backend/ums_smart_revenue/config/settings.py ->
#     connector_local_secrets_file gates the accepted prefix set.
# ============================================================================
"""local-secret:// credential-ref gate tests (UMS_CONNECTOR_LOCAL_SECRETS_FILE)."""

from __future__ import annotations

import pytest

from ums_smart_revenue.config.settings import load_app_settings
from ums_smart_revenue.connectors.credentials import (
    SECRET_REF_PREFIXES,
    allowed_secret_ref_prefixes,
    is_external_secret_ref,
)


def test_local_secret_ref_rejected_while_local_secrets_file_unset(monkeypatch, tmp_path) -> None:
    """local-secret:// refs stay rejected while the secrets-file env var is unset."""
    monkeypatch.delenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", raising=False)

    assert is_external_secret_ref("local-secret://yt-owner") is False
    assert is_external_secret_ref("  local-secret://yt-owner  ") is False
    assert allowed_secret_ref_prefixes() == SECRET_REF_PREFIXES


def test_local_secret_ref_accepted_while_local_secrets_file_set(monkeypatch, tmp_path) -> None:
    """local-secret:// refs are accepted only while the secrets-file env var is set."""
    secrets_file = tmp_path / "connector-secrets.json"
    secrets_file.write_text('{"yt-owner": "{}"}', encoding="utf-8")
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))

    assert is_external_secret_ref("local-secret://yt-owner") is True
    assert is_external_secret_ref("  local-secret://yt-owner  ") is True
    # Blank names stay rejected exactly like the production prefixes.
    assert is_external_secret_ref("local-secret://") is False
    assert is_external_secret_ref("local-secret://   ") is False
    assert allowed_secret_ref_prefixes() == (*SECRET_REF_PREFIXES, "local-secret://")


def test_local_secret_ref_still_rejected_after_setting_removed(monkeypatch, tmp_path) -> None:
    """Removing the env var restores the production rejection on the next settings load."""
    secrets_file = tmp_path / "connector-secrets.json"
    secrets_file.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    assert is_external_secret_ref("local-secret://yt-owner") is True

    monkeypatch.delenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", raising=False)
    load_app_settings.cache_clear()

    assert is_external_secret_ref("local-secret://yt-owner") is False


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_local_secrets_file_env_normalizes_to_disabled(monkeypatch, blank) -> None:
    """Blank or whitespace-only env values normalize to None and keep the lane disabled."""
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", blank)

    assert load_app_settings().connector_local_secrets_file is None
    assert is_external_secret_ref("local-secret://yt-owner") is False
    assert allowed_secret_ref_prefixes() == SECRET_REF_PREFIXES


def test_local_secret_gate_survives_malformed_currency_in_database_mode(
    monkeypatch, tmp_path
) -> None:
    """The gate must not crash when UMS_TENANT_PRIMARY_CURRENCY is malformed.

    Database-authz deployments never consume the currency setting; strict
    validation is deferred for mode-independent consumers (contract shared
    with app.py / connectors/google/audit.py), so credential-ref validation
    must keep working instead of raising ValueError.
    """
    secrets_file = tmp_path / "connector-secrets.json"
    secrets_file.write_text('{"yt-owner": "{}"}', encoding="utf-8")
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    monkeypatch.setenv("UMS_TENANT_PRIMARY_CURRENCY", "not-a-code")

    assert is_external_secret_ref("local-secret://yt-owner") is True
