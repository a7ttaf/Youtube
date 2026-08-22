# ============================================================================
# Purpose: AdSense management client and adapter tests (spec §5.6).
# Database/ORM: None.
# Standards: httpx2 imported as httpx for mock transports.
# Blast Radius: Test coverage for AdSense connector client only.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/adsense_management_client.py
#     -> SUT.
# ============================================================================
"""AdSense client + adapter tests (spec §5.6)."""

from __future__ import annotations

import json

import httpx2 as httpx
import pytest

from ums_smart_revenue.connectors.google.adsense_management_client import (
    AdSenseManagementClient,
    adsense_response_to_parser_payload,
)
from ums_smart_revenue.connectors.google.errors import (
    GoogleApiResponseError,
    MalformedAdsenseAccountIdError,
    MalformedReportMonthError,
)
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient


def test_fetch_monthly_report_pins_currency_usd_timezone_and_date_bounds(
    mock_credentials,
) -> None:
    """The client must lock dateRange=CUSTOM, MONTH dimension, USD currency,
    Google reporting timezone, and the AdSense earnings metric pair.

    The first-day/last-day bounds are derived from the calendar (monthrange) so
    a 28/29/30/31-day month is covered without per-month branching.
    """
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Capture the AdSense reports.generate query for boundary assertion."""
        captured["url"] = str(request.url)
        captured["q"] = dict(request.url.params)
        captured["q_items"] = list(request.url.params.multi_items())
        body = json.dumps(
            {
                "request": {"accountId": "accounts/should-not-be-trusted"},
                "headers": [],
                "rows": [],
            }
        ).encode()
        return httpx.Response(200, content=body)

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = AdSenseManagementClient(http=http)
    out = client.fetch_monthly_report(account_id="pub-1", report_month="2026-05")
    assert "accounts/pub-1/reports:generate" in captured["url"]
    q = captured["q"]
    assert q["dateRange"] == "CUSTOM"
    assert q["startDate.year"] == "2026"
    assert q["startDate.month"] == "5"
    assert q["startDate.day"] == "1"
    assert q["endDate.year"] == "2026"
    assert q["endDate.month"] == "5"
    assert q["endDate.day"] == "31"
    q_items = captured["q_items"]
    assert [value for key, value in q_items if key == "metrics"] == [
        "ESTIMATED_EARNINGS",
        "TOTAL_EARNINGS",
    ]
    assert q["dimensions"] == "MONTH"
    assert q["currencyCode"] == "USD"
    assert q["reportingTimeZone"] == "GOOGLE_TIME_ZONE"
    assert "report_id" in out
    assert out["request"] == {
        "accountId": "accounts/pub-1",
        "dateRange": {
            "startDate": {"year": 2026, "month": 5, "day": 1},
            "endDate": {"year": 2026, "month": 5, "day": 31},
        },
        "dimensions": ["MONTH"],
        "metrics": ["ESTIMATED_EARNINGS", "TOTAL_EARNINGS"],
        "currencyCode": "USD",
        "reportingTimeZone": "GOOGLE_TIME_ZONE",
    }
    # Wire-to-payload preservation at the HTTP boundary: the empty `headers`
    # and `rows` from the mock response must survive the adapter wrap so the
    # parser sees the same shape the API returned. Guards against a future
    # default-shift (e.g. silently coercing None -> []) at the wire seam.
    assert out["headers"] == []
    assert out["rows"] == []


def test_fetch_monthly_report_accepts_full_account_resource_name(
    mock_credentials,
) -> None:
    """Stored AdSense account resources must not double-prefix the URL path."""
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Capture the normalized AdSense report URL."""
        captured["path"] = request.url.path
        return httpx.Response(200, json={"headers": [], "rows": []})

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = AdSenseManagementClient(http=http)
    out = client.fetch_monthly_report(
        account_id="accounts/pub-1",
        report_month="2026-05",
    )

    assert captured["path"] == "/v2/accounts/pub-1/reports:generate"
    assert out["request"]["accountId"] == "accounts/pub-1"


def test_adapter_wraps_response_with_deterministic_report_id() -> None:
    """Two identical (account_id, report_month) inputs must produce the same
    deterministic SHA-256 report_id stamp, since AdSense reports.generate does
    not return a stable report id and AdSenseManagementParser requires one.
    """
    response = {
        "request": {"accountId": "accounts/should-not-be-trusted"},
        "headers": [{"type": "DIMENSION", "name": "MONTH"}],
        "rows": [],
    }
    payload = adsense_response_to_parser_payload(
        response_json=response,
        account_id="pub-1",
        report_month="2026-05",
    )
    assert payload["report_id"]
    payload2 = adsense_response_to_parser_payload(
        response_json=response,
        account_id="pub-1",
        report_month="2026-05",
    )
    assert payload["report_id"] == payload2["report_id"]
    # Request is synthesized from the locked client inputs because AdSense v2
    # ReportResult does not echo a request block.
    assert payload["request"] == {
        "accountId": "accounts/pub-1",
        "dateRange": {
            "startDate": {"year": 2026, "month": 5, "day": 1},
            "endDate": {"year": 2026, "month": 5, "day": 31},
        },
        "dimensions": ["MONTH"],
        "metrics": ["ESTIMATED_EARNINGS", "TOTAL_EARNINGS"],
        "currencyCode": "USD",
        "reportingTimeZone": "GOOGLE_TIME_ZONE",
    }
    assert payload["headers"] == response["headers"]
    assert payload["rows"] == response["rows"]


