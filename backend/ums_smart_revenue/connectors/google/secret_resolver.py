"""Secret resolver dispatch.

resolve_secret(ref) parses the URI scheme and dispatches to a registered
SecretResolver. Implemented schemes (registered at app/test boot):
- gcp-secret-manager:// -> GcpSecretManagerResolver (B2.1)
- local-secret://       -> LocalSecretResolver (B2.1, test only)

Other ORM-accepted prefixes (aws-secretsmanager://, secret-manager://,
vault://, kms://, azure-keyvault://) are intentionally unregistered until a
future credential-lifecycle PR. They raise UnsupportedSecretSchemeError so
B2 fails closed instead of silently dropping the secret.
"""
from __future__ import annotations

from typing import Protocol

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretUriError,
    UnsupportedSecretSchemeError,
)


class SecretResolver(Protocol):
    def resolve(self, ref: str) -> str:
        """Return the secret payload as a string. Raise SecretNotFoundError /
        SecretFetchError on backend failure."""


_REGISTRY: dict[str, SecretResolver] = {}


def register_resolver(*, scheme: str, resolver: SecretResolver) -> None:
    _REGISTRY[scheme] = resolver


def _parse_scheme(ref: str) -> str:
    if not ref or "://" not in ref:
        raise MalformedSecretUriError(ref=ref)
    scheme, _, rest = ref.partition("://")
    if not scheme or not rest:
        raise MalformedSecretUriError(ref=ref)
    return scheme


def resolve_secret(ref: str) -> str:
    scheme = _parse_scheme(ref)
    resolver = _REGISTRY.get(scheme)
    if resolver is None:
        raise UnsupportedSecretSchemeError(scheme=scheme)
    return resolver.resolve(ref)
