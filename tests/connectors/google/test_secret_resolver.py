"""Secret resolver dispatch tests.

A dispatcher maps a URI scheme (e.g., 'gcp-secret-manager') to a resolver
implementation. Unknown / unimplemented schemes raise
UnsupportedSecretSchemeError; ORM-accepted prefixes that aren't implemented
(aws-secretsmanager://, vault://, kms://, azure-keyvault://)
are intentionally unknown until a future credential-lifecycle PR.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretUriError,
    ResolverAlreadyRegisteredError,
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


@pytest.mark.parametrize(
    "ref",
    [
        "",                       # empty
        "no-scheme",              # missing ://
        "gcp-secret-manager:/",   # malformed delimiter
        "://no-scheme-name",      # empty scheme
    ],
)
def test_resolve_secret_raises_for_malformed_uri(ref: str) -> None:
    with pytest.raises(MalformedSecretUriError):
        resolve_secret(ref)
