"""local-secret:// resolver - test/dev backend backed by an injected mapping."""

from __future__ import annotations

import pytest

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretUriError,
    SecretNotFoundError,
)
from ums_smart_revenue.connectors.google.local_secret_resolver import (
    LocalSecretResolver,
)


def test_resolve_returns_payload_from_mapping() -> None:
    resolver = LocalSecretResolver(mapping={"yt-creds": '{"refresh_token": "rt"}'})
    out = resolver.resolve("local-secret://yt-creds")
    assert out == '{"refresh_token": "rt"}'


def test_resolve_raises_not_found_for_unknown_key() -> None:
    resolver = LocalSecretResolver(mapping={})
    with pytest.raises(SecretNotFoundError):
        resolver.resolve("local-secret://missing")


@pytest.mark.parametrize(
    "ref",
    [
        "local-secret://",  # empty key
        "local-secret:/yt-creds",  # missing one /
        "not-local://yt-creds",  # wrong scheme
    ],
)
def test_resolve_raises_malformed_uri(ref: str) -> None:
    resolver = LocalSecretResolver(mapping={"yt-creds": "x"})
    with pytest.raises(MalformedSecretUriError):
        resolver.resolve(ref)
