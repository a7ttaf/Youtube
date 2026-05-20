from decimal import Decimal

from ums_smart_revenue.finance.reconciliation import (
    build_revenue_reconciliation_issue_queue,
    build_revenue_reconciliation_preview,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry


def revenue_fact(
    *,
    source_kind: str,
    gross_revenue_usd: str,
    confidence_score: str,
) -> RevenueFactEntry:
    return RevenueFactEntry(
        id=f"fact-{source_kind}",
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        source_kind=source_kind,
        source_report_id=f"report-{source_kind}",
        gross_revenue_usd=Decimal(gross_revenue_usd),
        net_revenue_usd=None,
        views=0,
        watch_time_minutes=Decimal("0"),
        confidence_score=Decimal(confidence_score),
        imported_by=None,
    )


def test_reconciliation_preview_detects_material_source_variance():
    preview = build_revenue_reconciliation_preview(
        [
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                confidence_score="0.9800",
            ),
            revenue_fact(
                source_kind="ADSENSE",
                gross_revenue_usd="930.00",
                confidence_score="0.9000",
            ),
        ]
    )

    assert preview.to_api() == {
        "month": "2026-03",
        "youtube_channel_id": "channel-tv-a",
        "status": "VARIANCE_DETECTED",
        "primary_source_kind": "YOUTUBE_CMS",
        "primary_gross_revenue_usd": "1000",
        "compared_source_count": 2,
        "gross_revenue_variance_usd": "70",
        "gross_revenue_variance_percent": "0.07",
        "confidence_score": "0.87",
        "issues": [
            {
                "issue_type": "GROSS_REVENUE_VARIANCE",
                "severity": "HIGH",
                "message": (
                    "Gross revenue sources differ by 7.00% for channel-tv-a in 2026-03."
                ),
            }
        ],
    }


def test_reconciliation_preview_marks_single_source_as_insufficient():
    preview = build_revenue_reconciliation_preview(
        [
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                confidence_score="0.9800",
            )
        ]
    )

    assert preview.status == "INSUFFICIENT_SOURCES"
    assert preview.confidence_score == Decimal("0.4900")
    assert preview.issues[0].issue_type == "INSUFFICIENT_SOURCES"


def test_issue_queue_groups_month_facts_and_returns_only_unresolved_channels():
    queue = build_revenue_reconciliation_issue_queue(
        [
            revenue_fact(
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd="1000.00",
                confidence_score="0.9800",
            ),
            revenue_fact(
                source_kind="ADSENSE",
                gross_revenue_usd="930.00",
                confidence_score="0.9000",
            ),
            RevenueFactEntry(
                id="fact-news-cms",
                month="2026-03",
                youtube_channel_id="channel-news-a",
                source_kind="YOUTUBE_CMS",
                source_report_id="report-news-cms",
                gross_revenue_usd=Decimal("500.00"),
                net_revenue_usd=None,
                views=0,
                watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("0.9900"),
                imported_by=None,
            ),
            RevenueFactEntry(
                id="fact-news-adsense",
                month="2026-03",
                youtube_channel_id="channel-news-a",
                source_kind="ADSENSE",
                source_report_id="report-news-adsense",
                gross_revenue_usd=Decimal("500.00"),
                net_revenue_usd=None,
                views=0,
                watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("0.9800"),
                imported_by=None,
            ),
        ],
        month="2026-03",
    )

    assert queue.to_api() == {
        "month": "2026-03",
        "issue_count": 1,
        "items": [
            {
                "month": "2026-03",
                "youtube_channel_id": "channel-tv-a",
                "status": "VARIANCE_DETECTED",
                "primary_source_kind": "YOUTUBE_CMS",
                "primary_gross_revenue_usd": "1000",
                "compared_source_count": 2,
                "gross_revenue_variance_usd": "70",
                "gross_revenue_variance_percent": "0.07",
                "confidence_score": "0.87",
                "issues": [
                    {
                        "issue_type": "GROSS_REVENUE_VARIANCE",
                        "severity": "HIGH",
                        "message": (
                            "Gross revenue sources differ by 7.00% "
                            "for channel-tv-a in 2026-03."
                        ),
                    }
                ],
            }
        ],
    }
