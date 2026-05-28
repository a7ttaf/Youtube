"""AdSense client + adapter tests (spec §5.6)."""
from __future__ import annotations

import json

import httpx
import pytest

from ums_smart_revenue.connectors.google.adsense_management_client import (
    AdSenseManagementClient,
    adsense_response_to_parser_payload,
)
from ums_smart_revenue.connectors.google.errors import MalformedReportMonthError
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient


def test_fetch_monthly_report_pins_currency_usd_and_date_bounds(
    mock_credentials,
) -> None:
    """The client must lock dateRange=CUSTOM, MONTH dimension, USD currency, and
    the AdSense earnings + paid metric pair on every wire request.

    The first-day/last-day bounds are derived from the calendar (monthrange) so
    a 28/29/30/31-day month is covered without per-month branching.
    """
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Capture the AdSense reports.generate query for boundary assertion."""
        captured["url"] = str(request.url)
        captured["q"] = dict(request.url.params)
        body = json.dumps(
            {
                "request": {"accountId": "accounts/pub-1"},
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
    assert q["metrics"] == "ESTIMATED_EARNINGS,PAID_AMOUNT"
    assert q["dimensions"] == "MONTH"
    assert q["currencyCode"] == "USD"
    assert "report_id" in out


def test_adapter_wraps_response_with_deterministic_report_id() -> None:
    """Two identical (account_id, report_month) inputs must produce the same
    deterministic SHA-256 report_id stamp, since AdSense reports.generate does
    not return a stable report id and AdSenseManagementParser requires one.
    """
    response = {
        "request": {"accountId": "accounts/pub-1", "currencyCode": "USD"},
        "headers": [{"type": "DIMENSION", "name": "MONTH"}],
        "rows": [],
    }
    payload = adsense_response_to_parser_payload(
        response_json=response, account_id="pub-1", report_month="2026-05",
    )
    assert payload["report_id"]
    payload2 = adsense_response_to_parser_payload(
        response_json=response, account_id="pub-1", report_month="2026-05",
    )
    assert payload["report_id"] == payload2["report_id"]


def test_adapter_report_id_differs_per_account_or_month() -> None:
    """Determinism must not collapse distinct (account, month) slices into one
    report_id; otherwise two different reports could share provenance and a
    stale parse could overwrite a fresh one.
    """
    response = {"request": {"accountId": "accounts/pub-1"}, "headers": [], "rows": []}
    base = adsense_response_to_parser_payload(
        response_json=response, account_id="pub-1", report_month="2026-05",
    )
    other_account = adsense_response_to_parser_payload(
        response_json=response, account_id="pub-2", report_month="2026-05",
    )
    other_month = adsense_response_to_parser_payload(
        response_json=response, account_id="pub-1", report_month="2026-04",
    )
    assert base["report_id"] != other_account["report_id"]
    assert base["report_id"] != other_month["report_id"]


@pytest.mark.parametrize("bad_month", ["2026-5", "2026", "abcd-ef", "2026-13", ""])
def test_fetch_monthly_report_rejects_malformed_report_month(
    mock_credentials, bad_month: str,
) -> None:
    """Malformed report_month must raise MalformedReportMonthError before any
    HTTP request is issued, matching YouTube Analytics' typed-boundary pattern
    so the orchestrator's GoogleConnectorError handler can record FAILED runs.
    """
    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(lambda _r: httpx.Response(200)),
    )
    client = AdSenseManagementClient(http=http)
    with pytest.raises(MalformedReportMonthError):
        client.fetch_monthly_report(account_id="pub-1", report_month=bad_month)
