from decimal import Decimal
from importlib import import_module

import pytest

from ums_smart_revenue.finance.bank_reconciliation import (
    MonthBankReconciliationSummary,
)
from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.payment_matching import MonthlyPaymentMatchSummary
from ums_smart_revenue.finance.reconciliation import ReconciliationIssue
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry


def payment_summary(**overrides):
    values = {
        "month": "2026-03",
        "currency": "USD",
        "status": "PAYMENT_VARIANCE",
        "youtube_revenue_total_usd": Decimal("1000.0000"),
        "adsense_paid_amount": Decimal("900.0000"),
        "payment_gap_usd": Decimal("100.0000"),
        "youtube_source_channel_count": 1,
        "missing_youtube_source_channel_count": 0,
        "payment_count": 1,
        "paid_payment_count": 1,
        "non_paid_payment_count": 0,
        "unsupported_payment_currency_count": 0,
        "tolerance_usd": Decimal("0.0100"),
        "issues": [
            ReconciliationIssue(
                issue_type="PAYMENT_GAP",
                severity="HIGH",
                message="Payment gap.",
            )
        ],
    }
    values.update(overrides)
    return MonthlyPaymentMatchSummary(**values)


def bank_summary(**overrides):
    values = {
        "month": "2026-03",
        "currency": "USD",
        "status": "MISSING_BANK_RECEIPT",
        "adsense_paid_amount_usd": Decimal("900.0000"),
        "bank_received_amount_usd": Decimal("0.0000"),
        "bank_gap_usd": None,
        "transfer_fee_usd": Decimal("0.0000"),
        "fx_difference_usd": Decimal("0.0000"),
        "payment_count": 1,
        "paid_payment_count": 1,
        "non_paid_payment_count": 0,
        "unsupported_payment_currency_count": 0,
        "entry_count": 0,
        "tolerance_usd": Decimal("0.0100"),
        "issues": [
            ReconciliationIssue(
                issue_type="MISSING_BANK_RECEIPT",
                severity="HIGH",
                message="No bank receipt.",
            )
        ],
        "entries": [],
    }
    values.update(overrides)
    return MonthBankReconciliationSummary(**values)


def manual_override(status: str = "APPROVED") -> RevenueManualOverrideEntry:
    return RevenueManualOverrideEntry(
        id=f"override-{status}",
        month="2026-03",
        youtube_channel_id="channel-tv-a",
        adjustment_revenue_usd=Decimal("50.00"),
        reason="Finance correction",
        status=status,
        created_by="00000000-0000-0000-0000-00000000b001",
        approved_by=("00000000-0000-0000-0000-00000000b002" if status == "APPROVED" else None),
        approval_reason="Approved correction" if status == "APPROVED" else None,
    )


def revenue_fact(
    *,
    month: str,
    amount: str,
    channel_id: str = "channel-tv-a",
    fact_id: str | None = None,
    source_report_id: str | None = None,
) -> RevenueFactEntry:
    return RevenueFactEntry(
        id=fact_id or f"{channel_id}-{month}",
        month=month,
        youtube_channel_id=channel_id,
        source_kind="YOUTUBE_CMS",
        source_report_id=source_report_id or f"cms-{month}",
        gross_revenue_usd=Decimal(amount),
        net_revenue_usd=None,
        views=0,
        watch_time_minutes=Decimal("0"),
        confidence_score=Decimal("0.9800"),
        imported_by="00000000-0000-0000-0000-00000000b001",
    )


def build_alerts(**overrides):
    module = import_module("ums_smart_revenue.finance.smart_alerts")
    values = {
        "month": "2026-03",
        "payment_match": payment_summary(),
        "bank_reconciliation": bank_summary(),
        "close_status": "OPEN",
        "manual_overrides": [manual_override()],
    }
    values.update(overrides)
    return module.build_monthly_smart_alert_summary(**values)


