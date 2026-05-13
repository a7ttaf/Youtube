from decimal import Decimal
from importlib import import_module

from ums_smart_revenue.finance.bank_reconciliation import (
    MonthBankReconciliationSummary,
)
from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.payment_matching import MonthlyPaymentMatchSummary
from ums_smart_revenue.finance.reconciliation import ReconciliationIssue


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
        approved_by=(
            "00000000-0000-0000-0000-00000000b002"
            if status == "APPROVED"
            else None
        ),
        approval_reason="Approved correction" if status == "APPROVED" else None,
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


def test_smart_alerts_reject_negative_high_gap_threshold():
    try:
        build_alerts(high_gap_threshold_usd=Decimal("-1.00"))
    except ValueError as exc:
        assert str(exc) == "high_gap_threshold_usd must be non-negative"
    else:
        raise AssertionError("negative high_gap_threshold_usd should be rejected")
