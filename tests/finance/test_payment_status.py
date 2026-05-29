"""Tests for the monthly AdSense payment paid/unpaid status breakdown."""
from datetime import date
from decimal import Decimal
from importlib import import_module

from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry

MONTH = "2026-04"


def adsense_payment(
    *,
    name: str,
    amount: str,
    status: str = "PAID",
    currency: str = "USD",
    account: str = "pub-111",
    month: str = MONTH,
) -> AdSensePaymentEntry:
    """Create an AdSense payment read-model row for tests."""
    return AdSensePaymentEntry(
        id=f"{account}-{name}",
        source_account_id=account,
        month=month,
        payment_name=name,
"""
Tests for payment status summary builder.

This module contains tests verifying monthly payment status summaries,
including rollups, per-account breakdowns, outstanding totals, and
serialization behavior.
"""
        payment_date=date(2026, 5, 21),
        payment_amount=Decimal(amount),
        payment_currency=currency,
        payment_status=status,
        raw_payload={"paymentId": name},
        source_report_id="adsense-payment-2026-04",
        imported_by=None,
    )


def build(payments, *, month=MONTH):
    """Invoke the builder under test (import_module so collection precedes impl)."""
    module = import_module("ums_smart_revenue.finance.payment_status")
    return module.build_monthly_payment_status_summary(month=month, payments=payments)


def _bucket(summary, status):
    """Return the month-rollup bucket for a status."""
    try:
        b = next(b for b in summary.status_totals if b.status == status)
    except StopIteration:
        return None
    return b


def test_month_rollup_lists_all_four_statuses_in_canonical_order():
    """Test that the monthly rollup includes all four statuses in canonical order and counts payments correctly."""
    summary = build([adsense_payment(name="p1", amount="8400.00")])
    assert [b.status for b in summary.status_totals] == [
        "PAID",
        "PENDING",
        "UNPAID",
        "CANCELLED",
    ]
    assert summary.total_payment_count == 1


def test_status_bucket_groups_amounts_per_currency_alphabetically():
    """Test that status buckets group amounts per currency in alphabetical order of currency codes."""
    summary = build(
        [
            adsense_payment(name="p1", amount="1200.00", status="PENDING", currency="USD"),
            adsense_payment(name="p2", amount="300.00", status="PENDING", currency="EUR"),
        ]
    )
    pending = _bucket(summary, "PENDING")
    assert pending.count == 2
    assert [(c.currency, c.amount) for c in pending.currency_totals] == [
        ("EUR", Decimal("300.00")),
        ("USD", Decimal("1200.00")),
    ]


def test_cancelled_amount_appears_in_cancelled_currency_totals():
    """Test that cancelled payments' amounts appear in CANCELLED status currency totals."""
    # Operator-pinned case 1.
    summary = build(
        [adsense_payment(name="c1", amount="99.00", status="CANCELLED", currency="USD")]
    )
    cancelled = _bucket(summary, "CANCELLED")
    assert cancelled.count == 1
    assert [(c.currency, c.amount) for c in cancelled.currency_totals] == [
        ("USD", Decimal("99.00"))
    ]


def test_cancelled_is_excluded_from_outstanding_totals():
    """Test that cancelled payments are excluded from outstanding totals."""
    # Operator-pinned case 2.
    summary = build(
        [
            adsense_payment(name="c1", amount="99.00", status="CANCELLED"),
            adsense_payment(name="u1", amount="500.00", status="UNPAID", currency="GBP"),
        ]
    )
    assert [(c.currency, c.amount) for c in summary.outstanding_totals] == [
        ("GBP", Decimal("500.00"))
    ]


def test_non_usd_payments_are_accepted_and_grouped_not_errored():
    """Test that non-USD payments are accepted and correctly grouped in outstanding totals."""
    # Operator-pinned case 3.
    summary = build(
        [
            adsense_payment(name="e1", amount="300.00", status="PENDING", currency="EUR"),
            adsense_payment(name="g1", amount="500.00", status="UNPAID", currency="GBP"),
        ]
    )
    assert [(c.currency, c.amount) for c in summary.outstanding_totals] == [
        ("EUR", Decimal("300.00")),
        ("GBP", Decimal("500.00")),
    ]


