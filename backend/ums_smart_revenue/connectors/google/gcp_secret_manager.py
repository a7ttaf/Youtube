"""GCP Secret Manager resolver.

URI shape:
    gcp-secret-manager://projects/{project}/secrets/{name}/versions/{version}
    secret-manager://projects/{project}/secrets/{name}/versions/{version}
where {version} is either an integer or 'latest'. The path after :// is the
exact name expected by SecretManagerServiceClient.access_secret_version.
"""

from __future__ import annotations

import re
from typing import Protocol

from google.api_core import exceptions as gcp_exceptions

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretPayloadError,
    MalformedSecretUriError,
    SecretFetchError,
    SecretNotFoundError,
)

_NAME_PATTERN = re.compile(r"^projects/[^/]+/secrets/[^/]+/versions/[^/]+$")
_SUPPORTED_PREFIXES = ("gcp-secret-manager://", "secret-manager://")


class _SecretPayload(Protocol):
    data: bytes


class _SecretVersionResponse(Protocol):
    payload: _SecretPayload


class _SecretManagerClient(Protocol):
    def access_secret_version(self, *, request: dict) -> _SecretVersionResponse: ...


class GcpSecretManagerResolver:
    """Resolver for GCP Secret Manager URI schemes.

    Inject the client at construction time (B2.1 wiring uses a real
    SecretManagerServiceClient; tests use a mock).
    """

    def __init__(self, *, client: _SecretManagerClient | None = None) -> None:
        self._client = client

    def resolve(self, ref: str) -> str:
        name = _name_from_ref(ref)
        if not _NAME_PATTERN.match(name):
            raise MalformedSecretUriError(ref=ref)
        try:
            response = self._get_client(ref=ref).access_secret_version(request={"name": name})
        except gcp_exceptions.NotFound as exc:
            raise SecretNotFoundError(ref=ref) from exc
        except gcp_exceptions.GoogleAPICallError as exc:
            raise SecretFetchError(ref=ref, inner=exc) from exc
        payload: bytes = response.payload.data
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedSecretPayloadError(detail="payload is not utf-8") from exc

    def _get_client(self, *, ref: str) -> _SecretManagerClient:
        # ============================================================================
        # Purpose: Lazily construct the GCP Secret Manager client and preserve
        #          construction/import/auth failures behind the connector's typed
        #          SecretFetchError boundary.
        # Database/ORM: None.
        # Standards: No secret payloads in error text; typed connector error for
        #            upstream environment, credential, import, and API-client setup
        #            failures.
        # Blast Radius: Secret resolution only. No graph projection impact detected.
        # Connections:
        #   - File: backend/ums_smart_revenue/connectors/google/secret_resolver.py ->
        #     dispatches gcp-secret-manager:// refs to this resolver.
        #   - File: backend/ums_smart_revenue/connectors/google/errors.py ->
        #     SecretFetchError stores the original exception for operator logs/tests.
        # ============================================================================
        if self._client is None:
            try:
                from google.cloud import secretmanager

                self._client = secretmanager.SecretManagerServiceClient()
            except Exception as exc:
                raise SecretFetchError(ref=ref, inner=exc) from exc
        return self._client


def _name_from_ref(ref: str) -> str:
    for prefix in _SUPPORTED_PREFIXES:
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    raise MalformedSecretUriError(ref=ref)