def test_smart_alerts_detect_payment_bank_month_and_override_risks():
    summary = build_alerts()

    codes = [alert.code for alert in summary.alerts]
    assert summary.status == "ATTENTION_REQUIRED"
    assert summary.highest_severity == "HIGH"
    assert codes == [
        "PAYMENT_NOT_MATCHED",
        "BANK_AMOUNT_MISSING",
        "UNEXPLAINED_GAP_HIGH",
        "MONTH_NOT_LOCKED",
        "MANUAL_OVERRIDE_USED",
    ]
    assert summary.alerts[0].confidence == "E_MISSING"
    assert summary.alerts[-1].details["approved_override_count"] == 1


def test_smart_alerts_flag_missing_channel_revenue_facts():
    summary = build_alerts(
        payment_match=payment_summary(
            status="NO_YOUTUBE_REVENUE",
            youtube_revenue_total_usd=Decimal("0.0000"),
            payment_gap_usd=None,
        ),
        missing_revenue_fact_channel_count=2,
        missing_revenue_fact_channel_sample=["channel-tv-b", "channel-tv-a"],
    )

    codes = [alert.code for alert in summary.alerts]
    # The coverage alert is inserted after MISSING_REVENUE_SOURCE and before
    # PAYMENT_NOT_MATCHED, matching the per-channel ordering in the spec.
    assert codes.index("CHANNELS_MISSING_REVENUE_FACTS") == (
        codes.index("MISSING_REVENUE_SOURCE") + 1
    )
    assert codes.index("CHANNELS_MISSING_REVENUE_FACTS") < codes.index("PAYMENT_NOT_MATCHED")
    coverage_alert = next(
        (alert for alert in summary.alerts if alert.code == "CHANNELS_MISSING_REVENUE_FACTS"),
        None,
    )
    assert coverage_alert is not None, "expected CHANNELS_MISSING_REVENUE_FACTS alert"
    assert coverage_alert.severity == "HIGH"
    assert coverage_alert.confidence == "E_MISSING"
    assert coverage_alert.details == {
        "channel_count": 2,
        "sample_channel_ids": ["channel-tv-a", "channel-tv-b"],
    }


def test_smart_alerts_cap_missing_channel_sample_at_twenty():
    sample = [f"channel-{index:03d}" for index in range(25)]
    summary = build_alerts(
        missing_revenue_fact_channel_count=25,
        missing_revenue_fact_channel_sample=sample,
    )

    coverage_alert = next(
        (alert for alert in summary.alerts if alert.code == "CHANNELS_MISSING_REVENUE_FACTS"),
        None,
    )
    assert coverage_alert is not None, "expected CHANNELS_MISSING_REVENUE_FACTS alert"
    assert coverage_alert.details["channel_count"] == 25
    assert coverage_alert.details["sample_channel_ids"] == sorted(sample)[:20]


def test_smart_alerts_omit_missing_channel_alert_when_empty():
    summary = build_alerts(
        missing_revenue_fact_channel_count=0,
        missing_revenue_fact_channel_sample=[],
    )

    codes = [alert.code for alert in summary.alerts]
    assert "CHANNELS_MISSING_REVENUE_FACTS" not in codes


def test_smart_alerts_reject_negative_missing_channel_count():
    with pytest.raises(
        ValueError,
        match="missing_revenue_fact_channel_count must be non-negative",
    ):
        build_alerts(missing_revenue_fact_channel_count=-1)


def test_smart_alerts_return_clear_when_payment_bank_and_close_are_clean():
    summary = build_alerts(
        payment_match=payment_summary(
            status="PAYMENT_MATCHED",
            payment_gap_usd=Decimal("0.0000"),
            issues=[],
        ),
        bank_reconciliation=bank_summary(
            status="BANK_CONFIRMED",
            bank_received_amount_usd=Decimal("900.0000"),
            bank_gap_usd=Decimal("0.0000"),
            entry_count=1,
            issues=[],
        ),
        close_status="LOCKED",
        manual_overrides=[],
    )

    assert summary.to_api() == {
        "month": "2026-03",
        "status": "CLEAR",
        "highest_severity": None,
        "alert_count": 0,
        "alerts": [],
    }


def test_smart_alerts_flag_missing_revenue_source():
    summary = build_alerts(
        payment_match=payment_summary(
            status="NO_YOUTUBE_REVENUE",
            youtube_revenue_total_usd=Decimal("0.0000"),
            payment_gap_usd=None,
        )
    )

    assert summary.alerts[0].code == "MISSING_REVENUE_SOURCE"
    assert summary.alerts[0].severity == "HIGH"


