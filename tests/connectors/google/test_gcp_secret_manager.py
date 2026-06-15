"""GCP Secret Manager resolver tests.

The resolver parses gcp-secret-manager://projects/{p}/secrets/{n}/versions/{v}
into a fully-qualified name, calls SecretManagerServiceClient.access_secret_version,
and returns the decoded payload. NotFound -> SecretNotFoundError; any other
google-cloud error -> SecretFetchError.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from google.api_core import exceptions as gcp_exceptions
from google.cloud import secretmanager

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretPayloadError,
    MalformedSecretUriError,
    SecretFetchError,
    SecretNotFoundError,
)
from ums_smart_revenue.connectors.google.gcp_secret_manager import (
    GcpSecretManagerResolver,
)


def _make_client_returning(payload: bytes) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.payload.data = payload
    client.access_secret_version.return_value = response
    return client


def test_resolve_returns_decoded_payload() -> None:
    client = _make_client_returning(b'{"refresh_token": "rt"}')
    resolver = GcpSecretManagerResolver(client=client)
    out = resolver.resolve("gcp-secret-manager://projects/my-proj/secrets/yt-creds/versions/latest")
    assert out == '{"refresh_token": "rt"}'
    client.access_secret_version.assert_called_once_with(
        request={"name": "projects/my-proj/secrets/yt-creds/versions/latest"}
    )


def test_resolve_accepts_generic_secret_manager_alias() -> None:
    client = _make_client_returning(b'{"refresh_token": "rt"}')
    resolver = GcpSecretManagerResolver(client=client)

    out = resolver.resolve("secret-manager://projects/my-proj/secrets/yt-creds/versions/latest")

    assert out == '{"refresh_token": "rt"}'
    client.access_secret_version.assert_called_once_with(
        request={"name": "projects/my-proj/secrets/yt-creds/versions/latest"}
    )


def test_resolve_wraps_non_utf8_payload_as_malformed_payload() -> None:
    client = _make_client_returning(b"\xff\xfe\x00")
    resolver = GcpSecretManagerResolver(client=client)

    with pytest.raises(MalformedSecretPayloadError) as ctx:
        resolver.resolve("gcp-secret-manager://projects/my-proj/secrets/yt-creds/versions/latest")

    assert "utf-8" in ctx.value.detail


def test_resolve_raises_not_found_on_gcp_404() -> None:
    client = MagicMock()
    client.access_secret_version.side_effect = gcp_exceptions.NotFound("missing")
    resolver = GcpSecretManagerResolver(client=client)
    with pytest.raises(SecretNotFoundError):
        resolver.resolve("gcp-secret-manager://projects/x/secrets/y/versions/1")


def test_resolve_wraps_other_gcp_errors_as_fetch_error() -> None:
    client = MagicMock()
    inner = gcp_exceptions.PermissionDenied("denied")
    client.access_secret_version.side_effect = inner
    resolver = GcpSecretManagerResolver(client=client)
    with pytest.raises(SecretFetchError) as ctx:
        resolver.resolve("gcp-secret-manager://projects/x/secrets/y/versions/latest")
    assert ctx.value.inner is inner


def test_resolve_wraps_client_construction_failure_as_fetch_error(monkeypatch) -> None:
    ref = "gcp-secret-manager://projects/x/secrets/y/versions/latest"
    inner = RuntimeError("ADC unavailable")

    def raise_on_construct():
        raise inner

    monkeypatch.setattr(secretmanager, "SecretManagerServiceClient", raise_on_construct)
    resolver = GcpSecretManagerResolver()

    with pytest.raises(SecretFetchError) as ctx:
        resolver.resolve(ref)

    assert ctx.value.ref == ref
    assert ctx.value.inner is inner


@pytest.mark.parametrize(
    "ref",
    [
        "gcp-secret-manager://x",  # not projects/.../secrets/.../versions/...
        "gcp-secret-manager://projects/p/secrets/n",  # missing /versions/
        "gcp-secret-manager://projects//secrets/n/versions/1",  # empty project
    ],
)
def test_resolve_raises_malformed_uri(ref: str) -> None:
    resolver = GcpSecretManagerResolver(client=MagicMock())
    with pytest.raises(MalformedSecretUriError):
        resolver.resolve(ref)