def test_outstanding_totals_sum_pending_and_unpaid_only():
    """Test that outstanding totals sum amounts for PENDING and UNPAID statuses only, excluding PAID and CANCELLED."""
    summary = build(
        [
            adsense_payment(name="paid", amount="8400.00", status="PAID"),
            adsense_payment(name="pend", amount="1200.00", status="PENDING"),
            adsense_payment(name="unp", amount="500.00", status="UNPAID"),
            adsense_payment(name="canc", amount="99.00", status="CANCELLED"),
        ]
    )
    assert [(c.currency, c.amount) for c in summary.outstanding_totals] == [
        ("USD", Decimal("1700.00"))
    ]


def test_all_paid_month_has_no_outstanding():
    """Test that a fully paid month has no outstanding totals."""
    summary = build([adsense_payment(name="p1", amount="8400.00", status="PAID")])
    assert summary.outstanding_totals == []


def test_empty_month_reports_zeroed_rollup_and_no_accounts():
    """Test that an empty month reports zero counts for all statuses and no accounts."""
    summary = build([])
    assert summary.total_payment_count == 0
    assert [(b.status, b.count, b.currency_totals) for b in summary.status_totals] == [
        ("PAID", 0, []),
        ("PENDING", 0, []),
        ("UNPAID", 0, []),
        ("CANCELLED", 0, []),
    ]
    assert summary.outstanding_totals == []
    assert summary.accounts == []


def test_per_account_breakdown_splits_by_source_account_id():
    """Test that per-account breakdown splits totals by source account ID correctly."""
    summary = build(
        [
            adsense_payment(name="p1", amount="8400.00", status="PAID", account="pub-111"),
            adsense_payment(name="pend", amount="1200.00", status="PENDING", account="pub-222"),
            adsense_payment(name="unp", amount="500.00", status="UNPAID", currency="GBP", account="pub-222"),
        ]
    )
    assert [a.source_account_id for a in summary.accounts] == ["pub-111", "pub-222"]
    pub111 = summary.accounts[0]
    assert pub111.total_payment_count == 1
    assert [b.status for b in pub111.status_totals] == ["PAID"]
    assert pub111.outstanding_totals == []
    pub222 = summary.accounts[1]
    assert pub222.total_payment_count == 2
    assert [b.status for b in pub222.status_totals] == ["PENDING", "UNPAID"]
    assert [(c.currency, c.amount) for c in pub222.outstanding_totals] == [
        ("GBP", Decimal("500.00")),
        ("USD", Decimal("1200.00")),
    ]


def test_rollup_equals_sum_of_account_breakdowns():
    """Test that the overall rollup equals the sum of individual account breakdowns."""
    payments = [
        adsense_payment(name="p1", amount="8400.00", status="PAID", account="pub-111"),
        adsense_payment(name="pend", amount="1200.00", status="PENDING", account="pub-222"),
        adsense_payment(name="unp", amount="500.00", status="UNPAID", currency="GBP", account="pub-222"),
    ]
    summary = build(payments)
    assert summary.total_payment_count == sum(
        a.total_payment_count for a in summary.accounts
    )
    assert [(c.currency, c.amount) for c in summary.outstanding_totals] == [
        ("GBP", Decimal("500.00")),
        ("USD", Decimal("1200.00")),
    ]


def test_determinism_independent_of_input_order():
    """Test that summary generation is deterministic regardless of input payment order."""
    payments = [
        adsense_payment(name="g1", amount="500.00", status="UNPAID", currency="GBP", account="pub-222"),
        adsense_payment(name="p1", amount="8400.00", status="PAID", account="pub-111"),
        adsense_payment(name="e1", amount="300.00", status="PENDING", currency="EUR", account="pub-222"),
    ]
    first = build(payments)
    second = build(list(reversed(payments)))
    assert first.to_api() == second.to_api()
    assert [a.source_account_id for a in first.accounts] == ["pub-111", "pub-222"]


def test_amount_serialization_preserves_precision_without_scientific_notation():
    """Test that serialization to API preserves decimal precision without using scientific notation."""
    summary = build([adsense_payment(name="p1", amount="1234.5678", status="PENDING")])
    assert summary.to_api()["outstanding_totals"] == [
        {"currency": "USD", "amount": "1234.5678"}
    ]


def test_payments_from_other_months_are_ignored():
    """Test that payments from other months are ignored when building summary for a specific month."""
    summary = build(
        [
            adsense_payment(name="this", amount="100.00", status="PENDING", month="2026-04"),
            adsense_payment(name="other", amount="999.00", status="PENDING", month="2026-03"),
        ],
        month="2026-04",
    )
    assert summary.total_payment_count == 1
    assert [(c.currency, c.amount) for c in summary.outstanding_totals] == [
        ("USD", Decimal("100.00"))
    ]
