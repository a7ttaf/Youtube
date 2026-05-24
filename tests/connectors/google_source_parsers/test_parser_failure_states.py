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


def test_parsers_reject_non_object_payload() -> None:
    """A non-object top-level payload (JSON list/string) must raise ParserError,
    not a raw AttributeError from `.get` on a non-dict. Each parser's first
    action is require_dict(payload, ...), so the container guard lives there.
    """
    for parser in (
        YouTubeReportingParser(),
        YouTubeAnalyticsParser(),
        AdSenseManagementParser(),
    ):
        with pytest.raises(ParserError):
            list(parser.parse(["not", "an", "object"], tenant_id=TENANT_ID))


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


def test_youtube_reporting_rejects_boolean_line_index() -> None:
    """bool is a subclass of int; True must not be silently treated as 1."""
    payload = {
        "report_metadata": {"report_id": "r", "report_type": "t"},
        "rows": [{
            "line_index": True,
            "date_range": {"start": "2026-04-01", "end": "2026-04-30"},
            "dimensions": {"channel": "UC_x", "content_owner": "cms-1"},
            "metrics": {"estimatedRevenue": "10.00", "currencyCode": "USD"},
        }],
    }
    with pytest.raises(ParserError):
        list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))


def test_youtube_reporting_rejects_malformed_date() -> None:
    """date.fromisoformat ValueError must surface as ParserError."""
    payload = {
        "report_metadata": {"report_id": "r", "report_type": "t"},
        "rows": [{
            "line_index": 0,
            "date_range": {"start": "2026-13-01", "end": "2026-04-30"},  # month 13
            "dimensions": {"channel": "UC_x", "content_owner": "cms-1"},
            "metrics": {"estimatedRevenue": "10.00", "currencyCode": "USD"},
        }],
    }
    with pytest.raises(ParserError):
        list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))


def test_youtube_analytics_rejects_malformed_date() -> None:
    payload = {
        "query_request": {
            "ids": "contentOwner==cms-1",
            "startDate": "not-a-date",
            "endDate": "2026-04-30",
            "metrics": "estimatedRevenue",
            "dimensions": "channel",
            "currency": "USD",
        },
        "columnHeaders": [
            {"name": "channel", "columnType": "DIMENSION", "dataType": "STRING"},
            {"name": "estimatedRevenue", "columnType": "METRIC", "dataType": "FLOAT"},
        ],
        "rows": [["UC_x", "100.00"]],
    }
    with pytest.raises(ParserError):
        list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))


def test_youtube_analytics_rejects_header_missing_name() -> None:
    """A typed columnHeader without a name must raise ParserError, not KeyError."""
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
            {"columnType": "DIMENSION", "dataType": "STRING"},  # missing 'name'
            {"name": "estimatedRevenue", "columnType": "METRIC", "dataType": "FLOAT"},
        ],
        "rows": [["UC_x", "100.00"]],
    }
    with pytest.raises(ParserError):
        list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))


def test_adsense_rejects_invalid_calendar_date() -> None:
    """date(year, month, day) ValueError must surface as ParserError."""
    payload = {
        "request": {
            "accountId": "accounts/pub-1",
            "dateRange": {
                "startDate": {"year": 2026, "month": 13, "day": 1},  # month 13
                "endDate": {"year": 2026, "month": 4, "day": 30},
            },
            "currencyCode": "USD",
        },
        "report_id": "r",
        "headers": [{"name": "PAID_AMOUNT", "type": "METRIC_CURRENCY", "currencyCode": "USD"}],
        "rows": [],
    }
    with pytest.raises(ParserError):
        list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))


def test_adsense_rejects_header_missing_name() -> None:
    """A typed AdSense header without a name must raise ParserError, not KeyError."""
    payload = {
        "request": {
            "accountId": "accounts/pub-1",
            "dateRange": {
                "startDate": {"year": 2026, "month": 4, "day": 1},
                "endDate": {"year": 2026, "month": 4, "day": 30},
            },
            "currencyCode": "USD",
        },
        "report_id": "r",
        "headers": [{"type": "METRIC_CURRENCY", "currencyCode": "USD"}],  # missing 'name'
        "rows": [],
    }
    with pytest.raises(ParserError):
        list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))


