import json
from datetime import date
from decimal import Decimal
from importlib import resources
from uuid import uuid4

import pytest

from ums_smart_revenue.connectors.google_source_parsers import (
    ParserError,
    YouTubeAnalyticsParser,
)
from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    YOUTUBE_ANALYTICS_COUNTRY_EVIDENCE_REPORT_TYPE,
    YOUTUBE_ANALYTICS_PROJECTING_REPORT_TYPE,
)

TENANT_ID = uuid4()


def _load_fixture(name: str) -> dict[str, object]:
    ref = resources.files("tests.connectors._fixtures.youtube_analytics").joinpath(name)
    with ref.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def test_parse_emits_one_row_per_metric_per_data_row() -> None:
    # Three rows, two monetary metrics each => 6 ParsedSourceRow.
    payload = _load_fixture("sample_query_response_2026_04.json")
    rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
    assert len(rows) == 6


def test_parse_preserves_amounts_and_uses_query_currency() -> None:
    payload = _load_fixture("sample_query_response_2026_04.json")
    rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
    # All rows carry USD because the query currency was USD.
    assert all(r.currency_code == "USD" for r in rows)
    # estimatedRevenue values appear with full precision preserved.
    est_amounts = {r.amount_native for r in rows if r.metric_key == "estimatedRevenue"}
    assert Decimal("1234.567890") in est_amounts


def test_parse_sets_value_kind_estimated_for_estimated_metric() -> None:
    payload = _load_fixture("sample_query_response_2026_04.json")
    rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
    for row in rows:
        if row.metric_key == "estimatedRevenue":
            assert row.value_kind == "estimated"
        if row.metric_key == "grossRevenue":
            assert row.value_kind == "estimated"  # Analytics is always estimated


def test_period_uses_query_start_end() -> None:
    payload = _load_fixture("sample_query_response_2026_04.json")
    rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
    for row in rows:
        assert row.period_start == date(2026, 4, 1)
        assert row.period_end == date(2026, 4, 30)
        assert row.report_month == "2026-04"


def test_source_row_key_stable_across_reruns() -> None:
    a = list(
        YouTubeAnalyticsParser().parse(
            _load_fixture("sample_query_response_2026_04.json"),
            tenant_id=TENANT_ID,
        )
    )
    b = list(
        YouTubeAnalyticsParser().parse(
            _load_fixture("sample_query_response_2026_04_rerun.json"),
            tenant_id=TENANT_ID,
        )
    )
    assert sorted(r.source_row_key for r in a) == sorted(r.source_row_key for r in b)


def test_parser_uses_youtube_analytics_source_system() -> None:
    payload = _load_fixture("sample_query_response_2026_04.json")
    rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
    assert all(r.source_system == "youtube_analytics" for r in rows)


def test_different_ids_produce_distinct_keys() -> None:
    """Two payloads identical except for `ids` MUST produce distinct source_row_keys.

    Without per-account scoping, the repo PK
    (tenant_id, source_system, source_row_key) would silently collapse
    cross-account data in a multi-CMS or multi-channel-account tenant.
    """
    base = _load_fixture("sample_query_response_2026_04.json")
    other = {
        **base,
        "query_request": {**base["query_request"], "ids": "contentOwner==cms-test-OTHER"},
    }

    a_keys = sorted(
        r.source_row_key for r in YouTubeAnalyticsParser().parse(base, tenant_id=TENANT_ID)
    )
    b_keys = sorted(
        r.source_row_key for r in YouTubeAnalyticsParser().parse(other, tenant_id=TENANT_ID)
    )

    assert set(a_keys).isdisjoint(set(b_keys)), (
        "source_row_keys must differ when `ids` differs (per-account scope)"
    )


