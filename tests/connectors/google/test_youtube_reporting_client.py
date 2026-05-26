"""YouTube Reporting client tests (spec §5.4)."""
from __future__ import annotations

import httpx
import pytest  # noqa: F401 — kept for T24/T25 (pytest.raises / pytest.mark.parametrize)

from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.connectors.google.youtube_reporting_client import (
    YouTubeReportingClient,
)


def test_list_supported_jobs_filters_to_whitelist(mock_credentials) -> None:
    payload = {
        "jobs": [
            {"id": "job-a", "reportTypeId": "channel_basic_a2"},
            {"id": "job-b", "reportTypeId": "channel_combined_a2"},
            {"id": "job-c", "reportTypeId": "channel_demographics_a1"},  # not supported
        ]
    }
    def handler(request: httpx.Request) -> httpx.Response:
        import json
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    jobs = client.list_supported_jobs(account_id="content-owner-1")
    ids = {job["id"] for job in jobs}
    assert ids == {"job-a", "job-b"}


def test_list_supported_jobs_paginates(mock_credentials) -> None:
    pages = iter([
        {"jobs": [{"id": "j1", "reportTypeId": "channel_basic_a2"}], "nextPageToken": "tok-2"},
        {"jobs": [{"id": "j2", "reportTypeId": "channel_basic_a2"}]},
    ])
    def handler(request: httpx.Request) -> httpx.Response:
        import json
        return httpx.Response(200, content=json.dumps(next(pages)).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    jobs = client.list_supported_jobs(account_id="acct")
    assert [j["id"] for j in jobs] == ["j1", "j2"]


def test_list_supported_jobs_sends_on_behalf_of_content_owner(mock_credentials) -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return httpx.Response(200, content=b'{"jobs": []}')

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    client.list_supported_jobs(account_id="content-owner-42")
    assert captured[0]["onBehalfOfContentOwner"] == "content-owner-42"


def test_list_supported_jobs_filters_out_jobs_with_missing_report_type_id(
    mock_credentials,
) -> None:
    payload = {
        "jobs": [
            {"id": "job-a", "reportTypeId": "channel_basic_a2"},
            {"id": "job-b"},  # no reportTypeId at all
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    jobs = client.list_supported_jobs(account_id="acct")
    assert [j["id"] for j in jobs] == ["job-a"]


def test_list_supported_jobs_treats_empty_next_page_token_as_terminal(
    mock_credentials,
) -> None:
    payload = {
        "jobs": [{"id": "j1", "reportTypeId": "channel_basic_a2"}],
        "nextPageToken": "",  # empty-string terminator (not absent key)
    }

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    jobs = client.list_supported_jobs(account_id="acct")
    assert [j["id"] for j in jobs] == ["j1"]
