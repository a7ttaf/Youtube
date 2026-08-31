"""Pure-function and parser-boundary tests for C1 canonical selection.

No DB, no session. Verifies the rule wiring per source_system, the
frozen-mapping contract, and country-evidence handling before canonicalization.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest

from ums_smart_revenue.connectors.google_source_parsers import YouTubeAnalyticsParser
from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    GoogleRevenueSourceRowEntry,
    ParsedSourceRow,
)
from ums_smart_revenue.finance.google_source_normalizer import (
    CANONICAL_METRIC_RULE,
    SOURCE_SYSTEM_TO_SOURCE_KIND,
    SkippedSourceRow,
    SkipReason,
    _scoped_source_rows,
    _source_row_buckets,
    select_canonical_row,
)
from ums_smart_revenue.finance.revenue_facts import (
    RevenueFactSourceKind,
    RevenueFactValidationError,
)


def test_source_system_to_source_kind_mapping_covers_three_supported_systems():
    """Map every supported Google system to its revenue-fact source kind."""
    assert dict(SOURCE_SYSTEM_TO_SOURCE_KIND) == {
        "youtube_reporting": RevenueFactSourceKind.YOUTUBE_CMS,
        "youtube_analytics": RevenueFactSourceKind.YOUTUBE_ANALYTICS,
        "adsense_management": RevenueFactSourceKind.ADSENSE,
    }


def test_canonical_metric_rule_mapping_is_frozen():
    """Reject runtime mutation of the canonical metric priorities."""
    with pytest.raises(TypeError):
        CANONICAL_METRIC_RULE["youtube_reporting"] = ("foo",)  # type: ignore[index]


def _entry(
    *,
    source_system: str,
    metric_key: str,
    source_row_key: str,
    amount: str = "100.000000",
    currency: str = "USD",
    youtube_channel_id: str | None = "UC_test_1",
    value_kind: str = "estimated",
    raw_payload: object | None = None,
    source_report_id: str | None = "r-1",
) -> GoogleRevenueSourceRowEntry:
    """Build one persisted-source-row value for pure selection tests."""
    return GoogleRevenueSourceRowEntry(
        id=f"id-{source_row_key[:8]}",
        tenant_id="00000000-0000-0000-0000-000000000001",
        source_system=source_system,
        source_row_key=source_row_key,
        source_account_id="acct-test-1",
        content_owner_id=None,
        youtube_channel_id=youtube_channel_id,
        report_type="x",
        report_month="2026-04",
        period_start=date(2026, 4, 1),
        period_end=date(2026, 4, 30),
        metric_key=metric_key,
        value_kind=value_kind,
        amount_native=Decimal(amount),
        currency_code=currency,
        source_report_id=source_report_id,
        raw_file_id=None,
        raw_payload={} if raw_payload is None else raw_payload,
        imported_by=None,
        ingested_at=date(2026, 4, 1),  # type: ignore[arg-type]
    )


def _parsed_country_evidence() -> ParsedSourceRow:
    """Build one real parser output row with the production country shape."""
    payload: dict[str, object] = {
        "query_request": {
            "startDate": "2026-04-01",
            "endDate": "2026-04-30",
            "ids": "channel==UC_test_1",
            "metrics": "estimatedRevenue",
            "dimensions": "channel,country",
            "currency": "USD",
        },
        "columnHeaders": [
            {"name": "channel", "columnType": "DIMENSION"},
            {"name": "country", "columnType": "DIMENSION"},
            {"name": "estimatedRevenue", "columnType": "METRIC"},
        ],
        "rows": [["UC_test_1", "US", "25.000000"]],
    }
    parsed_rows = list(
        YouTubeAnalyticsParser().parse(
            payload,
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        )
    )
    # FIX: Assert parser cardinality explicitly so an empty generator yields an
    # actionable test failure instead of leaking StopIteration from next().
    assert len(parsed_rows) == 1
    return parsed_rows[0]


def _parsed_worldwide() -> ParsedSourceRow:
    """Build one parser output row with the live month-only dimension shape."""
    payload: dict[str, object] = {
        "query_request": {
            "startDate": "2026-04-01",
            "endDate": "2026-04-30",
            "ids": "channel==UC_test_1",
            "metrics": "estimatedRevenue",
            "dimensions": "channel,month",
            "currency": "USD",
        },
        "columnHeaders": [
            {"name": "channel", "columnType": "DIMENSION"},
            {"name": "month", "columnType": "DIMENSION"},
            {"name": "estimatedRevenue", "columnType": "METRIC"},
        ],
        "rows": [["UC_test_1", "2026-04", "100.000000"]],
    }
    parsed_rows = list(
        YouTubeAnalyticsParser().parse(
            payload,
            tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        )
    )
    # FIX: Assert parser cardinality explicitly so an empty generator yields an
    # actionable test failure instead of leaking StopIteration from next().
    assert len(parsed_rows) == 1
    return parsed_rows[0]


def _entry_from_parsed(row: ParsedSourceRow) -> GoogleRevenueSourceRowEntry:
    """Convert parser output into the persisted-entry shape used by C1 tests."""
    return _entry(
        source_system=row.source_system,
        metric_key=row.metric_key,
        source_row_key=row.source_row_key,
        amount=str(row.amount_native),
        currency=row.currency_code,
        youtube_channel_id=row.youtube_channel_id,
        value_kind=row.value_kind,
        raw_payload=row.raw_payload,
        source_report_id=row.source_report_id,
    )


def test_select_canonical_row_youtube_reporting_picks_estimatedRevenue():  # noqa: N802
    """Select estimated revenue for YouTube Reporting rows."""
    rows = [
        _entry(
            source_system="youtube_reporting",
            metric_key="estimatedRevenue",
            source_row_key="a" * 64,
        )
    ]
    canonical, rest = select_canonical_row(rows)
    assert canonical is rows[0]
    assert rest == []


def test_select_canonical_row_youtube_analytics_picks_estimatedRevenue():  # noqa: N802
    """Select estimated revenue for worldwide YouTube Analytics rows."""
    rows = [
        _entry(
            source_system="youtube_analytics",
            metric_key="estimatedRevenue",
            source_row_key="b" * 64,
        )
    ]
    canonical, rest = select_canonical_row(rows)
    assert canonical is rows[0]
    assert rest == []


def test_select_canonical_row_adsense_prefers_PAID_AMOUNT_over_ESTIMATED_EARNINGS():  # noqa: N802
    """Prefer paid AdSense amounts over estimated earnings."""
    paid = _entry(
        source_system="adsense_management", metric_key="PAID_AMOUNT", source_row_key="c" * 64
    )
    earnings = _entry(
        source_system="adsense_management", metric_key="ESTIMATED_EARNINGS", source_row_key="d" * 64
    )
    canonical, rest = select_canonical_row([earnings, paid])
    assert canonical is paid
    assert rest == [earnings]


def test_select_canonical_row_adsense_falls_back_to_ESTIMATED_EARNINGS_when_no_PAID_AMOUNT():  # noqa: N802
    """Fall back to estimated AdSense earnings when no paid amount exists."""
    earnings = _entry(
        source_system="adsense_management", metric_key="ESTIMATED_EARNINGS", source_row_key="e" * 64
    )
    canonical, rest = select_canonical_row([earnings])
    assert canonical is earnings
    assert rest == []


def test_select_canonical_row_returns_none_when_no_preferred_metric_present():
    """Return no canonical row when a bucket has no preferred metric."""
    unpaid = _entry(
        source_system="adsense_management", metric_key="UNPAID_AMOUNT", source_row_key="f" * 64
    )
    canonical, rest = select_canonical_row([unpaid])
    assert canonical is None
    assert rest == [unpaid]


def test_select_canonical_row_tie_break_is_deterministic_by_source_row_key_asc():
    """Break equal-priority ties by ascending source-row key."""
    later = _entry(
        source_system="youtube_reporting", metric_key="estimatedRevenue", source_row_key="b" * 64
    )
    earlier = _entry(
        source_system="youtube_reporting", metric_key="estimatedRevenue", source_row_key="a" * 64
    )
    canonical_run1, _ = select_canonical_row([later, earlier])
    canonical_run2, _ = select_canonical_row([earlier, later])
    assert canonical_run1 is earlier
    assert canonical_run2 is earlier  # input order does not change selection


def test_select_canonical_row_non_canonical_rest_excludes_canonical():
    """Return every unselected row without duplicating the canonical row."""
    a = _entry(
        source_system="adsense_management",
        metric_key="PAID_AMOUNT",
        source_row_key="g" * 64,
    )
    b = _entry(
        source_system="adsense_management",
        metric_key="ESTIMATED_EARNINGS",
        source_row_key="h" * 64,
    )
    c = _entry(
        source_system="adsense_management",
        metric_key="UNPAID_AMOUNT",
        source_row_key="i" * 64,
    )
    canonical, rest = select_canonical_row([b, c, a])
    assert canonical is a
    # GoogleRevenueSourceRowEntry is unhashable (dict field), so compare by id()
    # to preserve the set-comparison intent (order-independent identity check).
    assert {id(r) for r in rest} == {id(b), id(c)}
    assert canonical not in rest


def test_select_canonical_row_raises_on_unsupported_source_system():
    """Reject source systems outside the canonicalization contract."""
    bogus = _entry(
        source_system="totally_bogus_system", metric_key="estimatedRevenue", source_row_key="z" * 64
    )
    with pytest.raises(RevenueFactValidationError, match="Unsupported source_system"):
        select_canonical_row([bogus])


def test_parser_emits_country_evidence_with_allowlisted_source_system():
    """Country dimensions stay on the valid Analytics source-system contract."""
    row = _parsed_country_evidence()

    assert row.source_system == "youtube_analytics"
    assert row.raw_payload["dimensions"] == {"channel": "UC_test_1", "country": "US"}


def test_parser_worldwide_row_remains_projectable():
    """The live parser's channel/month shape is not mistaken for country evidence."""
    worldwide = _entry_from_parsed(_parsed_worldwide())
    skipped: list[SkippedSourceRow] = []

    buckets = _source_row_buckets([worldwide], skipped)
    canonical, rest = select_canonical_row(buckets[("UC_test_1", "youtube_analytics")])

    assert canonical is worldwide
    assert rest == []
    assert skipped == []