def test_channel_scoped_ids_selector_is_accepted() -> None:
    """A `channel==<id>` selector is a valid alternative to `contentOwner==<id>`
    and must ingest: source_account_id keeps the raw selector and there is no
    content owner (content_owner_id is None). Guards against the malformed-ids
    fail-closed guard over-restricting the channel-scoped path.
    """
    base = _load_fixture("sample_query_response_2026_04.json")
    channel_scoped = {
        **base,
        "query_request": {**base["query_request"], "ids": "channel==UC-test-1"},
    }
    rows = list(YouTubeAnalyticsParser().parse(channel_scoped, tenant_id=TENANT_ID))
    assert rows  # ingests rather than failing closed
    assert all(r.source_account_id == "channel==UC-test-1" for r in rows)
    assert all(r.content_owner_id is None for r in rows)


def test_missing_currency_defaults_to_usd() -> None:
    """`currency` is an optional reports.query request parameter; the YouTube
    Analytics API defaults financial metrics to USD when it is omitted. A replay
    payload that drops the optional parameter must ingest as USD rather than
    failing closed.
    """
    base = _load_fixture("sample_query_response_2026_04.json")
    request_without_currency = {k: v for k, v in base["query_request"].items() if k != "currency"}
    payload = {**base, "query_request": request_without_currency}
    rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
    assert rows  # ingests rather than failing closed
    assert all(r.currency_code == "USD" for r in rows)


def test_surrounding_whitespace_currency_is_trimmed() -> None:
    """A present `currency` with surrounding whitespace is trimmed and stored as
    the bare ISO code, so "  USD  " and "USD" are the same currency (and key)."""
    base = _load_fixture("sample_query_response_2026_04.json")
    payload = {**base, "query_request": {**base["query_request"], "currency": "  USD  "}}
    rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
    assert rows
    assert all(r.currency_code == "USD" for r in rows)


def test_channel_selector_representation_does_not_change_keys() -> None:
    """YouTube Analytics accepts equivalent channel selectors (`channel==MINE`
    and `channel==<CHANNEL_ID>`) for the same channel. Switching representation
    between runs must NOT change source_row_keys, or upsert_many would insert
    duplicates instead of updating in place. The channel identity comes from the
    row's `channel` dimension, so the dedup key derives from the canonical
    account identity, not the raw `ids` selector string.
    """
    base = _load_fixture("sample_query_response_2026_04.json")
    mine = {**base, "query_request": {**base["query_request"], "ids": "channel==MINE"}}
    explicit = {
        **base,
        "query_request": {**base["query_request"], "ids": "channel==UC-explicit-id"},
    }
    mine_keys = sorted(
        r.source_row_key for r in YouTubeAnalyticsParser().parse(mine, tenant_id=TENANT_ID)
    )
    explicit_keys = sorted(
        r.source_row_key for r in YouTubeAnalyticsParser().parse(explicit, tenant_id=TENANT_ID)
    )
    assert mine_keys == explicit_keys


def test_sibling_metric_rows_have_isolated_raw_payload_dimensions() -> None:
    """The two monetary metrics of one data_row (estimatedRevenue, grossRevenue)
    share the same dim_values object while parsing; each yielded row must store
    an OWNED copy so mutating one row's raw_payload dimensions cannot corrupt the
    sibling metric row's audit payload (CLAUDE.md rule 4).
    """
    payload = _load_fixture("sample_query_response_2026_04.json")
    rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
    # The parser yields all monetary metrics of a data_row consecutively, so
    # rows[0] and rows[1] are the two metrics of the first data_row.
    assert rows[0].youtube_channel_id == rows[1].youtube_channel_id
    assert rows[0].metric_key != rows[1].metric_key
    rows[0].raw_payload["dimensions"]["__probe__"] = "x"
    assert "__probe__" not in rows[1].raw_payload["dimensions"], (
        "sibling metric rows must have isolated audit payloads"
    )


