import json
from datetime import date
from decimal import Decimal
from importlib import resources
from uuid import uuid4

import pytest

from ums_smart_revenue.connectors.google_source_parsers import (
    ParserError,
    YouTubeReportingParser,
)

TENANT_ID = uuid4()


def _load_fixture(name: str) -> dict[str, object]:
    ref = resources.files("tests.connectors._fixtures.youtube_reporting").joinpath(name)
    with ref.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def test_parse_emits_one_row_per_input_line() -> None:
    payload = _load_fixture("sample_estimated_revenue_2026_04.json")
    parser = YouTubeReportingParser()
    rows = list(parser.parse(payload, tenant_id=TENANT_ID))
    assert len(rows) == 3


def test_parse_preserves_amount_and_currency_exactly() -> None:
    payload = _load_fixture("sample_estimated_revenue_2026_04.json")
    rows = list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))
    amounts_by_channel = {(r.youtube_channel_id, r.amount_native, r.currency_code) for r in rows}
    assert (
        ("UC_test_alpha", Decimal("1234.560000"), "USD") in amounts_by_channel
    )
    assert (
        ("UC_test_beta", Decimal("456.780000"), "GBP") in amounts_by_channel
    )


def test_parse_sets_value_kind_estimated_and_source_system_youtube_reporting() -> None:
    payload = _load_fixture("sample_estimated_revenue_2026_04.json")
    rows = list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))
    assert all(r.source_system == "youtube_reporting" for r in rows)
    assert all(r.value_kind == "estimated" for r in rows)


def test_parse_sets_period_and_report_month() -> None:
    payload = _load_fixture("sample_estimated_revenue_2026_04.json")
    rows = list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))
    for row in rows:
        assert row.period_start == date(2026, 4, 1)
        assert row.period_end == date(2026, 4, 30)
        assert row.report_month == "2026-04"


def test_parse_carries_source_report_id_and_content_owner() -> None:
    payload = _load_fixture("sample_estimated_revenue_2026_04.json")
    rows = list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))
    for row in rows:
        assert row.source_report_id == "yt-rep-2026-04-channel-estimated-001"
        assert row.content_owner_id == "cms-test-1"


def test_source_row_key_is_deterministic_across_reruns() -> None:
    first = list(YouTubeReportingParser().parse(
        _load_fixture("sample_estimated_revenue_2026_04.json"), tenant_id=TENANT_ID,
    ))
    second = list(YouTubeReportingParser().parse(
        _load_fixture("sample_estimated_revenue_2026_04_rerun.json"), tenant_id=TENANT_ID,
    ))
    first_keys = sorted(r.source_row_key for r in first)
    second_keys = sorted(r.source_row_key for r in second)
    assert first_keys == second_keys
    for key in first_keys:
        assert len(key) == 64


def test_raw_payload_carries_the_input_row_dict() -> None:
    payload = _load_fixture("sample_estimated_revenue_2026_04.json")
    rows = list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))
    for row in rows:
        assert "dimensions" in row.raw_payload
        assert "metrics" in row.raw_payload


def test_raw_payload_is_isolated_from_caller_mutation() -> None:
    """parse() must deep-copy each source row into raw_payload: mutating a
    NESTED object on the caller's input payload after parsing must not rewrite
    the stored audit evidence. A shallow dict(row) copy would leave the nested
    dimensions/metrics objects aliased to the caller.
    """
    payload = _load_fixture("sample_estimated_revenue_2026_04.json")
    rows = list(YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))
    input_rows = payload["rows"]
    assert isinstance(input_rows, list) and input_rows
    for input_row in input_rows:
        input_row["dimensions"]["channel"] = "UC_MUTATED_AFTER_PARSE"
    for row in rows:
        assert row.raw_payload["dimensions"]["channel"] != "UC_MUTATED_AFTER_PARSE"


