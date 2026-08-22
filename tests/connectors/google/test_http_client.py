# ============================================================================
# Purpose: GoogleHttpClient request/retry/auth behavior tests.
# Database/ORM: None.
# Standards: httpx2 imported as httpx for transport doubles.
# Blast Radius: Test coverage for connector HTTP transport only.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/http_client.py -> SUT.
# ============================================================================
"""GoogleHttpClient happy-path tests.

The client invokes credentials.before_request(...) on every request so
google-auth handles refresh; it parses JSON responses and returns the
decoded dict.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx2 as httpx
import pytest
from google.auth.exceptions import GoogleAuthError, RefreshError

from ums_smart_revenue.connectors.google.errors import (
    GoogleApiAuthError,
    GoogleApiClientError,
    GoogleApiRateLimitError,
    GoogleApiResponseError,
    GoogleApiServerError,
    OAuthRefreshError,
)
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient


def _next_response(responses: Iterator[httpx.Response]) -> httpx.Response:
    try:
        return next(responses)
    except StopIteration as exc:
        raise AssertionError("unexpected extra HTTP request") from exc


def test_request_invokes_before_request_and_parses_json(mock_credentials) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, content=json.dumps({"jobs": [{"id": "job-1"}]}).encode())

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    body = client.request(method="GET", url="https://example.com/v1/jobs")
    assert body == {"jobs": [{"id": "job-1"}]}
    assert captured["auth"] == "Bearer fake-bearer"


def test_request_passes_google_auth_transport_request_to_credentials() -> None:
    seen: dict[str, object] = {}

    class _Credentials:
        @staticmethod
        def before_request(request, method, url, headers) -> None:
            seen["request"] = request
            seen["method"] = method
            seen["url"] = url
            headers["Authorization"] = "Bearer captured"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}")

    client = GoogleHttpClient(
        credentials=_Credentials(),
        transport=httpx.MockTransport(handler),
    )

    client.request(method="GET", url="https://example.com/v1/jobs")

    assert seen["request"] is not None
    assert seen["method"] == "GET"
    assert seen["url"] == "https://example.com/v1/jobs"


def test_before_request_refresh_error_is_typed_oauth_error() -> None:
    inner = RefreshError("token revoked")

    class _Credentials:
        @staticmethod
        def before_request(request, method, url, headers) -> None:
            raise inner

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP request must not run after auth refresh failure")

    client = GoogleHttpClient(
        credentials=_Credentials(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(OAuthRefreshError) as ctx:
        client.request(method="GET", url="https://example.com/v1/jobs")

    assert ctx.value.inner is inner


def test_before_request_google_auth_error_is_typed_oauth_error() -> None:
    inner = GoogleAuthError("token endpoint transport failed")

    class _Credentials:
        @staticmethod
        def before_request(request, method, url, headers) -> None:
            raise inner

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP request must not run after auth refresh failure")

    client = GoogleHttpClient(
        credentials=_Credentials(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(OAuthRefreshError) as ctx:
        client.request(method="GET", url="https://example.com/v1/jobs")

    assert ctx.value.inner is inner


def test_request_passes_query_params(mock_credentials) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = dict(request.url.params)
        return httpx.Response(200, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client.request(
        method="GET",
        url="https://example.com/v1/x",
        params={"pageToken": "abc", "limit": "10"},
    )
    assert captured["query"] == {"pageToken": "abc", "limit": "10"}


@pytest.mark.parametrize("status", [400, 404, 422])
def test_4xx_client_errors_no_retry(mock_credentials, status) -> None:
    calls = []

    def handler(_request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiClientError) as ctx:
        client.request(method="GET", url="https://example.com/x")
    assert ctx.value.status == status
    assert len(calls) == 1  # no retry


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_no_retry(mock_credentials, status) -> None:
    calls = []

    def handler(_request):
        calls.append(1)
        return httpx.Response(status, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiAuthError):
        client.request(method="GET", url="https://example.com/x")
    assert len(calls) == 1


def test_429_retries_then_raises(mock_credentials, monkeypatch) -> None:
    monkeypatch.setattr(
        "ums_smart_revenue.connectors.google.http_client.time.sleep",
        lambda _: None,
    )
    calls = []

    def handler(_request):
        calls.append(1)
        return httpx.Response(429, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiRateLimitError) as ctx:
        client.request(method="GET", url="https://example.com/x")
    assert ctx.value.attempts == 4
    assert len(calls) == 4


def test_429_honors_retry_after(mock_credentials, monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "ums_smart_revenue.connectors.google.http_client.time.sleep",
        sleeps.append,
    )
    seq = iter(
        [
            httpx.Response(429, headers={"Retry-After": "3"}, content=b"{}"),
            httpx.Response(200, content=b"{}"),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return _next_response(seq)

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client.request(method="GET", url="https://example.com/x")
    assert sleeps == [3.0]


def test_429_without_retry_after_uses_backoff_schedule(mock_credentials, monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "ums_smart_revenue.connectors.google.http_client.time.sleep",
        sleeps.append,
    )
    calls = []

    def handler(_request):
        calls.append(1)
        return httpx.Response(429, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiRateLimitError):
        client.request(method="GET", url="https://example.com/x")
    assert sleeps == [1.0, 2.0, 4.0]  # 3 backoff sleeps before the 4th attempt raises
    assert len(calls) == 4


def test_5xx_retries_then_raises(mock_credentials, monkeypatch) -> None:
    monkeypatch.setattr(
        "ums_smart_revenue.connectors.google.http_client.time.sleep",
        lambda _: None,
    )
    calls = []

    def handler(_request):
        calls.append(1)
        return httpx.Response(503, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiServerError) as ctx:
        client.request(method="GET", url="https://example.com/x")
    assert len(calls) == 4
    assert ctx.value.attempts == 4
    assert ctx.value.status == 503


def test_non_json_response_raises_response_error(mock_credentials) -> None:
    def handler(_request):
        return httpx.Response(200, content=b"<html>not json</html>")

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiResponseError) as ctx:
        client.request(method="GET", url="https://example.com/x")
    assert "json" in ctx.value.reason.lower()


def test_non_object_json_raises_response_error(mock_credentials) -> None:
    def handler(_request):
        return httpx.Response(200, content=b"[1, 2, 3]")

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiResponseError):
        client.request(method="GET", url="https://example.com/x")


def test_fetch_bytes_retries_5xx_via_shared_helper(mock_credentials, monkeypatch) -> None:
    monkeypatch.setattr(
        "ums_smart_revenue.connectors.google.http_client.time.sleep",
        lambda _: None,
    )
    seq = iter(
        [
            httpx.Response(503, content=b""),
            httpx.Response(200, content=b"csv-bytes"),
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return _next_response(seq)

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    out = client.fetch_bytes(url="https://x/y")
    assert out == b"csv-bytes"


def test_remote_protocol_error_uses_connect_retry_budget(mock_credentials, monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "ums_smart_revenue.connectors.google.http_client.time.sleep",
        sleeps.append,
    )
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.RemoteProtocolError("server disconnected")
        return httpx.Response(200, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )

    client.request(method="GET", url="https://example.com/x")

    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]


def test_proxy_error_uses_connect_retry_budget(mock_credentials, monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "ums_smart_revenue.connectors.google.http_client.time.sleep",
        sleeps.append,
    )
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ProxyError("proxy failed")
        return httpx.Response(200, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )

    client.request(method="GET", url="https://example.com/x")

    assert calls["n"] == 3
    assert sleeps == [1.0, 2.0]
