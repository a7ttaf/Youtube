import json
from datetime import date
from decimal import Decimal
from importlib import resources
from uuid import uuid4

from ums_smart_revenue.connectors.google_source_parsers import YouTubeAnalyticsParser

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
