"""Each parser must raise ParserError (typed) on malformed payloads.

This pins the failure-state contract for the parser/ingestion
orchestration skeleton. Live connector job runner failure-state
recording is out of B1 scope.
"""

from uuid import uuid4

import pytest

from ums_smart_revenue.connectors.google_source_parsers import (
    AdSenseManagementParser,
    ParserError,
    YouTubeAnalyticsParser,
    YouTubeReportingParser,
)

TENANT_ID = uuid4()


def test_youtube_reporting_rejects_missing_metadata() -> None:
    with pytest.raises(ParserError):
        list(YouTubeReportingParser().parse({}, tenant_id=TENANT_ID))


def test_youtube_reporting_rejects_non_string_amount() -> None:
    payload = {
        "report_metadata": {"report_id": "r", "report_type": "t"},
        "rows": [{
            "line_index": 0,
            "date_range": {"start": "2026-04-01", "end": "2026-04-30"},
            "dimensions": {"channel": "UC_x"},
            "metrics": {"estimatedRevenue": 123.45, "currencyCode": "USD"},  # float, not str
        }],
    }
    with pytest.raises(ParserError):
        list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))


def test_youtube_analytics_rejects_mismatched_row_length() -> None:
    payload = {
        "query_request": {
            "ids": "contentOwner==cms-1",
            "startDate": "2026-04-01",
            "endDate": "2026-04-30",
            "metrics": "estimatedRevenue",
            "dimensions": "channel",
            "currency": "USD",
        },
        "columnHeaders": [
            {"name": "channel", "columnType": "DIMENSION", "dataType": "STRING"},
            {"name": "estimatedRevenue", "columnType": "METRIC", "dataType": "FLOAT"},
        ],
        "rows": [["UC_x", "100.00", "EXTRA_VALUE"]],  # 3 cells, 2 headers
    }
    with pytest.raises(ParserError):
        list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))


def test_adsense_rejects_missing_date_range() -> None:
    payload = {
        "request": {"accountId": "accounts/pub-1", "currencyCode": "USD"},
        "report_id": "r",
        "headers": [{"name": "PAID_AMOUNT", "type": "METRIC_CURRENCY", "currencyCode": "USD"}],
        "rows": [],
    }
    with pytest.raises(ParserError):
        list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))