def test_historical_channel_data_flag_changes_row_identity() -> None:
    """includeHistoricalChannelData selects a different dataset (true includes
    pre-link channel history), so two runs differing only in that flag are
    distinct datasets and must NOT collide on the source_row_key (CLAUDE.md rule
    4). The fixture omits the flag (API default False).
    """
    base = _load_fixture("sample_query_response_2026_04.json")
    with_true = {
        **base,
        "query_request": {**base["query_request"], "includeHistoricalChannelData": True},
    }
    default_keys = sorted(
        r.source_row_key for r in YouTubeAnalyticsParser().parse(base, tenant_id=TENANT_ID)
    )
    true_keys = sorted(
        r.source_row_key for r in YouTubeAnalyticsParser().parse(with_true, tenant_id=TENANT_ID)
    )
    assert default_keys  # ingests with the flag omitted (defaults to False)
    assert default_keys != true_keys


def test_non_boolean_historical_flag_raises() -> None:
    """A present-but-non-boolean includeHistoricalChannelData is malformed and
    must fail closed (bool is checked before int, since bool is an int subclass).
    """
    base = _load_fixture("sample_query_response_2026_04.json")
    bad = {
        **base,
        "query_request": {**base["query_request"], "includeHistoricalChannelData": "true"},
    }
    with pytest.raises(ParserError):
        list(YouTubeAnalyticsParser().parse(bad, tenant_id=TENANT_ID))


def test_rejects_non_finite_amount() -> None:
    """Infinity/NaN Decimal strings must fail at the parser, not downstream."""
    payload = _load_fixture("sample_query_response_2026_04.json")
    bad = {**payload}
    # columnHeaders order is [channel, country, estimatedRevenue, grossRevenue];
    # mutate the first row's estimatedRevenue (index 2) to Infinity.
    first_row = list(bad["rows"][0])
    first_row[2] = "Infinity"
    bad["rows"] = [first_row, *bad["rows"][1:]]
    with pytest.raises(ParserError):
        list(YouTubeAnalyticsParser().parse(bad, tenant_id=TENANT_ID))


def test_different_currency_produces_distinct_keys() -> None:
    """The same ids/metrics/dimensions/period fetched in a different currency
    must produce distinct source_row_keys, otherwise one currency pull would
    overwrite the other on the unique upsert key.
    """
    base = _load_fixture("sample_query_response_2026_04.json")
    other = {**base, "query_request": {**base["query_request"], "currency": "EUR"}}
    a = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(base, tenant_id=TENANT_ID))
    b = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(other, tenant_id=TENANT_ID))
    assert set(a).isdisjoint(set(b))


def test_different_filters_produce_distinct_keys() -> None:
    """Two pulls differing only by `filters` must produce distinct keys;
    filtered datasets are distinct even when the returned dimensions match.
    """
    base = _load_fixture("sample_query_response_2026_04.json")
    us = {**base, "query_request": {**base["query_request"], "filters": "country==US"}}
    ca = {**base, "query_request": {**base["query_request"], "filters": "country==CA"}}
    a = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(us, tenant_id=TENANT_ID))
    b = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(ca, tenant_id=TENANT_ID))
    assert set(a).isdisjoint(set(b))


def test_reordered_metrics_produce_same_key() -> None:
    """Reordering the metrics CSV must not change source_row_key — the same
    dataset re-requested with reordered metrics must upsert, not duplicate.
    """
    base = _load_fixture(
        "sample_query_response_2026_04.json"
    )  # metrics: estimatedRevenue,grossRevenue
    reordered = {
        **base,
        "query_request": {**base["query_request"], "metrics": "grossRevenue,estimatedRevenue"},
    }
    a = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(base, tenant_id=TENANT_ID))
    b = sorted(
        r.source_row_key for r in YouTubeAnalyticsParser().parse(reordered, tenant_id=TENANT_ID)
    )
    assert a == b


def test_reordered_filters_produce_same_key() -> None:
    """Reordering filter clauses must not change source_row_key (idempotency)."""
    base = _load_fixture("sample_query_response_2026_04.json")
    p1 = {
        **base,
        "query_request": {**base["query_request"], "filters": "country==US;day==2026-04-01"},
    }
    p2 = {
        **base,
        "query_request": {**base["query_request"], "filters": "day==2026-04-01;country==US"},
    }
    a = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(p1, tenant_id=TENANT_ID))
    b = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(p2, tenant_id=TENANT_ID))
    assert a == b


