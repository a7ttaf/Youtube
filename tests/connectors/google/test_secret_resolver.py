"""Secret resolver dispatch tests.

A dispatcher maps a URI scheme (e.g., 'gcp-secret-manager') to a resolver
implementation. Unknown / unimplemented schemes raise
UnsupportedSecretSchemeError; ORM-accepted prefixes that aren't implemented
(aws-secretsmanager://, vault://, kms://, azure-keyvault://)
are intentionally unknown until a future credential-lifecycle PR.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ums_smart_revenue.connectors.google.errors import (
    LocalSecretsFileError,
    MalformedSecretUriError,
    ResolverAlreadyRegisteredError,
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
    from ums_smart_revenue.connectors.google import secret_resolver as sr

    snapshot = dict(sr._REGISTRY)
    sr._REGISTRY.clear()
    yield
    sr._REGISTRY.clear()
    sr._REGISTRY.update(snapshot)


class _StubResolver:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls: list[str] = []

    def resolve(self, ref: str) -> str:
        self.calls.append(ref)
        return self.payload


def test_resolve_secret_dispatches_to_registered_scheme(monkeypatch) -> None:
    stub = _StubResolver(payload='{"refresh_token": "x"}')
    register_resolver(scheme="local-secret", resolver=stub)
    out = resolve_secret("local-secret://my-key")
    assert out == '{"refresh_token": "x"}'
    assert stub.calls == ["local-secret://my-key"]


def test_register_resolver_rejects_duplicate_scheme() -> None:
    first = _StubResolver(payload="first")
    second = _StubResolver(payload="second")
    register_resolver(scheme="local-secret", resolver=first)

    with pytest.raises(ResolverAlreadyRegisteredError) as ctx:
        register_resolver(scheme="local-secret", resolver=second)

    assert ctx.value.scheme == "local-secret"
    assert resolve_secret("local-secret://my-key") == "first"


def test_ensure_default_resolvers_registers_secret_manager_aliases(monkeypatch) -> None:
    from ums_smart_revenue.connectors.google import gcp_secret_manager

    monkeypatch.setattr(
        gcp_secret_manager,
        "GcpSecretManagerResolver",
        lambda: _StubResolver(payload="gcp-payload"),
    )

    ensure_default_resolvers()

    assert resolve_secret("gcp-secret-manager://projects/p/secrets/s/versions/latest") == (
        "gcp-payload"
    )
    assert resolve_secret("secret-manager://projects/p/secrets/s/versions/latest") == (
        "gcp-payload"
    )


def test_ensure_default_resolvers_is_race_safe(monkeypatch) -> None:
    from ums_smart_revenue.connectors.google import gcp_secret_manager

    constructed: list[_StubResolver] = []

    def _slow_resolver() -> _StubResolver:
        time.sleep(0.01)
        resolver = _StubResolver(payload="gcp-payload")
        constructed.append(resolver)
        return resolver

    monkeypatch.setattr(
        gcp_secret_manager,
        "GcpSecretManagerResolver",
        _slow_resolver,
    )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _idx: ensure_default_resolvers(), range(8)))

    assert len(constructed) == 1
    assert resolve_secret("gcp-secret-manager://projects/p/secrets/s/versions/latest") == (
        "gcp-payload"
    )
    assert resolve_secret("secret-manager://projects/p/secrets/s/versions/latest") == (
        "gcp-payload"
    )


def test_resolve_secret_raises_for_unknown_scheme() -> None:
    with pytest.raises(UnsupportedSecretSchemeError) as ctx:
        resolve_secret("aws-secretsmanager://my-arn")
    assert ctx.value.scheme == "aws-secretsmanager"


# -----------------------------------------------------------------------------
# UMS_CONNECTOR_LOCAL_SECRETS_FILE opt-in (demo/self-host local-secret:// lane)
# -----------------------------------------------------------------------------


def _stub_gcp_resolver(monkeypatch) -> None:
    from ums_smart_revenue.connectors.google import gcp_secret_manager

    monkeypatch.setattr(
        gcp_secret_manager,
        "GcpSecretManagerResolver",
        lambda: _StubResolver(payload="gcp-payload"),
    )


def test_ensure_default_resolvers_skips_local_secret_when_file_unset(monkeypatch) -> None:
    _stub_gcp_resolver(monkeypatch)
    monkeypatch.delenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", raising=False)

    ensure_default_resolvers()

    with pytest.raises(UnsupportedSecretSchemeError):
        resolve_secret("local-secret://yt-owner")


def test_ensure_default_resolvers_registers_file_backed_local_secret(monkeypatch, tmp_path) -> None:
    _stub_gcp_resolver(monkeypatch)
    secrets_file = tmp_path / "connector-secrets.json"
    payload = json.dumps(
        {
            "refresh_token": "rt",
            "client_id": "cid",
            "client_secret": "cs",
            "token_uri": "https://oauth2.googleapis.com/token",
        }
    )
    secrets_file.write_text(json.dumps({"yt-owner": payload}), encoding="utf-8")
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))

    ensure_default_resolvers()

    assert resolve_secret("local-secret://yt-owner") == payload


def test_file_backed_local_secret_rereads_file_on_each_resolve(monkeypatch, tmp_path) -> None:
    _stub_gcp_resolver(monkeypatch)
    secrets_file = tmp_path / "connector-secrets.json"
    secrets_file.write_text(json.dumps({"yt-owner": "payload-v1"}), encoding="utf-8")
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    ensure_default_resolvers()

    first = resolve_secret("local-secret://yt-owner")
    secrets_file.write_text(json.dumps({"yt-owner": "payload-v2"}), encoding="utf-8")
    second = resolve_secret("local-secret://yt-owner")

    assert first == "payload-v1"
    assert second == "payload-v2"


def test_file_backed_local_secret_fails_closed_on_missing_file(monkeypatch, tmp_path) -> None:
    _stub_gcp_resolver(monkeypatch)
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(tmp_path / "does-not-exist.json"))
    ensure_default_resolvers()

    with pytest.raises(LocalSecretsFileError) as ctx:
        resolve_secret("local-secret://yt-owner")
    assert ctx.value.path.endswith("does-not-exist.json")
    assert isinstance(ctx.value.inner, OSError)


def test_file_backed_local_secret_fails_closed_on_invalid_utf8(monkeypatch, tmp_path) -> None:
    _stub_gcp_resolver(monkeypatch)
    secrets_file = tmp_path / "connector-secrets.json"
    secrets_file.write_bytes(b'{"yt-owner": "\xff\xfe-invalid-utf8"}')
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    ensure_default_resolvers()

    with pytest.raises(LocalSecretsFileError) as ctx:
        resolve_secret("local-secret://yt-owner")
    assert isinstance(ctx.value.inner, UnicodeDecodeError)


def test_file_backed_local_secret_fails_closed_on_malformed_json(monkeypatch, tmp_path) -> None:
    _stub_gcp_resolver(monkeypatch)
    secrets_file = tmp_path / "connector-secrets.json"
    secrets_file.write_text("{not-json", encoding="utf-8")
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    ensure_default_resolvers()

    with pytest.raises(LocalSecretsFileError):
        resolve_secret("local-secret://yt-owner")


@pytest.mark.parametrize(
    "content",
    [
        '["not", "an", "object"]',  # top-level array
        '{"yt-owner": 123}',  # non-string payload
        '{"yt-owner": {"nested": "object"}}',  # nested object payload
        '{"yt-owner": null}',  # null payload
    ],
)
def test_file_backed_local_secret_fails_closed_on_non_string_mapping(
    monkeypatch, tmp_path, content
) -> None:
    _stub_gcp_resolver(monkeypatch)
    secrets_file = tmp_path / "connector-secrets.json"
    secrets_file.write_text(content, encoding="utf-8")
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    ensure_default_resolvers()

    with pytest.raises(LocalSecretsFileError):
        resolve_secret("local-secret://yt-owner")


def test_file_backed_local_secret_unknown_key_raises_secret_not_found(
    monkeypatch, tmp_path
) -> None:
    _stub_gcp_resolver(monkeypatch)
    secrets_file = tmp_path / "connector-secrets.json"
    secrets_file.write_text(json.dumps({"other-owner": "payload"}), encoding="utf-8")
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    ensure_default_resolvers()

    with pytest.raises(SecretNotFoundError):
        resolve_secret("local-secret://yt-owner")


def test_registered_local_secret_test_resolver_takes_precedence_over_file_lane(
    monkeypatch, tmp_path
) -> None:
    """An explicitly registered local-secret resolver (tests/CLI smoke) is not
    replaced by the file-backed lane even when the env var is set."""
    _stub_gcp_resolver(monkeypatch)
    secrets_file = tmp_path / "connector-secrets.json"
    secrets_file.write_text(json.dumps({"yt-owner": "file-payload"}), encoding="utf-8")
    monkeypatch.setenv("UMS_CONNECTOR_LOCAL_SECRETS_FILE", str(secrets_file))
    stub = _StubResolver(payload="explicit-payload")
    register_resolver(scheme="local-secret", resolver=stub)

    ensure_default_resolvers()

    assert resolve_secret("local-secret://yt-owner") == "explicit-payload"


@pytest.mark.parametrize(
    "ref",
    [
        "",  # empty
        "no-scheme",  # missing ://
        "gcp-secret-manager:/",  # malformed delimiter
        "://no-scheme-name",  # empty scheme
    ],
)
def test_resolve_secret_raises_for_malformed_uri(ref: str) -> None:
    with pytest.raises(MalformedSecretUriError):
        resolve_secret(ref)
