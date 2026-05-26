"""Secret resolver dispatch tests.

A dispatcher maps a URI scheme (e.g., 'gcp-secret-manager') to a resolver
implementation. Unknown / unimplemented schemes raise
UnsupportedSecretSchemeError; ORM-accepted prefixes that aren't implemented
(aws-secretsmanager://, secret-manager://, vault://, kms://, azure-keyvault://)
are intentionally unknown until a future credential-lifecycle PR.
"""
from __future__ import annotations

import pytest

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretUriError,
    UnsupportedSecretSchemeError,
)
from ums_smart_revenue.connectors.google.secret_resolver import (
    register_resolver,
    resolve_secret,
)


@pytest.fixture(autouse=True)
def _reset_registry():
    from ums_smart_revenue.connectors.google import secret_resolver as sr
    snapshot = dict(sr._REGISTRY)
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
