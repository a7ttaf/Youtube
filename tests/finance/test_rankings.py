from decimal import Decimal

import pytest

from ums_smart_revenue.finance.net_revenue import (
    ChannelNetRevenueSummary,
    MonthNetRevenueSummary,
)
from ums_smart_revenue.finance.rankings import (
    RankingsValidationError,
    build_month_rankings,
)


def _channel(
    channel_id: str,
    *,
    gross: Decimal,
    net: Decimal | None,
    deduction: Decimal | None,
) -> ChannelNetRevenueSummary:
    """Build a minimal ChannelNetRevenueSummary for ranking roll-up tests."""
    return ChannelNetRevenueSummary(
        month="2026-03",
        youtube_channel_id=channel_id,
        status="CALCULATED",
        primary_source_kind="YOUTUBE_CMS",
        baseline_gross_revenue_usd=gross,
        baseline_net_revenue_usd=net,
        approved_manual_override_total_usd=Decimal("0"),
        adjusted_gross_revenue_usd=gross,
        net_revenue_usd=net,
        deduction_amount_usd=deduction,
        channel_direct_deduction_amount_usd=deduction,
        account_allocated_deduction_amount_usd=None,
        deduction_percentage=None,
        confidence="HIGH",
        approved_manual_override_count=0,
        pending_manual_override_count=0,
        issues=[],
    )


def _summary(channels: list[ChannelNetRevenueSummary]) -> MonthNetRevenueSummary:
    """Wrap channel rows in a MonthNetRevenueSummary for ranking input."""
    return MonthNetRevenueSummary(
        month="2026-03",
        status="CALCULATED",
        channel_count=len(channels),
        calculated_channel_count=len(channels),
        missing_net_source_count=0,
        pending_manual_override_count=0,
        total_adjusted_gross_revenue_usd=Decimal("0"),
        total_net_revenue_usd=Decimal("0"),
        total_deduction_amount_usd=Decimal("0"),
        total_channel_direct_deduction_amount_usd=Decimal("0"),
        total_account_allocated_deduction_amount_usd=Decimal("0"),
        unallocated_account_deduction_total_usd=None,
        unallocated_account_issues=None,
        channels=channels,
    )


def test_rolls_channel_metrics_up_to_company_and_sector():
    """Company/sector totals sum their member channels' metrics."""
    summary = _summary(
        [
            _channel("ch-a", gross=Decimal("100"), net=Decimal("80"),
                     deduction=Decimal("20")),
            _channel("ch-b", gross=Decimal("50"), net=Decimal("40"),
                     deduction=Decimal("10")),
        ]
    )
    result = build_month_rankings(
        summary=summary,
        channel_company={"ch-a": "co-1", "ch-b": "co-1"},
        channel_sector={"ch-a": "sec-1", "ch-b": "sec-1"},
        company_names={"co-1": "Company One"},
        sector_names={"sec-1": "Sector One"},
        channel_names={},
        metric="gross",
        limit=10,
    )
    company = result.companies[0]
    assert company.entity_id == "co-1"
    assert company.entity_name == "Company One"
    assert company.gross_revenue_usd == Decimal("150")
    assert company.net_revenue_usd == Decimal("120")
    assert company.deduction_amount_usd == Decimal("30")
    sector = result.sectors[0]
    assert sector.entity_id == "sec-1"
    assert sector.gross_revenue_usd == Decimal("150")


def test_ranks_descending_by_selected_metric_with_rank_numbers():
    """Channels rank descending by the selected metric, rank starts at 1."""
    summary = _summary(
        [
            _channel("ch-low", gross=Decimal("10"), net=Decimal("5"),
                     deduction=Decimal("5")),
            _channel("ch-high", gross=Decimal("90"), net=Decimal("80"),
                     deduction=Decimal("10")),
        ]
    )
    result = build_month_rankings(
        summary=summary,
        channel_company={},
        channel_sector={},
        company_names={},
        sector_names={},
        channel_names={},
        metric="gross",
        limit=10,
    )
    assert [e.entity_id for e in result.channels] == ["ch-high", "ch-low"]
    assert [e.rank for e in result.channels] == [1, 2]


def test_metric_net_selects_net_for_ordering():
    """metric=net orders by net revenue, not gross."""
    summary = _summary(
        [
            _channel("ch-a", gross=Decimal("100"), net=Decimal("1"),
                     deduction=Decimal("99")),
            _channel("ch-b", gross=Decimal("50"), net=Decimal("49"),
                     deduction=Decimal("1")),
        ]
    )
    result = build_month_rankings(
        summary=summary,
        channel_company={},
        channel_sector={},
        company_names={},
        sector_names={},
        channel_names={},
        metric="net",
        limit=10,
    )
    assert [e.entity_id for e in result.channels] == ["ch-b", "ch-a"]


def test_ties_break_by_entity_id_ascending():
    """Equal metric values tie-break by entity id ascending."""
    summary = _summary(
        [
            _channel("ch-z", gross=Decimal("50"), net=Decimal("50"),
                     deduction=Decimal("0")),
            _channel("ch-a", gross=Decimal("50"), net=Decimal("50"),
                     deduction=Decimal("0")),
            _channel("ch-m", gross=Decimal("50"), net=Decimal("50"),
                     deduction=Decimal("0")),
        ]
    )
    result = build_month_rankings(
        summary=summary,
        channel_company={},
        channel_sector={},
        company_names={},
        sector_names={},
        channel_names={},
        metric="gross",
        limit=10,
    )
    assert [e.entity_id for e in result.channels] == ["ch-a", "ch-m", "ch-z"]


