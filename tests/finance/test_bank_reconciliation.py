"""Tests for monthly bank reconciliation summary behavior."""

from datetime import date
from decimal import Decimal
from importlib import import_module


def bank_entry(**overrides):
    """Factory to create a BankReconciliationEntry with default values and optional overrides."""
    module = import_module("ums_smart_revenue.finance.bank_reconciliation")
    values = {
        "id": "entry-1",
        "month": "2026-03",
        "bank_reference": "bank-transfer-2026-04-22",
        "bank_received_date": date(2026, 4, 22),
        "bank_received_amount": Decimal("928.50"),
        "bank_received_currency": "USD",
        "bank_received_amount_usd": Decimal("928.50"),
        "transfer_fee_usd": Decimal("1.50"),
        "fx_difference_usd": Decimal("0"),
        "notes": "Bank transfer received",
        "source_report_id": "bank-statement-2026-04",
        "recorded_by": "00000000-0000-0000-0000-000000009901",
    }
    values.update(overrides)
    return module.BankReconciliationEntry(**values)


def adsense_payment(**overrides):
    """Factory to create an AdSensePaymentEntry with default values and optional overrides."""
    module = import_module("ums_smart_revenue.finance.adsense_payments")
    values = {
        "id": "payment-1",
        "month": "2026-03",
        "payment_name": "AdSense payment March 2026",
        "payment_date": date(2026, 4, 21),
        "payment_amount": Decimal("930.00"),
        "payment_currency": "USD",
        "payment_status": "PAID",
        "raw_payload": {"paymentId": "pay_2026_03"},
        "source_report_id": "adsense-payment-2026-03",
        "source_account_id": "pub-1",
        "imported_by": None,
    }
    values.update(overrides)
    return module.AdSensePaymentEntry(**values)


def build_summary(*, payments, bank_entries, month="2026-03"):
    """Builds a bank reconciliation summary for a given month
    using provided payments and bank entries."""
    module = import_module("ums_smart_revenue.finance.bank_reconciliation")
    return module.build_month_bank_reconciliation_summary(
        month=month,
        payments=payments,
        bank_entries=bank_entries,
    )


def test_bank_reconciliation_summary_confirms_matching_paid_payment_and_bank_receipt():
    """Test that summary status is BANK_CONFIRMED when payments and bank entries match in amount."""
    summary = build_summary(
        payments=[adsense_payment(payment_amount=Decimal("930.00"))],
        bank_entries=[bank_entry(bank_received_amount_usd=Decimal("930.00"))],
    )

    assert summary.status == "BANK_CONFIRMED"
    assert summary.adsense_paid_amount_usd == Decimal("930.0000")
    assert summary.bank_received_amount_usd == Decimal("930.0000")
    assert summary.bank_gap_usd == Decimal("0.0000")
    assert summary.entry_count == 1
    assert summary.issues == []


def test_bank_reconciliation_summary_detects_bank_gap_and_reports_fees():
    """Test that summary detects bank gap and reports fees when
    bank received amount differs from payment amount."""
    summary = build_summary(
        payments=[adsense_payment(payment_amount=Decimal("930.00"))],
        bank_entries=[
            bank_entry(
                bank_received_amount_usd=Decimal("928.50"),
                transfer_fee_usd=Decimal("1.50"),
                fx_difference_usd=Decimal("0.25"),
            )
        ],
    )

    assert summary.status == "BANK_VARIANCE"
    assert summary.bank_gap_usd == Decimal("1.5000")
    assert summary.transfer_fee_usd == Decimal("1.5000")
    assert summary.fx_difference_usd == Decimal("0.2500")
    assert summary.issues[0].issue_type == "BANK_GAP"


def test_bank_reconciliation_summary_excludes_non_paid_or_non_usd_payments():
    """Test that summary excludes non-paid or non-USD payments and reports appropriate issues."""
    summary = build_summary(
        payments=[
            adsense_payment(payment_status="PENDING"),
            adsense_payment(payment_amount=Decimal("930.00"), payment_currency="EUR"),
        ],
        bank_entries=[bank_entry(bank_received_amount_usd=Decimal("930.00"))],
    )

    assert summary.status == "MISSING_ADSENSE_PAYMENT"
    assert summary.adsense_paid_amount_usd == Decimal("0.0000")
    assert [issue.issue_type for issue in summary.issues] == [
        "MISSING_ADSENSE_PAYMENT",
        "NON_PAID_ADSENSE_PAYMENTS",
        "UNSUPPORTED_PAYMENT_CURRENCY",
    ]
