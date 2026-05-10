from decimal import Decimal

from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry
from ums_smart_revenue.finance.revenue_summary import build_adjusted_revenue_summary


def revenue_fact(
    *,
    source_kind: str,
    gross_revenue_usd: str,
    month: str = "2026-03",
    youtube_channel_id: str = "channel-tv-a",
) -> RevenueFactEntry:
    return RevenueFactEntry(
        id=f"fact-{source_kind}",
        month=month,
        youtube_channel_id=youtube_channel_id,
        source_kind=source_kind,
        source_report_id=f"report-{source_kind}",
        gross_revenue_usd=Decimal(gross_revenue_usd),
        net_revenue_usd=None,
        views=0,
        watch_time_minutes=Decimal("0"),
        confidence_score=Decimal("0.9800"),
        imported_by=None,
    )


def manual_override(
    *,
    status: str,
    adjustment_revenue_usd: str,
    month: str = "2026-03",
    youtube_channel_id: str = "channel-tv-a",
) -> RevenueManualOverrideEntry:
    return RevenueManualOverrideEntry(
        id=f"override-{status}-{adjustment_revenue_usd}",
        month=month,
        youtube_channel_id=youtube_channel_id,
        adjustment_revenue_usd=Decimal(adjustment_revenue_usd),
        reason="Revenue correction",
        status=status,
        created_by="00000000-0000-0000-0000-000000009401",
        approved_by="00000000-0000-0000-0000-000000009402" if status == "APPROVED" else None,
        approval_reason="Approved source correction" if status == "APPROVED" else None,
    )


def test_adjusted_revenue_summary_applies_only_approved_manual_overrides():
    summary = build_adjusted_revenue_summary(
        facts=[
            revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00"),
            revenue_fact(source_kind="ADSENSE", gross_revenue_usd="930.00"),
        ],
        manual_overrides=[
            manual_override(status="APPROVED", adjustment_revenue_usd="125.50"),
            manual_override(status="PENDING", adjustment_revenue_usd="-50.00"),
        ],
    )

    assert summary.to_api() == {
        "month": "2026-03",
        "youtube_channel_id": "channel-tv-a",
        "status": "ADJUSTED",
        "primary_source_kind": "YOUTUBE_CMS",
        "baseline_gross_revenue_usd": "1000",
        "approved_manual_override_total_usd": "125.5",
        "adjusted_gross_revenue_usd": "1125.5",
        "approved_manual_override_count": 1,
        "pending_manual_override_count": 1,
    }


def test_adjusted_revenue_summary_marks_net_zero_approved_override_as_adjusted():
    summary = build_adjusted_revenue_summary(
        facts=[revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00")],
        manual_overrides=[
            manual_override(status="APPROVED", adjustment_revenue_usd="50.00"),
            manual_override(status="APPROVED", adjustment_revenue_usd="-50.00"),
        ],
    )

    assert summary.status == "ADJUSTED"
    assert summary.approved_manual_override_total_usd == Decimal("0.00")
    assert summary.approved_manual_override_count == 2


def test_adjusted_revenue_summary_rejects_mixed_months_or_channels():
    try:
        build_adjusted_revenue_summary(
            facts=[
                revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00"),
                revenue_fact(source_kind="ADSENSE", gross_revenue_usd="900.00", month="2026-04"),
            ],
            manual_overrides=[],
        )
    except ValueError as exc:
        assert "inconsistent month/channel" in str(exc)
    else:
        raise AssertionError("Expected mixed fact months to be rejected")

    try:
        build_adjusted_revenue_summary(
            facts=[revenue_fact(source_kind="YOUTUBE_CMS", gross_revenue_usd="1000.00")],
            manual_overrides=[
                manual_override(status="APPROVED", adjustment_revenue_usd="25.00", youtube_channel_id="channel-tv-b")
            ],
        )
    except ValueError as exc:
        assert "inconsistent month/channel" in str(exc)
    else:
        raise AssertionError("Expected mixed override channels to be rejected")