def test_none_metric_sinks_to_bottom():
    """A None metric sorts below any real value (Decimal -Infinity sink)."""
    summary = _summary(
        [
            _channel("ch-none", gross=Decimal("100"), net=None, deduction=None),
            _channel("ch-real", gross=Decimal("50"), net=Decimal("1"),
                     deduction=Decimal("49")),
        ]
    )
    result = build_month_rankings(
        summary=summary,
        channel_company={},
        channel_sector={},
        company_names={},
        sector_names={},
        channel_names={},
        metric="net",
        limit=10,
    )
    assert [e.entity_id for e in result.channels] == ["ch-real", "ch-none"]
    assert result.channels[1].net_revenue_usd is None


def test_group_metric_is_none_only_when_all_members_none():
    """A group's net sums non-None members; None only if every member is None."""
    summary = _summary(
        [
            _channel("ch-a", gross=Decimal("100"), net=Decimal("80"),
                     deduction=Decimal("20")),
            _channel("ch-b", gross=Decimal("50"), net=None, deduction=None),
            _channel("ch-c", gross=Decimal("30"), net=None, deduction=None),
        ]
    )
    result = build_month_rankings(
        summary=summary,
        channel_company={
            "ch-a": "co-1",
            "ch-b": "co-1",
            "ch-c": "co-2",
        },
        channel_sector={},
        company_names={"co-1": "One", "co-2": "Two"},
        sector_names={},
        channel_names={},
        metric="gross",
        limit=10,
    )
    companies = {c.entity_id: c for c in result.companies}
    # co-1 has one non-None net (80) -> group net is 80, not None.
    assert companies["co-1"].net_revenue_usd == Decimal("80")
    # co-2's only member has None net -> group net is None.
    assert companies["co-2"].net_revenue_usd is None


def test_top_limit_truncates_each_dimension():
    """Only the top `limit` ranked entries are returned per dimension."""
    channels = [
        _channel(f"ch-{i:02d}", gross=Decimal(str(i)), net=Decimal(str(i)),
                 deduction=Decimal("0"))
        for i in range(20)
    ]
    summary = _summary(channels)
    result = build_month_rankings(
        summary=summary,
        channel_company={},
        channel_sector={},
        company_names={},
        sector_names={},
        channel_names={},
        metric="gross",
        limit=3,
    )
    assert len(result.channels) == 3
    assert [e.entity_id for e in result.channels] == ["ch-19", "ch-18", "ch-17"]


def test_company_name_falls_back_to_raw_id():
    """A company id with no name entry falls back to the raw id."""
    summary = _summary(
        [_channel("ch-a", gross=Decimal("10"), net=Decimal("10"),
                  deduction=Decimal("0"))]
    )
    result = build_month_rankings(
        summary=summary,
        channel_company={"ch-a": "co-unnamed"},
        channel_sector={},
        company_names={},
        sector_names={},
        channel_names={},
        metric="gross",
        limit=10,
    )
    assert result.companies[0].entity_name == "co-unnamed"


def test_bad_metric_raises_validation_error():
    """An unsupported metric raises RankingsValidationError."""
    summary = _summary([])
    with pytest.raises(RankingsValidationError):
        build_month_rankings(
            summary=summary,
            channel_company={},
            channel_sector={},
            company_names={},
            sector_names={},
            channel_names={},
            metric="profit",
            limit=10,
        )


def test_channel_name_resolves_from_map_and_falls_back_to_raw_id():
    """Channel entity_name resolves from channel_names; missing id falls back."""
    summary = _summary(
        [
            _channel("ch-named", gross=Decimal("100"), net=Decimal("90"),
                     deduction=Decimal("10")),
            _channel("ch-unnamed", gross=Decimal("50"), net=Decimal("40"),
                     deduction=Decimal("10")),
        ]
    )
    result = build_month_rankings(
        summary=summary,
        channel_company={},
        channel_sector={},
        company_names={},
        sector_names={},
        channel_names={"ch-named": "Channel Named"},
        metric="gross",
        limit=10,
    )
    by_id = {e.entity_id: e for e in result.channels}
    # Resolved name from the map.
    assert by_id["ch-named"].entity_name == "Channel Named"
    # No map entry -> raw youtube_channel_id fallback.
    assert by_id["ch-unnamed"].entity_name == "ch-unnamed"


def test_out_of_range_limit_raises_validation_error():
    """The service limit guard rejects limit<1 and limit>MAX independently."""
    summary = _summary(
        [_channel("ch-a", gross=Decimal("10"), net=Decimal("10"),
                  deduction=Decimal("0"))]
    )
    with pytest.raises(RankingsValidationError):
        build_month_rankings(
            summary=summary,
            channel_company={},
            channel_sector={},
            company_names={},
            sector_names={},
            channel_names={},
            metric="gross",
            limit=0,
        )
    with pytest.raises(RankingsValidationError):
        build_month_rankings(
            summary=summary,
            channel_company={},
            channel_sector={},
            company_names={},
            sector_names={},
            channel_names={},
            metric="gross",
            limit=101,
        )


def test_to_api_serializes_money_as_strings_and_preserves_none():
    """to_api() emits canonical money strings and preserves None metrics."""
    summary = _summary(
        [_channel("ch-a", gross=Decimal("100.50"), net=None, deduction=None)]
    )
    result = build_month_rankings(
        summary=summary,
        channel_company={},
        channel_sector={},
        company_names={},
        sector_names={},
        channel_names={},
        metric="gross",
        limit=10,
    )
    api = result.to_api()
    assert api["month"] == "2026-03"
    assert api["metric"] == "gross"
    entry = api["channels"][0]
    assert entry["rank"] == 1
    assert entry["entity_id"] == "ch-a"
    assert entry["gross_revenue_usd"] == "100.5"
    assert entry["net_revenue_usd"] is None
    assert entry["deduction_amount_usd"] is None
