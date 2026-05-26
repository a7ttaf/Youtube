"""GoogleHttpClient happy-path tests.

The client invokes credentials.before_request(...) on every request so
google-auth handles refresh; it parses JSON responses and returns the
decoded dict.
"""
from __future__ import annotations

import json

import httpx
import pytest

from ums_smart_revenue.connectors.google.errors import (
    GoogleApiAuthError,
    GoogleApiClientError,
    GoogleApiRateLimitError,
    GoogleApiServerError,
)
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient


def test_request_invokes_before_request_and_parses_json(mock_credentials) -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        return httpx.Response(
            200, content=json.dumps({"jobs": [{"id": "job-1"}]}).encode()
        )

    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    body = client.request(method="GET", url="https://example.com/v1/jobs")
    assert body == {"jobs": [{"id": "job-1"}]}
    assert captured["auth"] == "Bearer fake-bearer"


def test_request_passes_query_params(mock_credentials) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["query"] = dict(request.url.params)
        return httpx.Response(200, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client.request(
        method="GET", url="https://example.com/v1/x",
        params={"pageToken": "abc", "limit": "10"},
    )
    assert captured["query"] == {"pageToken": "abc", "limit": "10"}


@pytest.mark.parametrize("status", [400, 404, 422])
def test_4xx_client_errors_no_retry(mock_credentials, status) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiClientError) as ctx:
        client.request(method="GET", url="https://example.com/x")
    assert ctx.value.status == status
    assert len(calls) == 1  # no retry


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_no_retry(mock_credentials, status) -> None:
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(status, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
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

    def handler(request):
        calls.append(1)
        return httpx.Response(429, content=b"{}")

    client = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
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
    seq = iter([
        httpx.Response(429, headers={"Retry-After": "3"}, content=b"{}"),
        httpx.Response(200, content=b"{}"),
    ])
    client = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(lambda r: next(seq)),
    )
    client.request(method="GET", url="https://example.com/x")
    assert sleeps == [3.0]


def test_5xx_retries_then_raises(mock_credentials, monkeypatch) -> None:
    monkeypatch.setattr(
        "ums_smart_revenue.connectors.google.http_client.time.sleep",
        lambda _: None,
    )
    calls = []
    def handler(request):
        calls.append(1)
        return httpx.Response(503, content=b"{}")
    client = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    with pytest.raises(GoogleApiServerError):
        client.request(method="GET", url="https://example.com/x")
    assert len(calls) == 4