def test_missing_rows_is_zero_result() -> None:
    """A reports.query response that omits `rows` (no data for the range) must
    yield zero rows, not a ParserError, so sparse-channel runs do not fail.
    """
    base = _load_fixture("sample_query_response_2026_04.json")
    no_rows = {k: v for k, v in base.items() if k != "rows"}
    assert list(YouTubeAnalyticsParser().parse(no_rows, tenant_id=TENANT_ID)) == []


def test_content_owner_id_is_bare_id_not_selector() -> None:
    """content_owner_id stores the bare id (after '=='), not the full
    `contentOwner==...` selector, matching the Reporting parser and keeping
    the field usable for joins/filters. source_account_id keeps the selector.
    """
    payload = _load_fixture("sample_query_response_2026_04.json")  # ids: contentOwner==cms-test-1
    rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))
    assert rows
    assert all(r.source_account_id == "contentOwner==cms-test-1" for r in rows)
    assert all(r.content_owner_id == "cms-test-1" for r in rows)


def test_empty_and_omitted_filters_produce_same_key() -> None:
    """A present-but-empty filter and an omitted filter both mean unfiltered,
    so they must produce the same source_row_key (idempotency)."""
    base = _load_fixture("sample_query_response_2026_04.json")  # no filters key
    empty = {**base, "query_request": {**base["query_request"], "filters": ""}}
    a = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(base, tenant_id=TENANT_ID))
    b = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(empty, tenant_id=TENANT_ID))
    assert a == b


def test_reordered_multi_value_filter_produces_same_key() -> None:
    """Reordering comma-separated values inside a filter clause is semantically
    equivalent and must produce the same source_row_key."""
    base = _load_fixture("sample_query_response_2026_04.json")
    p1 = {**base, "query_request": {**base["query_request"], "filters": "video==id1,id2"}}
    p2 = {**base, "query_request": {**base["query_request"], "filters": "video==id2,id1"}}
    a = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(p1, tenant_id=TENANT_ID))
    b = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(p2, tenant_id=TENANT_ID))
    assert a == b


def test_parse_accepts_numeric_metric_values() -> None:
    """reports.query returns FLOAT/INTEGER metric cells as JSON numbers, not
    strings (per columnHeaders[].dataType). The parser must normalise numeric
    cells to Decimal; rejecting them blocks ingestion of real Analytics revenue
    reports. Floats route through str() so no binary-float artifact appears.
    """
    base = _load_fixture("sample_query_response_2026_04.json")
    # columnHeaders order: [channel, country, estimatedRevenue, grossRevenue].
    # Send the metric cells as JSON numbers (float + int) like the live API.
    numeric = {**base, "rows": [["UC_test_alpha", "US", 1234.56789, 1500]]}
    rows = list(YouTubeAnalyticsParser().parse(numeric, tenant_id=TENANT_ID))
    by_metric = {r.metric_key: r.amount_native for r in rows}
    assert by_metric["estimatedRevenue"] == Decimal("1234.56789")
    assert by_metric["grossRevenue"] == Decimal("1500")
    # Routed through str(): no binary-float noise (Decimal(float) would give
    # "1234.5678899999999..."). This pins the safe normalization path.
    assert str(by_metric["estimatedRevenue"]) == "1234.56789"


def test_rejects_bool_metric_value() -> None:
    """bool is an int subclass but is never a real money value; it must fail
    closed at the parser rather than coerce to Decimal(0)/Decimal(1)."""
    base = _load_fixture("sample_query_response_2026_04.json")
    bad = {**base, "rows": [["UC_test_alpha", "US", True, "1500.000000"]]}
    with pytest.raises(ParserError):
        list(YouTubeAnalyticsParser().parse(bad, tenant_id=TENANT_ID))


