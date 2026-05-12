from datetime import date
from decimal import Decimal
from importlib import import_module

from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry


def revenue_fact(
    *,
    channel_id: str,
    amount: str,
    source_kind: str = "YOUTUBE_CMS",
) -> RevenueFactEntry:
    return RevenueFactEntry(
        id=f"{channel_id}-{source_kind}",
        month="2026-03",
        youtube_channel_id=channel_id,
        source_kind=source_kind,
        source_report_id=f"{source_kind}-2026-03",
        gross_revenue_usd=Decimal(amount),
        net_revenue_usd=None,
        views=0,
        watch_time_minutes=Decimal("0"),
        confidence_score=Decimal("1"),
        imported_by=None,
    )


def adsense_payment(
    *,
    amount: str,
    status: str = "PAID",
    currency: str = "USD",
) -> AdSensePaymentEntry:
    return AdSensePaymentEntry(
        id=f"payment-{status}-{amount}",
        month="2026-03",
        payment_name=f"AdSense {status} {amount}",
        payment_date=date(2026, 4, 21),
        payment_amount=Decimal(amount),
        payment_currency=currency,
        payment_status=status,
        raw_payload={"paymentId": f"{status}-{amount}"},
        source_report_id="adsense-payment-2026-03",
        imported_by=None,
    )


def build_monthly_payment_match_summary(*, month, facts, payments):
    module = import_module("ums_smart_revenue.finance.payment_matching")
    return module.build_monthly_payment_match_summary(
        month=month,
        facts=facts,
        payments=payments,
    )


def test_monthly_payment_match_summary_matches_youtube_total_to_paid_adsense_payment():
    summary = build_monthly_payment_match_summary(
        month="2026-03",
        facts=[
            revenue_fact(channel_id="channel-tv-a", amount="600.00"),
            revenue_fact(channel_id="channel-tv-b", amount="330.00"),
        ],
        payments=[adsense_payment(amount="930.00")],
    )

    assert summary.status == "PAYMENT_MATCHED"
    assert summary.youtube_revenue_total_usd == Decimal("930.0000")
    assert summary.adsense_paid_amount == Decimal("930.0000")
    assert summary.payment_gap_usd == Decimal("0.0000")
    assert summary.youtube_source_channel_count == 2
    assert summary.paid_payment_count == 1
    assert summary.issues == []


def test_monthly_payment_match_summary_detects_payment_gap():
    summary = build_monthly_payment_match_summary(
        month="2026-03",
        facts=[revenue_fact(channel_id="channel-tv-a", amount="1000.00")],
        payments=[adsense_payment(amount="930.00")],
    )

    assert summary.status == "PAYMENT_VARIANCE"
    assert summary.payment_gap_usd == Decimal("70.0000")
    assert summary.issues[0].issue_type == "PAYMENT_GAP"
    assert (
        "YouTube revenue and paid AdSense payments differ by 70"
        in summary.issues[0].message
    )


def test_monthly_payment_match_summary_ignores_non_paid_adsense_payments_for_match():
    summary = build_monthly_payment_match_summary(
        month="2026-03",
        facts=[revenue_fact(channel_id="channel-tv-a", amount="930.00")],
        payments=[
            adsense_payment(amount="930.00", status="PENDING"),
            adsense_payment(amount="930.00", status="UNPAID"),
        ],
    )

    assert summary.status == "MISSING_ADSENSE_PAYMENT"
    assert summary.adsense_paid_amount == Decimal("0.0000")
    assert summary.payment_gap_usd is None
    assert [issue.issue_type for issue in summary.issues] == [
        "MISSING_ADSENSE_PAYMENT",
        "NON_PAID_ADSENSE_PAYMENTS",
    ]


def test_monthly_payment_match_summary_excludes_non_usd_adsense_payments():
    summary = build_monthly_payment_match_summary(
        month="2026-03",
        facts=[revenue_fact(channel_id="channel-tv-a", amount="930.00")],
        payments=[adsense_payment(amount="930.00", currency="EUR")],
    )

    assert summary.status == "MISSING_ADSENSE_PAYMENT"
    assert summary.currency == "USD"
    assert summary.adsense_paid_amount == Decimal("0.0000")
    assert summary.unsupported_payment_currency_count == 1
    assert [issue.issue_type for issue in summary.issues] == [
        "MISSING_ADSENSE_PAYMENT",
        "UNSUPPORTED_PAYMENT_CURRENCY",
    ]


def test_monthly_payment_match_summary_requires_youtube_revenue_sources():
    summary = build_monthly_payment_match_summary(
        month="2026-03",
        facts=[
            revenue_fact(
                channel_id="channel-tv-a",
                amount="930.00",
                source_kind="ADSENSE",
            )
        ],
        payments=[adsense_payment(amount="930.00")],
    )

    assert summary.status == "NO_YOUTUBE_REVENUE"
    assert summary.youtube_revenue_total_usd == Decimal("0.0000")
    assert summary.payment_gap_usd is None
    assert summary.missing_youtube_source_channel_count == 1
    assert summary.issues[0].issue_type == "NO_YOUTUBE_REVENUE_FACTS"