def test_smart_alerts_detect_month_over_month_revenue_anomaly():
    summary = build_alerts(
        payment_match=payment_summary(
            status="PAYMENT_MATCHED",
            payment_gap_usd=Decimal("0.0000"),
            issues=[],
        ),
        bank_reconciliation=bank_summary(
            status="BANK_CONFIRMED",
            bank_received_amount_usd=Decimal("900.0000"),
            bank_gap_usd=Decimal("0.0000"),
            entry_count=1,
            issues=[],
        ),
        close_status="LOCKED",
        manual_overrides=[],
        current_revenue_facts=[
            revenue_fact(month="2026-03", amount="900.00"),
        ],
        previous_revenue_facts=[
            revenue_fact(month="2026-02", amount="2000.00"),
        ],
    )

    assert summary.status == "ATTENTION_REQUIRED"
    assert [alert.code for alert in summary.alerts] == ["REVENUE_TREND_ANOMALY"]
    assert summary.alerts[0].details == {
        "threshold_percent": "50",
        "channel_count": 1,
        "channels": [
            {
                "youtube_channel_id": "channel-tv-a",
                "current_gross_revenue_usd": "900",
                "previous_gross_revenue_usd": "2000",
                "change_percent": "-55",
            }
        ],
    }


def test_smart_alerts_ignore_revenue_change_equal_to_threshold():
    summary = build_alerts(
        payment_match=payment_summary(
            status="PAYMENT_MATCHED",
            payment_gap_usd=Decimal("0.0000"),
            issues=[],
        ),
        bank_reconciliation=bank_summary(
            status="BANK_CONFIRMED",
            bank_received_amount_usd=Decimal("900.0000"),
            bank_gap_usd=Decimal("0.0000"),
            entry_count=1,
            issues=[],
        ),
        close_status="LOCKED",
        manual_overrides=[],
        current_revenue_facts=[
            revenue_fact(month="2026-03", amount="1000.00"),
        ],
        previous_revenue_facts=[
            revenue_fact(month="2026-02", amount="2000.00"),
        ],
    )

    assert summary.to_api() == {
        "month": "2026-03",
        "status": "CLEAR",
        "highest_severity": None,
        "alert_count": 0,
        "alerts": [],
    }


def test_smart_alerts_use_stable_primary_fact_tiebreaker():
    summary = build_alerts(
        payment_match=payment_summary(
            status="PAYMENT_MATCHED",
            payment_gap_usd=Decimal("0.0000"),
            issues=[],
        ),
        bank_reconciliation=bank_summary(
            status="BANK_CONFIRMED",
            bank_received_amount_usd=Decimal("900.0000"),
            bank_gap_usd=Decimal("0.0000"),
            entry_count=1,
            issues=[],
        ),
        close_status="LOCKED",
        manual_overrides=[],
        current_revenue_facts=[
            revenue_fact(
                month="2026-03",
                amount="4000.00",
                fact_id="z-current",
                source_report_id="z-report",
            ),
            revenue_fact(
                month="2026-03",
                amount="900.00",
                fact_id="a-current",
                source_report_id="a-report",
            ),
        ],
        previous_revenue_facts=[
            revenue_fact(
                month="2026-02",
                amount="2000.00",
                fact_id="a-previous",
                source_report_id="a-report",
            ),
        ],
    )

    assert [alert.code for alert in summary.alerts] == ["REVENUE_TREND_ANOMALY"]
    assert summary.alerts[0].details["channels"][0]["current_gross_revenue_usd"] == "900"


def test_smart_alerts_reject_negative_high_gap_threshold():
    with pytest.raises(
        ValueError,
        match="high_gap_threshold_usd must be non-negative",
    ):
        build_alerts(high_gap_threshold_usd=Decimal("-1.00"))


def test_smart_alerts_reject_negative_revenue_trend_anomaly_threshold():
    with pytest.raises(
        ValueError,
        match="revenue_trend_anomaly_threshold_percent must be non-negative",
    ):
        build_alerts(revenue_trend_anomaly_threshold_percent=Decimal("-0.01"))