def test_missing_content_owner_falls_back_to_channel() -> None:
    """content_owner is optional: a channel-scoped report omits it. When absent,
    source_account_id falls back to the channel id (a real identity, not a
    sentinel) and content_owner_id is None.
    """
    payload = _load_fixture("sample_estimated_revenue_2026_04.json")
    channel_scoped = {**payload}
    channel_scoped["rows"] = [
        {**r, "dimensions": {k: v for k, v in r["dimensions"].items() if k != "content_owner"}}
        for r in channel_scoped["rows"]
    ]
    rows = list(YouTubeReportingParser().parse(channel_scoped, tenant_id=TENANT_ID))
    assert rows  # ingests rather than failing closed
    for row in rows:
        assert row.content_owner_id is None
        assert row.source_account_id == row.youtube_channel_id


def test_non_string_content_owner_raises_parser_error() -> None:
    """A present-but-non-string content_owner is malformed and must fail closed."""
    payload = _load_fixture("sample_estimated_revenue_2026_04.json")
    bad = {**payload}
    bad["rows"] = [
        {**r, "dimensions": {**r["dimensions"], "content_owner": 123}} for r in bad["rows"]
    ]
    with pytest.raises(ParserError):
        list(YouTubeReportingParser().parse(bad, tenant_id=TENANT_ID))


def test_rejects_non_finite_amount() -> None:
    """NaN/Infinity Decimal strings must fail at the parser, not downstream."""
    payload = _load_fixture("sample_estimated_revenue_2026_04.json")
    bad = {**payload}
    bad["rows"] = [
        {**bad["rows"][0], "metrics": {**bad["rows"][0]["metrics"], "estimatedRevenue": "NaN"}},
    ]
    with pytest.raises(ParserError):
        list(YouTubeReportingParser().parse(bad, tenant_id=TENANT_ID))


def test_line_index_does_not_affect_source_row_key() -> None:
    """line_index is positional/unstable across YouTube Reporting backfills, so
    it must NOT affect source_row_key. The same logical rows (same report id +
    dimensions) keep stable keys even when their line_index values change —
    otherwise a reordered backfill would insert duplicates instead of upserting.
    """
    payload = _load_fixture("sample_estimated_revenue_2026_04.json")
    shifted = {
        **payload,
        "rows": [
            {**row, "line_index": row["line_index"] + 100} for row in payload["rows"]
        ],
    }
    a = sorted(r.source_row_key for r in YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))
    b = sorted(r.source_row_key for r in YouTubeReportingParser().parse(shifted, tenant_id=TENANT_ID))
    assert a == b


def test_report_id_does_not_affect_source_row_key() -> None:
    """A backfilled/corrected report gets a NEW report_id for the same period;
    the key must stay stable (report_type + period + dimensions) so the
    correction UPDATES the prior rows instead of inserting duplicates.
    """
    payload = _load_fixture("sample_estimated_revenue_2026_04.json")
    backfill = {
        **payload,
        "report_metadata": {
            **payload["report_metadata"],
            "report_id": "yt-rep-BACKFILL-correction-999",
        },
    }
    a = sorted(r.source_row_key for r in YouTubeReportingParser().parse(payload, tenant_id=TENANT_ID))
    b = sorted(r.source_row_key for r in YouTubeReportingParser().parse(backfill, tenant_id=TENANT_ID))
    assert a == b


def test_distinct_currency_rows_do_not_collide_on_source_row_key() -> None:
    """Two rows with identical report_type/period/dimensions but a different
    currency are distinct monetary rows and must produce DISTINCT
    source_row_keys, so an idempotent upsert keeps both instead of one silently
    overwriting the other (CLAUDE.md rule 4: preserve every financial number's
    source). Before currency was added to the youtube_reporting key, these
    collided onto a single upsert key.
    """
    payload = _load_fixture("sample_estimated_revenue_2026_04.json")
    base = payload["rows"][0]
    usd_row = {**base, "metrics": {**base["metrics"], "currencyCode": "USD"}}
    eur_row = {
        **base,
        "line_index": base["line_index"] + 1,
        "metrics": {**base["metrics"], "currencyCode": "EUR"},
    }
    only_currency_differs = {**payload, "rows": [usd_row, eur_row]}
    rows = list(YouTubeReportingParser().parse(only_currency_differs, tenant_id=TENANT_ID))
    assert {r.currency_code for r in rows} == {"USD", "EUR"}
    assert rows[0].source_row_key != rows[1].source_row_key
