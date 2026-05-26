"""GCP Secret Manager resolver.

URI shape:
    gcp-secret-manager://projects/{project}/secrets/{name}/versions/{version}
where {version} is either an integer or 'latest'. The path after :// is the
exact name expected by SecretManagerServiceClient.access_secret_version.
"""
from __future__ import annotations

import re
from typing import Protocol

from google.api_core import exceptions as gcp_exceptions

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretUriError,
    SecretFetchError,
    SecretNotFoundError,
)

_NAME_PATTERN = re.compile(
    r"^projects/[^/]+/secrets/[^/]+/versions/[^/]+$"
)


class _SecretManagerClient(Protocol):
    def access_secret_version(self, *, request: dict) -> object: ...


class GcpSecretManagerResolver:
    """Resolver for the gcp-secret-manager:// scheme.

    Inject the client at construction time (B2.1 wiring uses a real
    SecretManagerServiceClient; tests use a mock).
    """

    def __init__(self, *, client: _SecretManagerClient) -> None:
        self._client = client

    def resolve(self, ref: str) -> str:
        if not ref.startswith("gcp-secret-manager://"):
            raise MalformedSecretUriError(ref=ref)
        name = ref[len("gcp-secret-manager://") :]
        if not _NAME_PATTERN.match(name):
            raise MalformedSecretUriError(ref=ref)
        try:
            response = self._client.access_secret_version(request={"name": name})
        except gcp_exceptions.NotFound as exc:
            raise SecretNotFoundError(ref=ref) from exc
        except gcp_exceptions.GoogleAPICallError as exc:
            raise SecretFetchError(ref=ref, inner=exc) from exc
        payload: bytes = response.payload.data
        return payload.decode("utf-8")