def test_adsense_rejects_unsupported_header_type() -> None:
    """An unsupported header type fails closed (ParserError) rather than being
    skipped, which would shift cells and mislabel revenue values."""
    payload = {
        "request": {
            "accountId": "accounts/pub-1",
            "dateRange": {
                "startDate": {"year": 2026, "month": 4, "day": 1},
                "endDate": {"year": 2026, "month": 4, "day": 30},
            },
            "currencyCode": "USD",
        },
        "report_id": "r",
        "headers": [
            {"name": "CLICKS", "type": "METRIC_TALLY"},  # not dimension/currency
            {"name": "PAID_AMOUNT", "type": "METRIC_CURRENCY", "currencyCode": "USD"},
        ],
        "rows": [{"cells": [{"value": "10"}, {"value": "5.00"}]}],
    }
    with pytest.raises(ParserError):
        list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))


def test_adsense_rejects_metric_currency_header_mismatch() -> None:
    """A METRIC_CURRENCY header whose currencyCode diverges from the request
    currencyCode must fail closed: stamping request.currencyCode on an amount
    the header says is a different currency would mislabel a preserved value.
    """
    payload = {
        "request": {
            "accountId": "accounts/pub-1",
            "dateRange": {
                "startDate": {"year": 2026, "month": 4, "day": 1},
                "endDate": {"year": 2026, "month": 4, "day": 30},
            },
            "currencyCode": "USD",
        },
        "report_id": "r",
        "headers": [{"name": "PAID_AMOUNT", "type": "METRIC_CURRENCY", "currencyCode": "EUR"}],
        "rows": [{"cells": [{"value": "5.00"}]}],
    }
    with pytest.raises(ParserError):
        list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))


def test_adsense_rejects_metric_currency_header_without_currency() -> None:
    """A METRIC_CURRENCY header missing its currencyCode (or non-string) must
    raise ParserError rather than silently inheriting the request currency.
    """
    payload = {
        "request": {
            "accountId": "accounts/pub-1",
            "dateRange": {
                "startDate": {"year": 2026, "month": 4, "day": 1},
                "endDate": {"year": 2026, "month": 4, "day": 30},
            },
            "currencyCode": "USD",
        },
        "report_id": "r",
        "headers": [{"name": "PAID_AMOUNT", "type": "METRIC_CURRENCY"}],  # no currencyCode
        "rows": [{"cells": [{"value": "5.00"}]}],
    }
    with pytest.raises(ParserError):
        list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))


def test_youtube_analytics_rejects_unsupported_column_type() -> None:
    """An unsupported columnType fails closed (ParserError) rather than being
    skipped, which would misalign values with metrics."""
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
            {"name": "views", "columnType": "TALLY", "dataType": "INTEGER"},  # unsupported
            {"name": "estimatedRevenue", "columnType": "METRIC", "dataType": "FLOAT"},
        ],
        "rows": [["UC_x", "1000", "100.00"]],
    }
    with pytest.raises(ParserError):
        list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))


def test_youtube_analytics_rejects_multi_month_range() -> None:
    """A query spanning more than one calendar month would mis-bucket report_month."""
    payload = {
        "query_request": {
            "ids": "contentOwner==cms-1",
            "startDate": "2026-04-01",
            "endDate": "2026-05-31",  # crosses April -> May
            "metrics": "estimatedRevenue",
            "dimensions": "channel",
            "currency": "USD",
        },
        "columnHeaders": [
            {"name": "channel", "columnType": "DIMENSION", "dataType": "STRING"},
            {"name": "estimatedRevenue", "columnType": "METRIC", "dataType": "FLOAT"},
        ],
        "rows": [["UC_x", "100.00"]],
    }
    with pytest.raises(ParserError):
        list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))


def test_adsense_rejects_multi_month_range() -> None:
    """A dateRange spanning more than one calendar month would mis-bucket report_month."""
    payload = {
        "request": {
            "accountId": "accounts/pub-1",
            "dateRange": {
                "startDate": {"year": 2026, "month": 4, "day": 1},
                "endDate": {"year": 2026, "month": 5, "day": 31},  # crosses month
            },
            "currencyCode": "USD",
        },
        "report_id": "r",
        "headers": [{"name": "PAID_AMOUNT", "type": "METRIC_CURRENCY", "currencyCode": "USD"}],
        "rows": [{"cells": [{"value": "5.00"}]}],
    }
    with pytest.raises(ParserError):
        list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))


def test_youtube_reporting_rejects_multi_month_row() -> None:
    """A reporting row whose date_range crosses a calendar month would mis-bucket."""
    payload = {
        "report_metadata": {"report_id": "r", "report_type": "t"},
        "rows": [{
            "line_index": 0,
            "date_range": {"start": "2026-04-01", "end": "2026-05-31"},  # crosses month
            "dimensions": {"channel": "UC_x", "content_owner": "cms-1"},
            "metrics": {"estimatedRevenue": "10.00", "currencyCode": "USD"},
        }],
    }
    with pytest.raises(ParserError):
        list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))