def test_rejects_non_numeric_non_string_metric_value() -> None:
    """A metric cell that is neither a number nor a Decimal string (e.g. null)
    must fail closed at the parser, not silently coerce or crash downstream."""
    base = _load_fixture("sample_query_response_2026_04.json")
    bad = {**base, "rows": [["UC_test_alpha", "US", None, "1500.000000"]]}
    with pytest.raises(ParserError):
        list(YouTubeAnalyticsParser().parse(bad, tenant_id=TENANT_ID))


def test_rejects_non_finite_float_amount() -> None:
    """A non-finite float metric cell (inf/nan, which Python's json accepts)
    must be rejected at the parser, mirroring the Decimal-string Infinity case."""
    base = _load_fixture("sample_query_response_2026_04.json")
    bad = {**base, "rows": [["UC_test_alpha", "US", float("inf"), "1500.000000"]]}
    with pytest.raises(ParserError):
        list(YouTubeAnalyticsParser().parse(bad, tenant_id=TENANT_ID))


def test_blank_channel_dimension_raises() -> None:
    """A whitespace-only channel dimension value is not a valid attribution
    identity and must fail closed rather than persist a blank youtube_channel_id
    (the prior isinstance-only check let it through).
    """
    base = _load_fixture("sample_query_response_2026_04.json")
    # columnHeaders order: [channel, country, estimatedRevenue, grossRevenue].
    bad = {**base, "rows": [["   ", "US", "1234.567890", "1500.000000"]]}
    with pytest.raises(ParserError):
        list(YouTubeAnalyticsParser().parse(bad, tenant_id=TENANT_ID))


def test_whitespace_padded_channel_is_trimmed_and_keyed_consistently() -> None:
    """A channel dimension with surrounding whitespace is the same identity as
    the trimmed one: it is stored trimmed and hashes to the same source_row_key,
    so padding drift across reruns cannot duplicate rows (idempotency).
    """
    base = _load_fixture("sample_query_response_2026_04.json")
    clean = list(YouTubeAnalyticsParser().parse(base, tenant_id=TENANT_ID))
    # Pad the channel cell (index 0) of every data row.
    padded_rows = [[f"  {row[0]}  ", *row[1:]] for row in base["rows"]]
    padded = list(
        YouTubeAnalyticsParser().parse({**base, "rows": padded_rows}, tenant_id=TENANT_ID)
    )
    assert {r.youtube_channel_id for r in padded} == {r.youtube_channel_id for r in clean}
    assert sorted(r.source_row_key for r in padded) == sorted(r.source_row_key for r in clean)


def _dimension_contract_payload(
    *,
    declared_dimensions: str,
    returned_dimensions: list[str],
) -> dict[str, object]:
    """Build one internally aligned row for dimension-contract parser tests."""
    dimension_values = {
        "channel": "UC_dimension_contract",
        "month": "2026-04",
        "country": "US",
        "country ": "US",
        "Country": "US",
    }
    return {
        "query_request": {
            "ids": "channel==UC_dimension_contract",
            "startDate": "2026-04-01",
            "endDate": "2026-04-30",
            "metrics": "estimatedRevenue",
            "dimensions": declared_dimensions,
            "currency": "USD",
        },
        "columnHeaders": [
            *[
                {"name": name, "columnType": "DIMENSION", "dataType": "STRING"}
                for name in returned_dimensions
            ],
            {
                "name": "estimatedRevenue",
                "columnType": "METRIC",
                "dataType": "FLOAT",
            },
        ],
        "rows": [
            [
                *[dimension_values[name] for name in returned_dimensions],
                "100.000000",
            ]
        ],
    }


@pytest.mark.parametrize(
    ("declared_dimensions", "returned_dimensions", "error_match"),
    [
        ("channel,country", ["channel"], "missing=.*country"),
        ("channel", ["channel", "country"], "extra=.*country"),
        ("channel,country", ["channel", "country", "country"], "duplicate"),
        ("channel,country", ["channel", "country "], "canonical non-whitespace"),
        ("channel,country", ["channel", "Country"], "missing=.*country.*extra=.*Country"),
    ],
    ids=[
        "missing-country",
        "extra-country",
        "duplicate-country",
        "whitespace-country",
        "case-mismatch-country",
    ],
)
def test_dimension_headers_must_exactly_match_declared_query_dimensions(
    declared_dimensions: str,
    returned_dimensions: list[str],
    error_match: str,
) -> None:
    """Reject response dimension drift before any ParsedSourceRow can escape."""
    payload = _dimension_contract_payload(
        declared_dimensions=declared_dimensions,
        returned_dimensions=returned_dimensions,
    )

    with pytest.raises(ParserError, match=error_match):
        list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))


