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
    YOUTUBE_ANALYTICS_COUNTRY_EVIDENCE_REPORT_TYPE,
    YOUTUBE_ANALYTICS_PROJECTING_REPORT_TYPE,
    GoogleRevenueSourceRowEntry,
    ParsedSourceRow,
)
from ums_smart_revenue.finance.google_source_normalizer import (
    CANONICAL_METRIC_RULE,
    SOURCE_SYSTEM_TO_SOURCE_KIND,
    EvidenceDisposition,
    EvidenceReason,
    SkippedSourceRow,
    SkipReason,
    _partition_projection_rows,
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
    source_account_id: str = "acct-test-1",
    raw_payload: object | None = None,
    report_type: str = "x",
    source_report_id: str | None = "r-1",
) -> GoogleRevenueSourceRowEntry:
    """Build one persisted-source-row value for pure selection tests."""
    return GoogleRevenueSourceRowEntry(
        id=f"id-{source_row_key[:8]}",
        tenant_id="00000000-0000-0000-0000-000000000001",
        source_system=source_system,
        source_row_key=source_row_key,
        source_account_id=source_account_id,
        content_owner_id=None,
        youtube_channel_id=youtube_channel_id,
        report_type=report_type,
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
        source_account_id=row.source_account_id,
        report_type=row.report_type,
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
    """Country aliases are recognized case-insensitively before projection."""
    country = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="a" * 64,
        raw_payload={
            "dimensions": {"channel": "UC_test_1", country_alias: "US"},
        },
    )
    projecting, evidence, rejected = _partition_projection_rows([country])

    assert _source_row_buckets(projecting, []) == {}
    assert projecting == []
    assert evidence[0].disposition is EvidenceDisposition.REJECTED
    assert rejected == [
        SkippedSourceRow(
            source_row_id=country.id,
            reason=SkipReason.INVALID_NON_PROJECTING_EVIDENCE,
        )
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
    projecting, evidence, rejected = _partition_projection_rows([malformed])

    assert projecting == []
    assert evidence == []
    assert rejected == [
        SkippedSourceRow(
            source_row_id=malformed.id,
            reason=SkipReason.MALFORMED_SOURCE_PAYLOAD,
        )
    ]

    # The bucket-level fence stays fail-closed for direct callers too.
    bucket_skipped: list[SkippedSourceRow] = []
    assert _source_row_buckets([malformed], bucket_skipped) == {}
    assert bucket_skipped == [
        SkippedSourceRow(
            source_row_id=malformed.id,
            reason=SkipReason.MALFORMED_SOURCE_PAYLOAD,
        )
    ]


def _country_payload(
    *,
    channel: object = "UC_test_1",
    country: object = "US",
    disposition: object = "NON_PROJECTING_EVIDENCE",
) -> dict[str, object]:
    return {
        "dimensions": {"channel": channel, "country": country},
        "metric": "estimatedRevenue",
        "projection_disposition": disposition,
        "value": "100.000000",
    }


def test_partition_accepts_country_evidence_without_projecting_it() -> None:
    """Valid U2 evidence is counted, preserved, and absent from finance inputs."""
    worldwide = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="w" * 64,
        report_type=YOUTUBE_ANALYTICS_PROJECTING_REPORT_TYPE,
        raw_payload={
            "projection_disposition": "PROJECTING",
            "dimensions": {"channel": "UC_test_1"},
        },
    )
    country = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="u" * 64,
        report_type=YOUTUBE_ANALYTICS_COUNTRY_EVIDENCE_REPORT_TYPE,
        raw_payload=_country_payload(),
    )

    projecting, evidence, rejected = _partition_projection_rows([worldwide, country])

    assert projecting == [worldwide]
    assert rejected == []
    assert len(evidence) == 1
    assert evidence[0].disposition is EvidenceDisposition.ACCEPTED
    assert evidence[0].reason is EvidenceReason.NON_PROJECTING_EVIDENCE
    assert evidence[0].source_system == "youtube_analytics"
    assert evidence[0].source_account_id == "acct-test-1"
    assert evidence[0].country_code == "US"


def test_partition_keeps_legacy_worldwide_row_without_disposition_projecting() -> None:
    """Backward compatibility applies only to legacy rows with no country axis."""
    legacy = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="l" * 64,
        report_type=YOUTUBE_ANALYTICS_PROJECTING_REPORT_TYPE,
        raw_payload={"dimensions": {"channel": "UC_test_1"}},
    )
    projecting, evidence, rejected = _partition_projection_rows([legacy])
    assert projecting == [legacy]
    assert evidence == []
    assert rejected == []


def test_partition_rejects_legacy_country_row_without_evidence_report_type() -> None:
    """A country axis alone is enough to fence legacy/imported rows from facts."""
    legacy_country = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="c" * 64,
        report_type=YOUTUBE_ANALYTICS_PROJECTING_REPORT_TYPE,
        raw_payload={"dimensions": {"channel": "UC_test_1", "country": "US"}},
    )
    projecting, evidence, rejected = _partition_projection_rows([legacy_country])
    assert projecting == []
    assert evidence[0].disposition is EvidenceDisposition.REJECTED
    assert rejected[0].reason is SkipReason.INVALID_NON_PROJECTING_EVIDENCE


