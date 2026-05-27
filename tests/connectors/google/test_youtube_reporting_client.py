"""YouTube Reporting client tests (spec §5.4)."""
from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest  # noqa: F401 — kept for T24/T25 (pytest.raises / pytest.mark.parametrize)

from ums_smart_revenue.connectors.google.errors import GoogleApiResponseError
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.connectors.google.youtube_reporting_client import (
    YouTubeReportingClient,
)


def _next_json_response(pages: Iterator[dict[str, object]]) -> httpx.Response:
    try:
        payload = next(pages)
    except StopIteration as exc:
        raise AssertionError("unexpected extra HTTP request") from exc
    return httpx.Response(200, content=json.dumps(payload).encode())


def test_list_supported_jobs_filters_to_whitelist(mock_credentials) -> None:
    payload = {
        "jobs": [
            {"id": "job-a", "reportTypeId": "content_owner_estimated_revenue_a1"},
            {"id": "job-b", "reportTypeId": "content_owner_ad_revenue_raw_a1"},
            {"id": "job-c", "reportTypeId": "channel_demographics_a1"},  # not supported
        ]
    }
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    jobs = client.list_supported_jobs(account_id="content-owner-1")
    ids = {job["id"] for job in jobs}
    assert ids == {"job-a"}


def test_list_supported_jobs_paginates(mock_credentials) -> None:
    pages = iter([
        {
            "jobs": [{"id": "j1", "reportTypeId": "content_owner_estimated_revenue_a1"}],
            "nextPageToken": "tok-2",
        },
        {"jobs": [{"id": "j2", "reportTypeId": "content_owner_estimated_revenue_a1"}]},
    ])
    def handler(_request: httpx.Request) -> httpx.Response:
        return _next_json_response(pages)

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
    assert captured[0]["includeSystemManaged"] == "true"


def test_list_supported_jobs_filters_out_jobs_with_missing_report_type_id(
    mock_credentials,
) -> None:
    payload = {
        "jobs": [
            {"id": "job-a", "reportTypeId": "content_owner_estimated_revenue_a1"},
            {"id": "job-b"},  # no reportTypeId at all
        ]
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    jobs = client.list_supported_jobs(account_id="acct")
    assert [j["id"] for j in jobs] == ["job-a"]


@pytest.mark.parametrize("jobs_payload", [{"id": "job-a"}, "not-a-list", None])
def test_list_supported_jobs_rejects_malformed_jobs_list(
    mock_credentials, jobs_payload: object
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=json.dumps({"jobs": jobs_payload}).encode()
        )

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)

    with pytest.raises(GoogleApiResponseError, match="jobs"):
        client.list_supported_jobs(account_id="acct")


def test_list_supported_jobs_treats_empty_next_page_token_as_terminal(
    mock_credentials,
) -> None:
    payload = {
        "jobs": [{"id": "j1", "reportTypeId": "content_owner_estimated_revenue_a1"}],
        "nextPageToken": "",  # empty-string terminator (not absent key)
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    jobs = client.list_supported_jobs(account_id="acct")
    assert [j["id"] for j in jobs] == ["j1"]


@pytest.mark.parametrize("next_page_token", [None, False, 0, [], {}, "   "])
def test_list_supported_jobs_rejects_malformed_next_page_token(
    mock_credentials, next_page_token: object
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("malformed nextPageToken must not be reused")
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "jobs": [
                        {
                            "id": "j1",
                            "reportTypeId": "content_owner_estimated_revenue_a1",
                        }
                    ],
                    "nextPageToken": next_page_token,
                }
            ).encode(),
        )

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)

    with pytest.raises(GoogleApiResponseError, match="nextPageToken"):
        client.list_supported_jobs(account_id="acct")
    assert calls == 1


def test_list_reports_for_month_passes_date_bounds(mock_credentials) -> None:
    captured = {}
    def handler(request: httpx.Request) -> httpx.Response:
        captured.setdefault("queries", []).append(dict(request.url.params))
        return httpx.Response(200, content=json.dumps({"reports": []}).encode())
    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    client.list_reports_for_month(
        account_id="acct", job_id="job-1", report_month="2026-05",
    )
    q = captured["queries"][0]
    assert q["startTimeAtOrAfter"] == "2026-05-01T00:00:00Z"
    assert q["startTimeBefore"] == "2026-06-01T00:00:00Z"
    assert q["onBehalfOfContentOwner"] == "acct"