@pytest.mark.parametrize(
    ("declared_dimensions", "returned_dimensions", "expected_dimensions"),
    [
        (
            "channel,month",
            ["channel", "month"],
            {"channel": "UC_dimension_contract", "month": "2026-04"},
        ),
        (
            "channel,country",
            ["channel", "country"],
            {"channel": "UC_dimension_contract", "country": "US"},
        ),
    ],
    ids=["synthesized-channel-month", "country-evidence"],
)
def test_matching_dimension_headers_preserve_supported_analytics_evidence(
    declared_dimensions: str,
    returned_dimensions: list[str],
    expected_dimensions: dict[str, str],
) -> None:
    """Keep valid worldwide and country-dimensional evidence parseable."""
    payload = _dimension_contract_payload(
        declared_dimensions=declared_dimensions,
        returned_dimensions=returned_dimensions,
    )

    rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))

    assert len(rows) == 1
    assert rows[0].raw_payload["dimensions"] == expected_dimensions


@pytest.mark.parametrize(
    "declared_dimensions",
    ["channel,channel", "channel,Channel", "channel, country", "channel,,country"],
    ids=["duplicate", "case-duplicate", "whitespace", "empty-token"],
)
def test_malformed_declared_dimensions_fail_before_rows_are_emitted(
    declared_dimensions: str,
) -> None:
    """Reject ambiguous request metadata instead of canonicalizing it silently."""
    payload = _dimension_contract_payload(
        declared_dimensions=declared_dimensions,
        returned_dimensions=["channel", "country"],
    )

    with pytest.raises(ParserError, match="query_request.dimensions"):
        list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))


def test_country_rows_preserve_account_channel_country_and_non_projecting_token() -> None:
    """U2 provenance is explicit and keeps every attribution axis unchanged."""
    payload = _load_fixture("sample_query_response_2026_04.json")
    rows = list(YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID))

    assert rows
    assert {row.source_system for row in rows} == {"youtube_analytics"}
    assert {row.report_type for row in rows} == {YOUTUBE_ANALYTICS_COUNTRY_EVIDENCE_REPORT_TYPE}
    assert {row.source_account_id for row in rows} == {"contentOwner==cms-test-1"}
    assert {row.raw_payload["projection_disposition"] for row in rows} == {
        "NON_PROJECTING_EVIDENCE"
    }
    assert {
        (
            row.youtube_channel_id,
            row.raw_payload["dimensions"]["channel"],
            row.raw_payload["dimensions"]["country"],
        )
        for row in rows
    } == {
        ("UC_test_alpha", "UC_test_alpha", "US"),
        ("UC_test_alpha", "UC_test_alpha", "EG"),
        ("UC_test_beta", "UC_test_beta", "GB"),
    }


@pytest.mark.parametrize("country", ["us", "USA", " U", "", None, 123])
def test_country_evidence_rejects_invalid_country_token(country: object) -> None:
    """Malformed country attribution fails the whole report; no silent row skip."""
    payload = _load_fixture("sample_query_response_2026_04.json")
    first = list(payload["rows"][0])
    first[1] = country
    bad = {**payload, "rows": [first]}
    with pytest.raises(ParserError, match="country"):
        list(YouTubeAnalyticsParser().parse(bad, tenant_id=TENANT_ID))


def test_country_request_rejects_response_without_country_dimension() -> None:
    """A mislabeled country query cannot fall through as projecting revenue."""
    payload = _load_fixture("sample_query_response_2026_04.json")
    headers = [header for header in payload["columnHeaders"] if header["name"] != "country"]
    rows = [[row[0], *row[2:]] for row in payload["rows"]]
    bad = {**payload, "columnHeaders": headers, "rows": rows}
    with pytest.raises(ParserError, match="country"):
        list(YouTubeAnalyticsParser().parse(bad, tenant_id=TENANT_ID))


