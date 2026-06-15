"""Test/dev secret resolver backed by an injected mapping.

URI shape: local-secret://{name} where {name} is a key in the mapping.
Never registered in production; production registers only gcp-secret-manager://.
"""

from __future__ import annotations

from collections.abc import Mapping

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretUriError,
    SecretNotFoundError,
)


class LocalSecretResolver:
    def __init__(self, *, mapping: Mapping[str, str]) -> None:
        self._mapping = dict(mapping)

    def resolve(self, ref: str) -> str:
        if not ref.startswith("local-secret://"):
            raise MalformedSecretUriError(ref=ref)
        key = ref[len("local-secret://") :]
        if not key:
            raise MalformedSecretUriError(ref=ref)
        try:
            return self._mapping[key]
        except KeyError as exc:
            raise SecretNotFoundError(ref=ref) from exc
