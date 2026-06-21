"""GCP Secret Manager resolver.

URI shape:
    gcp-secret-manager://projects/{project}/secrets/{name}/versions/{version}
    secret-manager://projects/{project}/secrets/{name}/versions/{version}
where {version} is either an integer or 'latest'. The path after :// is the
exact name expected by SecretManagerServiceClient.access_secret_version.
"""

from __future__ import annotations

import re

from google.api_core import exceptions as gcp_exceptions

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretPayloadError,
    MalformedSecretUriError,
    SecretFetchError,
    SecretNotFoundError,
)

_NAME_PATTERN = re.compile(r"^projects/[^/]+/secrets/[^/]+/versions/[^/]+$")
_SUPPORTED_PREFIXES = ("gcp-secret-manager://", "secret-manager://")


class GcpSecretManagerResolver:
    """Resolver for GCP Secret Manager URI schemes.

    Inject the client at construction time (B2.1 wiring uses a real
    SecretManagerServiceClient; tests use a mock).
    """

    def __init__(self, *, client: object | None = None) -> None:
        self._client = client

    def resolve(self, ref: str) -> str:
        name = _name_from_ref(ref)
        if not _NAME_PATTERN.match(name):
            raise MalformedSecretUriError(ref=ref)
        try:
            response = _access_secret_version(self._get_client(ref=ref), name=name, ref=ref)
        except gcp_exceptions.NotFound as exc:
            raise SecretNotFoundError(ref=ref) from exc
        except gcp_exceptions.GoogleAPICallError as exc:
            raise SecretFetchError(ref=ref, inner=exc) from exc
        payload = _secret_payload_bytes(response)
        try:
            return payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MalformedSecretPayloadError(detail="payload is not utf-8") from exc

    def _get_client(self, *, ref: str) -> object:
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
            prefix_length = len(prefix)
            return ref[prefix_length:]
    raise MalformedSecretUriError(ref=ref)


def _access_secret_version(client: object, *, name: str, ref: str) -> object:
    """Call Secret Manager with a runtime check for injected test/client doubles."""
    access_secret_version = getattr(client, "access_secret_version", None)
    if not callable(access_secret_version):
        error = TypeError("secret manager client missing access_secret_version")
        raise SecretFetchError(ref=ref, inner=error) from error
    return access_secret_version(request={"name": name})


def _secret_payload_bytes(response: object) -> bytes:
    """Extract and validate the byte payload from a Secret Manager response."""
    payload_container = getattr(response, "payload", None)
    payload = getattr(payload_container, "data", None)
    if not isinstance(payload, bytes):
        raise MalformedSecretPayloadError(detail="payload data is not bytes")
    return payload
