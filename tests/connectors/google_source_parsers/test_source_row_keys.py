"""source_row_key derivation must be:
 - deterministic across repeated calls with the same inputs;
 - distinct across different inputs;
 - exactly 64 chars (SHA-256 hex digest);
 - source-system-specific (different prefix => different key).
"""

import pytest

from ums_smart_revenue.connectors.google_source_parsers import build_source_row_key


def test_youtube_reporting_key_is_deterministic() -> None:
    key1 = build_source_row_key(
        source_system="youtube_reporting",
        report_type="channel_basic_a2",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={"channel": "UC_x", "country": "US"},
    )
    key2 = build_source_row_key(
        source_system="youtube_reporting",
        report_type="channel_basic_a2",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={"country": "US", "channel": "UC_x"},  # dict order varies
    )
    assert key1 == key2


def test_youtube_analytics_key_uses_query_signature_and_period() -> None:
    key1 = build_source_row_key(
        source_system="youtube_analytics",
        query_signature="estimatedRevenue|channel,country",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={"channel": "UC_y", "country": "EG"},
    )
    key2 = build_source_row_key(
        source_system="youtube_analytics",
        query_signature="estimatedRevenue|channel,country",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={"channel": "UC_y", "country": "EG"},
    )
    assert key1 == key2


def test_adsense_management_key_uses_account_period_dimensions() -> None:
    key = build_source_row_key(
        source_system="adsense_management",
        source_report_id="adsense-report-2026-04",
        account_id="pub-test-001",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={"product": "AFC", "country": "EG"},
    )
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_different_inputs_produce_distinct_keys() -> None:
    # Distinguished by report_type / dimensions. source_report_id and line_index
    # are intentionally NOT part of the youtube_reporting key — see the parser
    # idempotency tests.
    keys = {
        build_source_row_key(
            source_system="youtube_reporting",
            report_type="channel_basic_a2",
            period_start="2026-04-01",
            period_end="2026-04-30",
            dimensions={"k": "v"},
        ),
        build_source_row_key(
            source_system="youtube_reporting",
            report_type="content_owner_basic_a3",
            period_start="2026-04-01",
            period_end="2026-04-30",
            dimensions={"k": "v"},
        ),
        build_source_row_key(
            source_system="youtube_reporting",
            report_type="channel_basic_a2",
            period_start="2026-04-01",
            period_end="2026-04-30",
            dimensions={"k": "w"},
        ),
    }
    assert len(keys) == 3


def test_different_source_systems_produce_distinct_keys() -> None:
    yt = build_source_row_key(
        source_system="youtube_reporting",
        report_type="channel_basic_a2",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={},
    )
    ana = build_source_row_key(
        source_system="youtube_analytics",
        query_signature="",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={},
    )
    ads = build_source_row_key(
        source_system="adsense_management",
        source_report_id="r-1",
        account_id="acct",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={},
    )
    assert len({yt, ana, ads}) == 3


def test_unknown_source_system_raises() -> None:
    with pytest.raises(ValueError, match="source_system"):
        build_source_row_key(source_system="not_a_real_source")  # type: ignore[call-arg]


def test_key_length_is_64_chars() -> None:
    key = build_source_row_key(
        source_system="youtube_reporting",
        report_type="t",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={},
    )
    assert len(key) == 64


def test_dimension_values_with_delimiters_do_not_collide() -> None:
    """Regression: unescaped '&'/'=' in the old canonical form let two
    distinct dimension sets serialise identically and silently overwrite
    each other via the unique upsert key.

    {"a": "b", "c": "d"} and {"a": "b&c=d"} both joined to the string
    "a=b&c=d" before this fix.
    """
    two_dimensions = build_source_row_key(
        source_system="youtube_reporting",
        report_type="t",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={"a": "b", "c": "d"},
    )
    one_dimension = build_source_row_key(
        source_system="youtube_reporting",
        report_type="t",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={"a": "b&c=d"},
    )
    assert two_dimensions != one_dimension


def test_field_boundary_shift_does_not_collide() -> None:
    """Regression: the old '|'-joined canonical form let a value containing
    '|' shift across a field boundary and collide. For AdSense,
    (source_report_id='a', account_id='b|c') and
    (source_report_id='a|b', account_id='c') both produced
    "...|a|b|c|..." before this fix.
    """
    account_owns_pipe = build_source_row_key(
        source_system="adsense_management",
        source_report_id="a",
        account_id="b|c",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={},
    )
    report_owns_pipe = build_source_row_key(
        source_system="adsense_management",
        source_report_id="a|b",
        account_id="c",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={},
    )
    assert account_owns_pipe != report_owns_pipe
