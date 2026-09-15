# ============================================================================
# Purpose: Coverage for the UMS_CONNECTOR_LOCAL_SECRETS_FILE opt-in lane of the
#   secret-resolver dispatch: registration gating (unset -> unsupported
#   scheme), file-backed resolution with per-resolve re-reads, and the
#   fail-closed family (missing file, invalid UTF-8, malformed JSON, non-string
#   mapping, oversized file, unknown key).
# Database/ORM: None — pure resolver-dispatch unit coverage; no Google network
#   access (the GCP resolver constructor is stubbed).
# Standards: Registry snapshot/restore around each test mirrors
#   test_secret_resolver.py; every test re-reads fresh settings via the
#   conftest reset_app_settings_cache fixture.
# Blast Radius: Test suite only — the production default (env unset) keeps
#   local-secret:// unregistered and unsupported.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/secret_resolver.py ->
#     ensure_default_resolvers + _FileBackedLocalSecretResolver under test.
#   - File: backend/ums_smart_revenue/config/settings.py ->
#     connector_local_secrets_file gates registration.
# ============================================================================
"""Opt-in local secrets-file resolver tests (UMS_CONNECTOR_LOCAL_SECRETS_FILE)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ums_smart_revenue.connectors.google import secret_resolver
from ums_smart_revenue.connectors.google.errors import (
    LocalSecretsFileError,
    SecretNotFoundError,
    UnsupportedSecretSchemeError,
)
from ums_smart_revenue.connectors.google.secret_resolver import (
    ensure_default_resolvers,
    register_resolver,
    resolve_secret,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    """Snapshot, clear, and restore the resolver registry around each test."""
    snapshot = dict(secret_resolver._REGISTRY)
    secret_resolver._REGISTRY.clear()
    yield
    secret_resolver._REGISTRY.clear()
    secret_resolver._REGISTRY.update(snapshot)


class _StubResolver:
    """Minimal SecretResolver stand-in returning a fixed payload."""

    def __init__(self, payload: str) -> None:
        """Store the payload this stub resolves every ref to."""
        self.payload = payload

    def resolve(self, ref: str) -> str:
        """Return the fixed payload (records the ref for assertions)."""
        return self.payload


def _stub_gcp_resolver(monkeypatch) -> None:
    """Patch the GCP resolver constructor so boot never needs Google credentials."""
    from ums_smart_revenue.connectors.google import gcp_secret_manager

    monkeypatch.setattr(
        gcp_secret_manager,
        "GcpSecretManagerResolver",
        lambda: _StubResolver(payload="gcp-payload"),
    )


def _write_secrets_file(directory: Path, content: str) -> Path:
    """Write ``content`` to a secrets file in ``directory`` and return its path."""
    path = directory / "connector-secrets.json"
    path.write_text(content, encoding="utf-8")
    return path


def test_ensure_default_resolvers_skips_local_secret_when_file_unset(monkeypatch) -> None:
    """The local-secret lane stays unregistered (fail closed) when the env var is unset."""
    _stub_gcp_resolver(monkeypatch)
    monkeypatch.delenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", raising=False)

    ensure_default_resolvers()

    with pytest.raises(UnsupportedSecretSchemeError):
        resolve_secret("local-secret://yt-owner")


def test_ensure_default_resolvers_registers_file_backed_local_secret(monkeypatch, tmp_path) -> None:
    """A configured secrets file registers the file-backed local-secret resolver."""
    _stub_gcp_resolver(monkeypatch)
    payload = json.dumps(
        {
            "refresh_token": "rt",
            "client_id": "cid",
            "client_secret": "cs",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    secrets_file = _write_secrets_file(tmp_path, json.dumps({"yt-owner": payload}))
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))

    ensure_default_resolvers()

    assert resolve_secret("local-secret://yt-owner") == payload


def test_file_backed_local_secret_rereads_file_on_each_resolve(monkeypatch, tmp_path) -> None:
    """Each resolve re-reads the mapping file so payload rotation applies without restart."""
    _stub_gcp_resolver(monkeypatch)
    secrets_file = _write_secrets_file(tmp_path, json.dumps({"yt-owner": "payload-v1"}))
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    ensure_default_resolvers()

    first = resolve_secret("local-secret://yt-owner")
    secrets_file.write_text(json.dumps({"yt-owner": "payload-v2"}), encoding="utf-8")
    second = resolve_secret("local-secret://yt-owner")

    assert first == "payload-v1"
    assert second == "payload-v2"


def test_file_backed_local_secret_fails_closed_on_missing_file(monkeypatch, tmp_path) -> None:
    """An unreadable secrets file fails closed with LocalSecretsFileError."""
    _stub_gcp_resolver(monkeypatch)
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(tmp_path / "does-not-exist.json"))
    ensure_default_resolvers()

    with pytest.raises(LocalSecretsFileError) as ctx:
        resolve_secret("local-secret://yt-owner")
    assert ctx.value.path.endswith("does-not-exist.json")
    assert isinstance(ctx.value.inner, OSError)


def test_file_backed_local_secret_fails_closed_on_invalid_utf8(monkeypatch, tmp_path) -> None:
    """Invalid UTF-8 content fails closed with LocalSecretsFileError, not a raw decode error."""
    _stub_gcp_resolver(monkeypatch)
    path = tmp_path / "connector-secrets.json"
    path.write_bytes(b'{"yt-owner": "\xff\xfe-invalid-utf8"}')
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(path))
    ensure_default_resolvers()

    with pytest.raises(LocalSecretsFileError) as ctx:
        resolve_secret("local-secret://yt-owner")
    assert isinstance(ctx.value.inner, UnicodeDecodeError)


def test_file_backed_local_secret_fails_closed_on_malformed_json(monkeypatch, tmp_path) -> None:
    """Invalid JSON in the secrets file fails closed with LocalSecretsFileError."""
    _stub_gcp_resolver(monkeypatch)
    secrets_file = _write_secrets_file(tmp_path, "{not-json")
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    ensure_default_resolvers()

    with pytest.raises(LocalSecretsFileError):
        resolve_secret("local-secret://yt-owner")


@pytest.mark.parametrize(
    "content",
    [
        '["not", "an", "object"]',
        '{"yt-owner": 123}',
        '{"yt-owner": {"nested": "object"}}',
        '{"yt-owner": null}',
    ],
)
def test_file_backed_local_secret_fails_closed_on_non_string_mapping(
    monkeypatch, tmp_path, content
) -> None:
    """Valid JSON that is not a str-to-str mapping fails closed with LocalSecretsFileError."""
    _stub_gcp_resolver(monkeypatch)
    secrets_file = _write_secrets_file(tmp_path, content)
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    ensure_default_resolvers()

    with pytest.raises(LocalSecretsFileError):
        resolve_secret("local-secret://yt-owner")


def test_file_backed_local_secret_fails_closed_on_oversized_file(monkeypatch, tmp_path) -> None:
    """A secrets file beyond the bounded read cap fails closed instead of parsing."""
    _stub_gcp_resolver(monkeypatch)
    padding = "x" * (1024 * 1024 + 16)
    secrets_file = _write_secrets_file(tmp_path, json.dumps({"yt-owner": padding}))
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    ensure_default_resolvers()

    with pytest.raises(LocalSecretsFileError):
        resolve_secret("local-secret://yt-owner")


def test_file_backed_local_secret_unknown_key_raises_secret_not_found(
    monkeypatch, tmp_path
) -> None:
    """A ref whose name is absent from the mapping raises SecretNotFoundError."""
    _stub_gcp_resolver(monkeypatch)
    secrets_file = _write_secrets_file(tmp_path, json.dumps({"other-owner": "payload"}))
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    ensure_default_resolvers()

    with pytest.raises(SecretNotFoundError):
        resolve_secret("local-secret://yt-owner")


def test_registered_local_secret_test_resolver_takes_precedence_over_file_lane(
    monkeypatch, tmp_path
) -> None:
    """An explicitly registered local-secret resolver (tests/CLI smoke) is not replaced."""
    _stub_gcp_resolver(monkeypatch)
    secrets_file = _write_secrets_file(tmp_path, json.dumps({"yt-owner": "file-payload"}))
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    register_resolver(scheme="local-secret", resolver=_StubResolver(payload="explicit-payload"))

    ensure_default_resolvers()

    assert resolve_secret("local-secret://yt-owner") == "explicit-payload"
