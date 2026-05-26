"""B2.1 error hierarchy tests.

Every B2 error subclasses GoogleConnectorError so the orchestrator can catch
the whole family in one except clause. Subclasses are distinguishable by
isinstance.
"""
from __future__ import annotations

from ums_smart_revenue.connectors.google.errors import (
    GoogleConnectorError,
    MalformedSecretPayloadError,
    MalformedSecretUriError,
    OAuthRefreshError,
    SecretFetchError,
    SecretNotFoundError,
    UnsupportedSecretSchemeError,
)


def test_all_b21_errors_subclass_google_connector_error() -> None:
    for cls in (
        UnsupportedSecretSchemeError,
        MalformedSecretUriError,
        SecretNotFoundError,
        SecretFetchError,
        MalformedSecretPayloadError,
        OAuthRefreshError,
    ):
        assert issubclass(cls, GoogleConnectorError), cls.__name__


def test_unsupported_secret_scheme_carries_scheme() -> None:
    err = UnsupportedSecretSchemeError(scheme="aws-secretsmanager")
    assert err.scheme == "aws-secretsmanager"
    assert "aws-secretsmanager" in str(err)


def test_secret_not_found_carries_ref() -> None:
    err = SecretNotFoundError(ref="gcp-secret-manager://projects/x/secrets/y/versions/latest")
    assert "y" in str(err)


def test_oauth_refresh_carries_inner_class_name() -> None:
    inner = RuntimeError("token revoked")
    err = OAuthRefreshError(inner=inner)
    assert "RuntimeError" in str(err)
    assert err.inner is inner