@pytest.mark.parametrize(
    ("account", "channel", "country", "disposition"),
    [
        ("   ", "UC_test_1", "US", "NON_PROJECTING_EVIDENCE"),
        ("acct-test-1", "UC-other", "US", "NON_PROJECTING_EVIDENCE"),
        ("acct-test-1", "UC_test_1", "us", "NON_PROJECTING_EVIDENCE"),
        ("acct-test-1", "UC_test_1", None, "NON_PROJECTING_EVIDENCE"),
        ("acct-test-1", "UC_test_1", "US", "UNKNOWN"),
        ("acct-test-1", "UC_test_1", "US", "PROJECTING"),
    ],
)
def test_partition_rejects_invalid_country_provenance_without_projection(
    account: str,
    channel: object,
    country: object,
    disposition: object,
) -> None:
    """Malformed evidence is visible as a defect and can never become a fact."""
    row = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="i" * 64,
        source_account_id=account,
        report_type=YOUTUBE_ANALYTICS_COUNTRY_EVIDENCE_REPORT_TYPE,
        raw_payload=_country_payload(
            channel=channel,
            country=country,
            disposition=disposition,
        ),
    )
    projecting, evidence, rejected = _partition_projection_rows([row])
    assert projecting == []
    assert evidence[0].disposition is EvidenceDisposition.REJECTED
    assert rejected == [
        SkippedSourceRow(
            source_row_id=row.id,
            reason=SkipReason.INVALID_NON_PROJECTING_EVIDENCE,
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


def test_partition_excludes_parser_country_evidence_and_records_audit():
    """Country evidence is separated before grouping and recorded for audit."""
    country = _entry_from_parsed(_parsed_country_evidence())
    worldwide = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="f" * 64,
        amount="100.000000",
        raw_payload={"dimensions": {"channel": "UC_test_1", "month": "2026-04"}},
    )
    projecting, evidence, rejected = _partition_projection_rows([country, worldwide])
    buckets = _source_row_buckets(projecting, [])

    assert list(buckets.keys()) == [("UC_test_1", "youtube_analytics")]
    assert buckets[("UC_test_1", "youtube_analytics")] == [worldwide]
    assert rejected == []
    assert [outcome.disposition for outcome in evidence] == [EvidenceDisposition.ACCEPTED]
    assert evidence[0].source_row_id == country.id
    assert evidence[0].country_code == "US"


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
    projecting, evidence, _rejected = _partition_projection_rows([country, worldwide])
    buckets = _source_row_buckets(projecting, [])

    canonical, rest = select_canonical_row(buckets[("UC_test_1", "youtube_analytics")])

    assert canonical is worldwide
    assert canonical.amount_native == Decimal("100.000000")
    assert rest == []
    assert len(evidence) == 1
    assert evidence[0].disposition is EvidenceDisposition.ACCEPTED


def test_partition_rejects_duplicate_country_provenance_deterministically() -> None:
    """Semantic duplicates cannot inflate accepted evidence telemetry."""
    first = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="1" * 64,
        report_type=YOUTUBE_ANALYTICS_COUNTRY_EVIDENCE_REPORT_TYPE,
        raw_payload=_country_payload(),
    )
    duplicate = _entry(
        source_system="youtube_analytics",
        metric_key="estimatedRevenue",
        source_row_key="2" * 64,
        report_type=YOUTUBE_ANALYTICS_COUNTRY_EVIDENCE_REPORT_TYPE,
        raw_payload=_country_payload(),
    )
    projecting, evidence, rejected = _partition_projection_rows([first, duplicate])
    assert projecting == []
    assert [outcome.disposition for outcome in evidence] == [
        EvidenceDisposition.ACCEPTED,
        EvidenceDisposition.REJECTED,
    ]
    assert evidence[1].reason is EvidenceReason.DUPLICATE_PROVENANCE
    assert rejected[0].reason is SkipReason.DUPLICATE_NON_PROJECTING_EVIDENCE

    _, reversed_evidence, reversed_rejected = _partition_projection_rows([duplicate, first])
    accepted_ids = {
        outcome.source_row_id
        for outcome in reversed_evidence
        if outcome.disposition is EvidenceDisposition.ACCEPTED
    }
    assert accepted_ids == {first.id}
    assert [row.source_row_id for row in reversed_rejected] == [duplicate.id]


def test_partition_unknown_source_fails_before_any_bucket_can_write() -> None:
    """Unknown source values are typed failures, never silent skips."""
    bogus = _entry(
        source_system="youtube_analytics_country_evidence",
        metric_key="estimatedRevenue",
        source_row_key="z" * 64,
        report_type=YOUTUBE_ANALYTICS_COUNTRY_EVIDENCE_REPORT_TYPE,
        raw_payload=_country_payload(),
    )
    with pytest.raises(RevenueFactValidationError, match="projection preflight"):
        _partition_projection_rows([bogus])