def test_adsense_rejects_reversed_date_range() -> None:
    """A reversed same-month dateRange (end before start) must fail closed."""
    payload = {
        "request": {
            "accountId": "accounts/pub-1",
            "dateRange": {
                "startDate": {"year": 2026, "month": 4, "day": 30},
                "endDate": {"year": 2026, "month": 4, "day": 1},  # before start
            },
            "currencyCode": "USD",
        },
        "report_id": "r",
        "headers": [{"name": "PAID_AMOUNT", "type": "METRIC_CURRENCY", "currencyCode": "USD"}],
        "rows": [{"cells": [{"value": "5.00"}]}],
    }
    with pytest.raises(ParserError):
        list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))


def test_youtube_analytics_rejects_reversed_date_range() -> None:
    """A reversed same-month range must fail closed before month bucketing."""
    payload = {
        "query_request": {
            "ids": "contentOwner==cms-1",
            "startDate": "2026-04-30",
            "endDate": "2026-04-01",  # before start, same month
            "metrics": "estimatedRevenue",
            "dimensions": "channel",
            "currency": "USD",
        },
        "columnHeaders": [
            {"name": "channel", "columnType": "DIMENSION", "dataType": "STRING"},
            {"name": "estimatedRevenue", "columnType": "METRIC", "dataType": "FLOAT"},
        ],
        "rows": [["UC_x", "100.00"]],
    }
    with pytest.raises(ParserError):
        list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))


def test_youtube_reporting_rejects_reversed_date_range() -> None:
    """A reporting row with end before start must fail closed."""
    payload = {
        "report_metadata": {"report_id": "r", "report_type": "t"},
        "rows": [{
            "line_index": 0,
            "date_range": {"start": "2026-04-30", "end": "2026-04-01"},  # reversed
            "dimensions": {"channel": "UC_x", "content_owner": "cms-1"},
            "metrics": {"estimatedRevenue": "10.00", "currencyCode": "USD"},
        }],
    }
    with pytest.raises(ParserError):
        list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))


def test_adsense_rejects_non_dict_cell() -> None:
    """A row cell that is not an object with a 'value' must fail closed rather
    than silently becoming None (which a DIMENSION cell would accept)."""
    payload = {
        "request": {
            "accountId": "accounts/pub-1",
            "dateRange": {
                "startDate": {"year": 2026, "month": 4, "day": 1},
                "endDate": {"year": 2026, "month": 4, "day": 30},
            },
            "currencyCode": "USD",
        },
        "report_id": "r",
        "headers": [
            {"name": "COUNTRY", "type": "DIMENSION"},
            {"name": "PAID_AMOUNT", "type": "METRIC_CURRENCY", "currencyCode": "USD"},
        ],
        "rows": [{"cells": ["EG", {"value": "5.00"}]}],  # first cell bare string
    }
    with pytest.raises(ParserError):
        list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))


def test_adsense_rejects_duplicate_header() -> None:
    """Duplicate headers would overwrite cells in the row mapping; fail closed."""
    payload = {
        "request": {
            "accountId": "accounts/pub-1",
            "dateRange": {
                "startDate": {"year": 2026, "month": 4, "day": 1},
                "endDate": {"year": 2026, "month": 4, "day": 30},
            },
            "currencyCode": "USD",
        },
        "report_id": "r",
        "headers": [
            {"name": "PAID_AMOUNT", "type": "METRIC_CURRENCY", "currencyCode": "USD"},
            {"name": "PAID_AMOUNT", "type": "METRIC_CURRENCY", "currencyCode": "USD"},  # dup
        ],
        "rows": [{"cells": [{"value": "1.00"}, {"value": "2.00"}]}],
    }
    with pytest.raises(ParserError):
        list(AdSenseManagementParser().parse(payload, tenant_id=TENANT_ID))


def test_youtube_analytics_rejects_duplicate_header() -> None:
    """Duplicate columnHeaders would overwrite cells in the row mapping; fail closed."""
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
            {"name": "estimatedRevenue", "columnType": "METRIC", "dataType": "FLOAT"},  # dup
        ],
        "rows": [["UC_x", "100.00", "200.00"]],
    }
    with pytest.raises(ParserError):
        list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