@pytest.mark.parametrize(
    "country_alias",
    ["country", "country_code", "COUNTRY", "COUNTRY_CODE", "Country_Code"],
)
def test_country_dimension_aliases_are_non_projecting(country_alias: str):
    """Country aliases are recognized case-insensitively before bucketing."""
    country = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="a" * 64,
        raw_payload={
            "dimensions": {"channel": "UC_test_1", country_alias: "US"},
        },
    )
    skipped: list[SkippedSourceRow] = []

    buckets = _source_row_buckets([country], skipped)

    assert buckets == {}
    assert skipped == [
        SkippedSourceRow(source_row_id=country.id, reason=SkipReason.NON_PROJECTING_EVIDENCE)
    ]


@pytest.mark.parametrize(
    "raw_payload",
    [
        [],
        {},
        {"dimensions": {}},
        {"dimensions": {"month": "2026-04"}},
        {"dimensions": []},
        {"dimensions": None},
        {"dimensions": {1: "US"}},
        {"dimensions": {"channel": "UC_test_1", "country ": "US"}},
    ],
    ids=[
        "payload-list",
        "missing-dimensions",
        "dimensions-empty",
        "channel-missing",
        "dimensions-list",
        "dimensions-null",
        "key-int",
        "whitespace-drifted-key",
    ],
)
def test_malformed_analytics_payload_is_skipped_before_projection(raw_payload: object):
    """Malformed Analytics payload containers never fall through as worldwide rows."""
    malformed = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="m" * 64,
        raw_payload=raw_payload,
    )
    skipped: list[SkippedSourceRow] = []

    buckets = _source_row_buckets([malformed], skipped)

    assert buckets == {}
    assert skipped == [
        SkippedSourceRow(
            source_row_id=malformed.id,
            reason=SkipReason.MALFORMED_SOURCE_PAYLOAD,
        )
    ]


