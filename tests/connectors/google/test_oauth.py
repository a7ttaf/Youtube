"""google-auth refresh wrapper tests.

build_credentials_from_payload(payload_json) parses the resolved secret string
and constructs google.oauth2.credentials.Credentials; missing fields or bad
JSON -> MalformedSecretPayloadError. refresh_credentials(creds) calls
creds.refresh(Request()); RefreshError -> OAuthRefreshError.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError

from ums_smart_revenue.connectors.google.errors import (
    MalformedSecretPayloadError,
    OAuthRefreshError,
)
from ums_smart_revenue.connectors.google.oauth import (
    build_credentials_from_payload,
    refresh_credentials,
)

_VALID_PAYLOAD = json.dumps(
    {
        "refresh_token": "rt-abc",
        "client_id": "cid-abc",
        "client_secret": "secret-abc",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
)


def test_build_credentials_returns_credentials_with_fields() -> None:
    creds = build_credentials_from_payload(_VALID_PAYLOAD)
    assert creds.refresh_token == "rt-abc"
    assert creds.client_id == "cid-abc"
    assert creds.client_secret == "secret-abc"
    assert creds.token_uri == "https://oauth2.googleapis.com/token"


@pytest.mark.parametrize("bad_json", ["", "not-json", "{", "[]"])
def test_build_credentials_rejects_non_object_json(bad_json: str) -> None:
    with pytest.raises(MalformedSecretPayloadError):
        build_credentials_from_payload(bad_json)


@pytest.mark.parametrize(
    "missing_field",
    ["refresh_token", "client_id", "client_secret", "token_uri"],
)
def test_build_credentials_rejects_missing_field(missing_field: str) -> None:
    payload = json.loads(_VALID_PAYLOAD)
    payload.pop(missing_field)
    with pytest.raises(MalformedSecretPayloadError) as ctx:
        build_credentials_from_payload(json.dumps(payload))
    assert missing_field in ctx.value.detail


def test_build_credentials_accepts_scopes_list() -> None:
    payload = json.loads(_VALID_PAYLOAD)
    payload["scopes"] = [
        "https://www.googleapis.com/auth/yt-analytics.readonly",
        "https://www.googleapis.com/auth/youtubepartner",
    ]

    creds = build_credentials_from_payload(json.dumps(payload))

    assert creds.scopes == payload["scopes"]


@pytest.mark.parametrize(
    "scopes",
    [
        "https://www.googleapis.com/auth/youtubepartner",
        {"scope": "https://www.googleapis.com/auth/youtubepartner"},
        ["https://www.googleapis.com/auth/youtubepartner", ""],
        ["https://www.googleapis.com/auth/youtubepartner", 123],
    ],
)
def test_build_credentials_rejects_malformed_scopes(scopes: object) -> None:
    payload = json.loads(_VALID_PAYLOAD)
    payload["scopes"] = scopes

    with pytest.raises(MalformedSecretPayloadError, match="scopes"):
        build_credentials_from_payload(json.dumps(payload))


def test_refresh_credentials_calls_refresh() -> None:
    creds = MagicMock()
    with patch("ums_smart_revenue.connectors.google.oauth.Request") as request_cls:
        refresh_credentials(creds)
    creds.refresh.assert_called_once()
    request_cls.assert_called_once()


def test_refresh_credentials_wraps_refresh_error() -> None:
    creds = MagicMock()
    inner = RefreshError("token revoked")
    creds.refresh.side_effect = inner
    with (
        patch("ums_smart_revenue.connectors.google.oauth.Request"),
        pytest.raises(OAuthRefreshError) as ctx,
    ):
        refresh_credentials(creds)
    assert ctx.value.inner is inner
