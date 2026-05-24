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
    a = list(YouTubeAnalyticsParser().parse(
        _load_fixture("sample_query_response_2026_04.json"), tenant_id=TENANT_ID,
    ))
    b = list(YouTubeAnalyticsParser().parse(
        _load_fixture("sample_query_response_2026_04_rerun.json"), tenant_id=TENANT_ID,
    ))
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
    other = {**base, "query_request": {**base["query_request"], "ids": "contentOwner==cms-test-OTHER"}}

    a_keys = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(base, tenant_id=TENANT_ID))
    b_keys = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(other, tenant_id=TENANT_ID))

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
    base = _load_fixture("sample_query_response_2026_04.json")  # metrics: estimatedRevenue,grossRevenue
    reordered = {**base, "query_request": {**base["query_request"], "metrics": "grossRevenue,estimatedRevenue"}}
    a = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(base, tenant_id=TENANT_ID))
    b = sorted(r.source_row_key for r in YouTubeAnalyticsParser().parse(reordered, tenant_id=TENANT_ID))
    assert a == b


def test_reordered_filters_produce_same_key() -> None:
    """Reordering filter clauses must not change source_row_key (idempotency)."""
    base = _load_fixture("sample_query_response_2026_04.json")
    p1 = {**base, "query_request": {**base["query_request"], "filters": "country==US;day==2026-04-01"}}
    p2 = {**base, "query_request": {**base["query_request"], "filters": "day==2026-04-01;country==US"}}
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