def test_channel_scope_filters_before_analytics_payload_classification():
    """Out-of-scope malformed Analytics rows stay absent from skip telemetry."""
    out_of_scope = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="o" * 64,
        youtube_channel_id="UC_out_of_scope",
        raw_payload=[],
    )
    skipped: list[SkippedSourceRow] = []

    in_scope_rows = _scoped_source_rows([out_of_scope], {"UC_in_scope"})
    buckets = _source_row_buckets(in_scope_rows, skipped)

    assert buckets == {}
    assert skipped == []


def test_source_row_buckets_excludes_parser_country_evidence_and_records_skip():
    """Country evidence is excluded before grouping and recorded for audit."""
    country = _entry_from_parsed(_parsed_country_evidence())
    worldwide = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="f" * 64,
        amount="100.000000",
        raw_payload={"dimensions": {"channel": "UC_test_1", "month": "2026-04"}},
    )
    skipped: list[SkippedSourceRow] = []
    buckets = _source_row_buckets([country, worldwide], skipped)

    assert list(buckets.keys()) == [("UC_test_1", "youtube_analytics")]
    assert buckets[("UC_test_1", "youtube_analytics")] == [worldwide]
    assert skipped == [
        SkippedSourceRow(source_row_id=country.id, reason=SkipReason.NON_PROJECTING_EVIDENCE)
    ]


def test_country_evidence_cannot_win_canonical_result():
    """The same-system guard preserves the worldwide amount selected as canonical."""
    country = replace(
        _entry_from_parsed(_parsed_country_evidence()),
        source_row_key="a" * 64,
    )
    worldwide = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="f" * 64,
        amount="100.000000",
        raw_payload={"dimensions": {"channel": "UC_test_1", "month": "2026-04"}},
    )
    skipped: list[SkippedSourceRow] = []
    buckets = _source_row_buckets([country, worldwide], skipped)

    canonical, rest = select_canonical_row(buckets[("UC_test_1", "youtube_analytics")])

    assert canonical is worldwide
    assert canonical.amount_native == Decimal("100.000000")
    assert rest == []
    assert len(skipped) == 1