def test_adapter_preserves_google_report_result_metadata() -> None:
    """Raw AdSense evidence must retain Google metadata outside parser fields."""
    response = {
        "request": {"accountId": "accounts/should-not-be-trusted"},
        "headers": [{"type": "DIMENSION", "name": "MONTH"}],
        "rows": [],
        "totalMatchedRows": "0",
        "totals": [{"cells": [{"value": "0.000000"}]}],
        "averages": [{"cells": [{"value": "0.000000"}]}],
        "warnings": ["LOW_DATA"],
        "startDate": {"year": 2026, "month": 5, "day": 1},
        "endDate": {"year": 2026, "month": 5, "day": 31},
    }

    payload = adsense_response_to_parser_payload(
        response_json=response,
        account_id="pub-1",
        report_month="2026-05",
    )

    assert payload["request"]["accountId"] == "accounts/pub-1"
    assert payload["totalMatchedRows"] == response["totalMatchedRows"]
    assert payload["totals"] == response["totals"]
    assert payload["averages"] == response["averages"]
    assert payload["warnings"] == response["warnings"]
    assert payload["startDate"] == response["startDate"]
    assert payload["endDate"] == response["endDate"]


def test_adapter_defaults_when_response_fields_missing() -> None:
    """When the wire response omits headers/rows, the adapter must synthesize
    request and stamp the documented defaults: headers=[], rows=None.

    The rows default is intentionally None (NOT []) because
    AdSenseManagementParser already maps missing/None rows to a clean
    zero-result, and re-defaulting to [] here would mask a future drift in
    that contract. Locking the defaults in a unit test closes a regression
    vector where a refactor could silently flip them.
    """
    payload = adsense_response_to_parser_payload(
        response_json={},
        account_id="pub-1",
        report_month="2026-05",
    )
    assert payload["request"] == {
        "accountId": "accounts/pub-1",
        "dateRange": {
            "startDate": {"year": 2026, "month": 5, "day": 1},
            "endDate": {"year": 2026, "month": 5, "day": 31},
        },
        "dimensions": ["MONTH"],
        "metrics": ["ESTIMATED_EARNINGS", "TOTAL_EARNINGS"],
        "currencyCode": "USD",
        "reportingTimeZone": "GOOGLE_TIME_ZONE",
    }
    assert payload["headers"] == []
    assert payload["rows"] is None
    # report_id still stamped even when the response is empty: provenance
    # must not depend on the body shape.
    assert payload["report_id"]


def test_adapter_report_id_differs_per_account_or_month() -> None:
    """Determinism must not collapse distinct (account, month) slices into one
    report_id; otherwise two different reports could share provenance and a
    stale parse could overwrite a fresh one.
    """
    response = {"request": {"accountId": "accounts/pub-1"}, "headers": [], "rows": []}
    base = adsense_response_to_parser_payload(
        response_json=response,
        account_id="pub-1",
        report_month="2026-05",
    )
    other_account = adsense_response_to_parser_payload(
        response_json=response,
        account_id="pub-2",
        report_month="2026-05",
    )
    other_month = adsense_response_to_parser_payload(
        response_json=response,
        account_id="pub-1",
        report_month="2026-04",
    )
    assert base["report_id"] != other_account["report_id"]
    assert base["report_id"] != other_month["report_id"]


def test_fetch_monthly_report_rejects_truncated_report_result(mock_credentials) -> None:
    """A ReportResult with more matched rows than returned rows must fail closed."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        """Return a truncated AdSense ReportResult shape from the mock transport."""
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "headers": [],
                "rows": [{"cells": [{"value": "2026-05"}]}],
                "totalMatchedRows": "2",
            },
        )

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = AdSenseManagementClient(http=http)
    with pytest.raises(GoogleApiResponseError, match="truncated"):
        client.fetch_monthly_report(account_id="pub-1", report_month="2026-05")
    assert calls == 1


@pytest.mark.parametrize(
    "bad_account_id",
    ["pub/1", "pub?x=1", "pub#frag", "pub%2F1", "accounts/pub/1", "accounts/"],
)
def test_fetch_monthly_report_rejects_reserved_account_id_delimiters_before_http(
    mock_credentials,
    bad_account_id: str,
) -> None:
    """Reserved path/query delimiters must fail before URL construction."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        """Count any unexpected HTTP call for the malformed-account branch."""
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = AdSenseManagementClient(http=http)
    with pytest.raises(MalformedAdsenseAccountIdError):
        client.fetch_monthly_report(
            account_id=bad_account_id,
            report_month="2026-05",
        )
    assert calls == 0


@pytest.mark.parametrize("bad_month", ["2026-5", "2026", "abcd-ef", "2026-13", ""])
def test_fetch_monthly_report_rejects_malformed_report_month(
    mock_credentials,
    bad_month: str,
) -> None:
    """Malformed report_month must raise MalformedReportMonthError before any
    HTTP request is issued, matching YouTube Analytics' typed-boundary pattern
    so the orchestrator's GoogleConnectorError handler can record FAILED runs.
    """
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        """Count any unexpected HTTP call for the malformed-month branch."""
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = AdSenseManagementClient(http=http)
    with pytest.raises(MalformedReportMonthError):
        client.fetch_monthly_report(account_id="pub-1", report_month=bad_month)
    assert calls == 0


@pytest.mark.parametrize("bad_account_id", ["", " ", "\t\n"])
def test_fetch_monthly_report_rejects_empty_account_id_before_http(
    mock_credentials,
    bad_account_id: str,
) -> None:
    """Empty/blank account ids must fail closed before URL construction."""
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        """Count any unexpected HTTP call for the malformed-account branch."""
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = AdSenseManagementClient(http=http)
    with pytest.raises(MalformedAdsenseAccountIdError):
        client.fetch_monthly_report(
            account_id=bad_account_id,
            report_month="2026-05",
        )
    assert calls == 0
