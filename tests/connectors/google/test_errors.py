"""B2 error hierarchy tests.

Every B2 error subclasses GoogleConnectorError so the orchestrator can catch
the whole family in one except clause. Subclasses are distinguishable by
isinstance. As the B2 slices land (B2.1 secrets/oauth, B2.2 blob storage,
B2.3/B2.4 to follow), new errors are appended to the family roster below.
"""
from __future__ import annotations

from ums_smart_revenue.connectors.google.errors import (
    BlobChecksumMismatchError,
    BlobUploadError,
    CredentialNotFoundError,
    GoogleApiAuthError,
    GoogleApiClientError,
    GoogleApiRateLimitError,
    GoogleApiResponseError,
    GoogleApiServerError,
    GoogleConnectorError,
    InactiveCredentialError,
    MalformedReportMonthError,
    MalformedSecretPayloadError,
    MalformedSecretUriError,
    OAuthRefreshError,
    RawFileAlreadyParsedError,
    RawFileLifecycleError,
    SecretFetchError,
    SecretNotFoundError,
    UnsupportedReportTypeError,
    UnsupportedSecretSchemeError,
)


def test_all_b2_errors_subclass_google_connector_error() -> None:
    for cls in (
        UnsupportedSecretSchemeError,
        MalformedSecretUriError,
        SecretNotFoundError,
        SecretFetchError,
        MalformedSecretPayloadError,
        OAuthRefreshError,
        BlobUploadError,
        BlobChecksumMismatchError,
        RawFileLifecycleError,
        RawFileAlreadyParsedError,
        CredentialNotFoundError,
        InactiveCredentialError,
        GoogleApiAuthError,
        GoogleApiClientError,
        GoogleApiRateLimitError,
        GoogleApiServerError,
        GoogleApiResponseError,
        UnsupportedReportTypeError,
        MalformedReportMonthError,
    ):
        assert issubclass(cls, GoogleConnectorError), cls.__name__


def test_unsupported_secret_scheme_carries_scheme() -> None:
    err = UnsupportedSecretSchemeError(scheme="aws-secretsmanager")
    assert err.scheme == "aws-secretsmanager"
    assert "aws-secretsmanager" in str(err)


def test_secret_not_found_carries_ref() -> None:
    ref = "gcp-secret-manager://projects/x/secrets/y/versions/latest"
    err = SecretNotFoundError(ref=ref)
    assert err.ref == ref
    assert ref not in str(err)
    assert "projects/x/secrets/y" not in str(err)
    assert "gcp-secret-manager" in str(err)


def test_malformed_secret_uri_redacts_secret_resource_name() -> None:
    ref = "gcp-secret-manager://projects/x/secrets/y/versions/latest"
    err = MalformedSecretUriError(ref=ref)
    assert err.ref == ref
    assert ref not in str(err)
    assert "projects/x/secrets/y" not in str(err)
    assert "gcp-secret-manager" in str(err)


def test_secret_fetch_error_redacts_secret_resource_name() -> None:
    ref = "gcp-secret-manager://projects/x/secrets/y/versions/latest"
    inner = RuntimeError("permission denied for projects/x/secrets/y")

    err = SecretFetchError(ref=ref, inner=inner)

    assert err.ref == ref
    assert err.inner is inner
    assert ref not in str(err)
    assert "projects/x/secrets/y" not in str(err)
    assert "gcp-secret-manager" in str(err)
    assert "RuntimeError" in str(err)


def test_oauth_refresh_carries_inner_class_name() -> None:
    inner = RuntimeError("token revoked")
    err = OAuthRefreshError(inner=inner)
    assert "RuntimeError" in str(err)
    assert err.inner is inner
