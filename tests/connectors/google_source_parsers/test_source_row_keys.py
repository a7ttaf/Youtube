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
        currency="USD",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={"channel": "UC_y", "country": "EG"},
    )
    key2 = build_source_row_key(
        source_system="youtube_analytics",
        query_signature="estimatedRevenue|channel,country",
        currency="USD",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={"channel": "UC_y", "country": "EG"},
    )
    assert key1 == key2


def test_adsense_management_key_uses_metric_account_period_dimensions() -> None:
    key = build_source_row_key(
        source_system="adsense_management",
        metric_key="ESTIMATED_EARNINGS",
        currency="USD",
        account_id="pub-test-001",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={"product": "AFC", "country": "EG"},
    )
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


def test_adsense_management_key_includes_currency() -> None:
    """Currency is part of the AdSense row identity: the same
    metric/account/period/dimensions reported in a different currency must NOT
    collapse to one upsert key (mirrors the youtube_analytics key).
    """
    kwargs = dict(
        source_system="adsense_management",
        metric_key="PAID_AMOUNT",
        account_id="pub-1",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={"product": "AFC"},
    )
    assert (
        build_source_row_key(currency="USD", **kwargs)
        != build_source_row_key(currency="EUR", **kwargs)
    )


def test_currency_is_required_for_currency_keyed_branches() -> None:
    """currency is a financial-identity field for the currency-keyed source
    systems, so omitting it must fail closed (loud KeyError) instead of silently
    hashing None and risking a distinct-currency upsert-key collision (CLAUDE.md
    rule 4). filters stays optional, so this guard is currency-specific.
    """
    for source_system, identity in (
        ("youtube_analytics", {"query_signature": "estimatedRevenue|channel"}),
        ("adsense_management", {"metric_key": "PAID_AMOUNT", "account_id": "pub-1"}),
    ):
        with pytest.raises(KeyError, match="currency"):
            build_source_row_key(
                source_system=source_system,
                period_start="2026-04-01",
                period_end="2026-04-30",
                dimensions={},
                **identity,
            )


def test_adsense_management_key_excludes_run_specific_report_id() -> None:
    """The AdSense key must NOT depend on a run-specific report_id: it is not a
    parameter of the key at all. The same metric/account/period/dimensions
    yields the same key regardless of which report generation produced it, so a
    regenerated report updates in place instead of inserting duplicate revenue.
    """
    kwargs = dict(
        source_system="adsense_management",
        currency="USD",
        account_id="pub-test-001",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={"product": "AFC"},
    )
    assert (
        build_source_row_key(metric_key="PAID_AMOUNT", **kwargs)
        != build_source_row_key(metric_key="ESTIMATED_EARNINGS", **kwargs)
    ), "different metrics for the same row must stay distinct"
    assert (
        build_source_row_key(metric_key="PAID_AMOUNT", **kwargs)
        == build_source_row_key(metric_key="PAID_AMOUNT", **kwargs)
    ), "same logical identity must be stable"


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
        currency="USD",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={},
    )
    ads = build_source_row_key(
        source_system="adsense_management",
        metric_key="PAID_AMOUNT",
        currency="USD",
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
    (metric_key='a', account_id='b|c') and
    (metric_key='a|b', account_id='c') both produced
    "...|a|b|c|..." before the JSON canonical form fixed it.
    """
    account_owns_pipe = build_source_row_key(
        source_system="adsense_management",
        metric_key="a",
        currency="USD",
        account_id="b|c",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={},
    )
    metric_owns_pipe = build_source_row_key(
        source_system="adsense_management",
        metric_key="a|b",
        currency="USD",
        account_id="c",
        period_start="2026-04-01",
        period_end="2026-04-30",
        dimensions={},
    )
    assert account_owns_pipe != metric_owns_pipe
