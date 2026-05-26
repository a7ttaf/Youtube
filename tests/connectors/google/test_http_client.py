"""GoogleHttpClient happy-path tests.

The client invokes credentials.before_request(...) on every request so
google-auth handles refresh; it parses JSON responses and returns the
decoded dict.
"""
from __future__ import annotations

import json

import httpx

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