def test_list_reports_for_month_paginates(mock_credentials) -> None:
    captured: dict[str, list[dict[str, str]]] = {}
    pages = iter([
        {"reports": [{"id": "r1", "downloadUrl": "https://x/r1"}], "nextPageToken": "p2"},
        {"reports": [{"id": "r2", "downloadUrl": "https://x/r2"}]},
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        captured.setdefault("queries", []).append(dict(request.url.params))
        return _next_json_response(pages)

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    reports = client.list_reports_for_month(
        account_id="acct", job_id="job-1", report_month="2026-05",
    )
    assert [r["id"] for r in reports] == ["r1", "r2"]
    # Page 2 must carry the pageToken from page 1's nextPageToken.
    assert captured["queries"][1]["pageToken"] == "p2"
    # Date-bounds must be sent on every paginated request, not only page 1.
    assert captured["queries"][1]["startTimeAtOrAfter"] == "2026-05-01T00:00:00Z"
    assert captured["queries"][1]["startTimeBefore"] == "2026-06-01T00:00:00Z"


@pytest.mark.parametrize("next_page_token", [None, False, 0, [], {}, "   "])
def test_list_reports_for_month_rejects_malformed_next_page_token(
    mock_credentials, next_page_token: object
) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AssertionError("malformed nextPageToken must not be reused")
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "reports": [{"id": "r1", "downloadUrl": "https://x/r1"}],
                    "nextPageToken": next_page_token,
                }
            ).encode(),
        )

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)

    with pytest.raises(GoogleApiResponseError, match="nextPageToken"):
        client.list_reports_for_month(
            account_id="acct", job_id="job-1", report_month="2026-05",
        )
    assert calls == 1


@pytest.mark.parametrize("reports_payload", [{"id": "r1"}, "not-a-list", None])
def test_list_reports_for_month_rejects_malformed_reports_list(
    mock_credentials, reports_payload: object
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=json.dumps({"reports": reports_payload}).encode()
        )

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)

    with pytest.raises(GoogleApiResponseError, match="reports"):
        client.list_reports_for_month(
            account_id="acct", job_id="job-1", report_month="2026-05",
        )


def test_list_reports_for_month_returns_oldest_create_time_first(
    mock_credentials,
) -> None:
    pages = iter(
        [
            {
                "reports": [
                    {
                        "id": "newer",
                        "downloadUrl": "https://x/newer",
                        "createTime": "2026-05-03T00:00:00Z",
                    },
                    {
                        "id": "older",
                        "downloadUrl": "https://x/older",
                        "createTime": "2026-05-01T00:00:00Z",
                    },
                ],
            }
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return _next_json_response(pages)

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    reports = client.list_reports_for_month(
        account_id="acct", job_id="job-1", report_month="2026-05",
    )

    assert [report["id"] for report in reports] == ["older", "newer"]


def test_list_reports_for_month_keeps_newest_replacement_per_period(
    mock_credentials,
) -> None:
    pages = iter(
        [
            {
                "reports": [
                    {
                        "id": "may-01-old",
                        "downloadUrl": "https://x/may-01-old",
                        "startTime": "2026-05-01T00:00:00Z",
                        "endTime": "2026-05-02T00:00:00Z",
                        "createTime": "2026-05-02T03:00:00Z",
                    },
                    {
                        "id": "may-01-new",
                        "downloadUrl": "https://x/may-01-new",
                        "startTime": "2026-05-01T00:00:00Z",
                        "endTime": "2026-05-02T00:00:00Z",
                        "createTime": "2026-05-03T03:00:00Z",
                    },
                    {
                        "id": "may-02",
                        "downloadUrl": "https://x/may-02",
                        "startTime": "2026-05-02T00:00:00Z",
                        "endTime": "2026-05-03T00:00:00Z",
                        "createTime": "2026-05-03T04:00:00Z",
                    },
                ],
            }
        ]
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return _next_json_response(pages)

    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    reports = client.list_reports_for_month(
        account_id="acct", job_id="job-1", report_month="2026-05",
    )

    assert [report["id"] for report in reports] == ["may-01-new", "may-02"]


def test_list_reports_handles_december_boundary(mock_credentials) -> None:
    captured = {}
    def handler(request: httpx.Request) -> httpx.Response:
        captured["q"] = dict(request.url.params)
        return httpx.Response(200, content=json.dumps({"reports": []}).encode())
    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    client.list_reports_for_month(
        account_id="acct", job_id="j", report_month="2026-12",
    )
    assert captured["q"]["startTimeBefore"] == "2027-01-01T00:00:00Z"


def test_fetch_report_downloads_csv_bytes(mock_credentials) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://youtubereporting.googleapis.com/downloads/abc"
        assert request.headers.get("Authorization") == "Bearer fake-bearer"
        return httpx.Response(200, content=b"day,channel\n2026-05,xyz\n")
    http = GoogleHttpClient(
        credentials=mock_credentials, transport=httpx.MockTransport(handler),
    )
    client = YouTubeReportingClient(http=http)
    out = client.fetch_report(
        download_url="https://youtubereporting.googleapis.com/downloads/abc"
    )
    assert out == b"day,channel\n2026-05,xyz\n"


@pytest.mark.parametrize(
    "download_url",
    [
        "http://youtubereporting.googleapis.com/downloads/abc",
        "https://example.com/downloads/abc",
        "https://127.0.0.1/downloads/abc",
        "https://localhost/downloads/abc",
    ],
)
def test_fetch_report_rejects_non_google_download_url_before_auth(
    download_url: str,
) -> None:
    class _FakeHttp:
        called = False

        def fetch_bytes(self, *, url: str) -> bytes:
            _ = url
            self.called = True
            return b""

    http = _FakeHttp()
    client = YouTubeReportingClient(http=http)  # type: ignore[arg-type]

    with pytest.raises(GoogleApiResponseError):
        client.fetch_report(download_url=download_url)

    assert http.called is False
