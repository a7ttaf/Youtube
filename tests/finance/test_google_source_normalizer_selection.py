"""Pure-function tests for select_canonical_row + module constants.

No DB, no session. Verifies the rule wiring per source_system and the
frozen-mapping contract.
"""

from datetime import date
from decimal import Decimal

import pytest

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    GoogleRevenueSourceRowEntry,
)
from ums_smart_revenue.finance.google_source_normalizer import (
    CANONICAL_METRIC_RULE,
    SOURCE_SYSTEM_TO_SOURCE_KIND,
    select_canonical_row,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactSourceKind


def test_source_system_to_source_kind_mapping_covers_three_supported_systems():
    assert dict(SOURCE_SYSTEM_TO_SOURCE_KIND) == {
        "youtube_reporting": RevenueFactSourceKind.YOUTUBE_CMS,
        "youtube_analytics": RevenueFactSourceKind.YOUTUBE_ANALYTICS,
        "adsense_management": RevenueFactSourceKind.ADSENSE,
    }


def test_canonical_metric_rule_mapping_is_frozen():
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
) -> GoogleRevenueSourceRowEntry:
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
        source_report_id="r-1",
        raw_file_id=None,
        raw_payload={},
        imported_by=None,
        ingested_at=date(2026, 4, 1),  # type: ignore[arg-type]
    )


def test_select_canonical_row_youtube_reporting_picks_estimatedRevenue():  # noqa: N802
    rows = [_entry(source_system="youtube_reporting", metric_key="estimatedRevenue", source_row_key="a" * 64)]
    canonical, rest = select_canonical_row(rows)
    assert canonical is rows[0]
    assert rest == []


def test_select_canonical_row_youtube_analytics_picks_estimatedRevenue():  # noqa: N802
    rows = [_entry(source_system="youtube_analytics", metric_key="estimatedRevenue", source_row_key="b" * 64)]
    canonical, rest = select_canonical_row(rows)
    assert canonical is rows[0]
    assert rest == []


def test_select_canonical_row_adsense_prefers_PAID_AMOUNT_over_ESTIMATED_EARNINGS():  # noqa: N802
    paid = _entry(source_system="adsense_management", metric_key="PAID_AMOUNT", source_row_key="c" * 64)
    earnings = _entry(source_system="adsense_management", metric_key="ESTIMATED_EARNINGS", source_row_key="d" * 64)
    canonical, rest = select_canonical_row([earnings, paid])
    assert canonical is paid
    assert rest == [earnings]


def test_select_canonical_row_adsense_falls_back_to_ESTIMATED_EARNINGS_when_no_PAID_AMOUNT():  # noqa: N802
    earnings = _entry(source_system="adsense_management", metric_key="ESTIMATED_EARNINGS", source_row_key="e" * 64)
    canonical, rest = select_canonical_row([earnings])
    assert canonical is earnings
    assert rest == []


def test_select_canonical_row_returns_none_when_no_preferred_metric_present():
    unpaid = _entry(source_system="adsense_management", metric_key="UNPAID_AMOUNT", source_row_key="f" * 64)
    canonical, rest = select_canonical_row([unpaid])
    assert canonical is None
    assert rest == [unpaid]


def test_select_canonical_row_tie_break_is_deterministic_by_source_row_key_asc():
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