def test_empty_country_response_requires_the_requested_metric_header() -> None:
    """A malformed empty success cannot authorize stale evidence deletion."""
    payload = _load_fixture("sample_query_response_2026_04.json")
    headers = [header for header in payload["columnHeaders"] if header["columnType"] == "DIMENSION"]
    bad = {
        **payload,
        "query_request": {
            **payload["query_request"],
            "metrics": "estimatedRevenue",
            # Declare the post-synthesis shape (channel is injected into the
            # response) so the metric-header contract is what this exercises.
            "dimensions": "channel,country",
        },
        "columnHeaders": headers,
        "rows": [],
    }
    with pytest.raises(ParserError, match="metrics"):
        list(YouTubeAnalyticsParser().parse(bad, tenant_id=TENANT_ID))


def test_empty_country_response_rejects_an_extra_dimension_header() -> None:
    """An empty response cannot widen U2 identity before stale cleanup."""
    payload = _load_fixture("sample_query_response_2026_04.json")
    bad = {
        **payload,
        "query_request": {
            **payload["query_request"],
            "metrics": "estimatedRevenue",
            "dimensions": "country",
        },
        "columnHeaders": [
            {"name": "channel", "columnType": "DIMENSION", "dataType": "STRING"},
            {"name": "country", "columnType": "DIMENSION", "dataType": "STRING"},
            {"name": "month", "columnType": "DIMENSION", "dataType": "STRING"},
            {"name": "estimatedRevenue", "columnType": "METRIC", "dataType": "FLOAT"},
        ],
        "rows": [],
    }
    with pytest.raises(ParserError, match="dimensions"):
        list(YouTubeAnalyticsParser().parse(bad, tenant_id=TENANT_ID))


def test_country_evidence_rejects_an_alternate_requested_metric() -> None:
    """Only the locked U2 estimatedRevenue metric may authorize persistence."""
    payload = _load_fixture("sample_query_response_2026_04.json")
    bad = {
        **payload,
        "query_request": {
            **payload["query_request"],
            "metrics": "grossRevenue",
            "dimensions": "country",
        },
        "columnHeaders": [
            {"name": "channel", "columnType": "DIMENSION", "dataType": "STRING"},
            {"name": "country", "columnType": "DIMENSION", "dataType": "STRING"},
            {"name": "grossRevenue", "columnType": "METRIC", "dataType": "FLOAT"},
        ],
        "rows": [],
    }
    with pytest.raises(ParserError, match="exactly estimatedRevenue"):
        list(YouTubeAnalyticsParser().parse(bad, tenant_id=TENANT_ID))


def test_worldwide_row_is_explicitly_projecting() -> None:
    """The ordinary monthly lane remains distinct from U2 evidence."""
    payload = _load_fixture("sample_query_response_2026_04.json")
    headers = [header for header in payload["columnHeaders"] if header["name"] != "country"]
    rows = [[row[0], *row[2:]] for row in payload["rows"]]
    worldwide = {
        **payload,
        "query_request": {**payload["query_request"], "dimensions": "channel"},
        "columnHeaders": headers,
        "rows": rows,
    }
    parsed = list(YouTubeAnalyticsParser().parse(worldwide, tenant_id=TENANT_ID))
    assert parsed
    assert {row.report_type for row in parsed} == {YOUTUBE_ANALYTICS_PROJECTING_REPORT_TYPE}
    assert {row.raw_payload["projection_disposition"] for row in parsed} == {"PROJECTING"}

    country_keys = {
        row.source_row_key for row in YouTubeAnalyticsParser().parse(payload, tenant_id=TENANT_ID)
    }
    worldwide_keys = {row.source_row_key for row in parsed}
    assert country_keys.isdisjoint(worldwide_keys)
