from decimal import Decimal
from importlib import import_module

from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry


def revenue_fact(
    *,
    source_kind: str = "YOUTUBE_CMS",
    gross_revenue_usd: str = "1000.00",
    net_revenue_usd: str | None = "880.00",
    confidence_score: str = "0.9800",
    youtube_channel_id: str = "channel-tv-a",
) -> RevenueFactEntry:
    """Build a RevenueFactEntry for test scenarios."""
    return RevenueFactEntry(
        id=f"fact-{source_kind}-{youtube_channel_id}",
        month="2026-03",
        youtube_channel_id=youtube_channel_id,
        source_kind=source_kind,
        source_report_id=f"report-{source_kind}",
        gross_revenue_usd=Decimal(gross_revenue_usd),
        net_revenue_usd=(
            Decimal(net_revenue_usd) if net_revenue_usd is not None else None
        ),
        views=0,
        watch_time_minutes=Decimal("0"),
        confidence_score=Decimal(confidence_score),
        imported_by=None,
    )


def manual_override(
    *,
    adjustment_revenue_usd: str = "50.00",
    status: str = "APPROVED",
    youtube_channel_id: str = "channel-tv-a",
) -> RevenueManualOverrideEntry:
    """Build a RevenueManualOverrideEntry for test scenarios."""
    return RevenueManualOverrideEntry(
        id=f"override-{status}-{youtube_channel_id}",
        month="2026-03",
        youtube_channel_id=youtube_channel_id,
        adjustment_revenue_usd=Decimal(adjustment_revenue_usd),
        reason="Finance correction",
        status=status,
        created_by="00000000-0000-0000-0000-00000000c001",
        approved_by=(
            "00000000-0000-0000-0000-00000000c002" if status == "APPROVED" else None
        ),
        approval_reason="Approved correction" if status == "APPROVED" else None,
    )


def net_revenue_module():
    """Import and return the net_revenue module."""
    return import_module("ums_smart_revenue.finance.net_revenue")


def test_channel_net_revenue_uses_official_net_and_approved_overrides():
    """Channel net revenue reflects approved override on top of official net."""
    summary = net_revenue_module().build_channel_net_revenue_summary(
        facts=[revenue_fact()],
        manual_overrides=[manual_override()],
    )

    assert summary.to_api() == {
        "month": "2026-03",
        "youtube_channel_id": "channel-tv-a",
        "status": "CALCULATED",
        "primary_source_kind": "YOUTUBE_CMS",
        "baseline_gross_revenue_usd": "1000",
        "baseline_net_revenue_usd": "880",
        "approved_manual_override_total_usd": "50",
        "adjusted_gross_revenue_usd": "1050",
        "net_revenue_usd": "930",
        "deduction_amount_usd": "120",
        "channel_direct_deduction_amount_usd": None,
        "account_allocated_deduction_amount_usd": None,
        "deduction_percentage": "11.4286",
        "confidence": "B_RECONCILED",
        "approved_manual_override_count": 1,
        "pending_manual_override_count": 0,
        "issues": [],
    }


def test_channel_net_revenue_reports_missing_net_source_without_inventing_values():
    """Missing net source is reported accurately without fabricating values."""
    summary = net_revenue_module().build_channel_net_revenue_summary(
        facts=[revenue_fact(net_revenue_usd=None)],
        manual_overrides=[],
    )

    assert summary.status == "NET_REVENUE_SOURCE_MISSING"
    assert summary.net_revenue_usd is None
    assert summary.deduction_amount_usd is None
    assert summary.confidence == "E_MISSING"
    assert summary.issues == [
        {
            "issue_type": "NET_REVENUE_SOURCE_MISSING",
            "severity": "HIGH",
            "message": (
                "Primary revenue source YOUTUBE_CMS has no net revenue for "
                "channel-tv-a in 2026-03."
            ),
        }
    ]


def test_month_net_revenue_summary_totals_calculated_channels_only():
    """Month summary only includes channels with calculated net revenue."""
    summary = net_revenue_module().build_month_net_revenue_summary(
        month="2026-03",
        facts=[
            revenue_fact(youtube_channel_id="channel-tv-a"),
            revenue_fact(
                youtube_channel_id="channel-news-a",
                gross_revenue_usd="200.00",
                net_revenue_usd=None,
            ),
        ],
        manual_overrides=[
            manual_override(youtube_channel_id="channel-tv-a"),
            manual_override(
                youtube_channel_id="channel-news-a",
                adjustment_revenue_usd="10.00",
            ),
        ],
    )

    api = summary.to_api()
    assert api["status"] == "PARTIAL"
    assert api["channel_count"] == 2
    assert api["calculated_channel_count"] == 1
    assert api["missing_net_source_count"] == 1
    assert api["total_adjusted_gross_revenue_usd"] == "1260"
    assert api["total_net_revenue_usd"] == "930"
    assert api["total_deduction_amount_usd"] == "120"
