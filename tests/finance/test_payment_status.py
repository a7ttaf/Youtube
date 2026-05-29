"""Tests for the monthly AdSense payment paid/unpaid status breakdown."""
from datetime import date
from decimal import Decimal
from importlib import import_module

import pytest

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
        return next(b for b in summary.status_totals if b.status == status)
    except StopIteration:
        pytest.fail(f"missing {status} status bucket")


def test_month_rollup_lists_all_four_statuses_in_canonical_order():
    """Report all four statuses in canonical order."""
    summary = build([adsense_payment(name="p1", amount="8400.00")])
    assert [b.status for b in summary.status_totals] == [
        "PAID",
        "PENDING",
        "UNPAID",
        "CANCELLED",
    ]
    assert summary.total_payment_count == 1


def test_status_bucket_groups_amounts_per_currency_alphabetically():
    """Group status amounts by currency in alphabetical order."""
    summary = build(
        [
            adsense_payment(
                name="p1", amount="1200.00", status="PENDING", currency="USD"
            ),
            adsense_payment(
                name="p2", amount="300.00", status="PENDING", currency="EUR"
            ),
        ]
    )
    pending = _bucket(summary, "PENDING")
    assert pending.count == 2
    assert [(c.currency, c.amount) for c in pending.currency_totals] == [
        ("EUR", Decimal("300.00")),
        ("USD", Decimal("1200.00")),
    ]


def test_cancelled_amount_appears_in_cancelled_currency_totals():
    """Include cancelled payments in the CANCELLED currency totals."""
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
    """Accept and group non-USD outstanding payments."""
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
    """Sum only pending and unpaid payments as outstanding."""
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
    """Report zeroed status rollups and no accounts for an empty month."""
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
    """Split per-account status rollups by source account ID."""
    summary = build(
        [
            adsense_payment(
                name="p1", amount="8400.00", status="PAID", account="pub-111"
            ),
            adsense_payment(
                name="pend", amount="1200.00", status="PENDING", account="pub-222"
            ),
            adsense_payment(
                name="unp",
                amount="500.00",
                status="UNPAID",
                currency="GBP",
                account="pub-222",
            ),
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
    """Keep the month rollup equal to the sum of account rollups."""
    payments = [
        adsense_payment(
            name="p1", amount="8400.00", status="PAID", account="pub-111"
        ),
        adsense_payment(
            name="pend", amount="1200.00", status="PENDING", account="pub-222"
        ),
        adsense_payment(
            name="unp",
            amount="500.00",
            status="UNPAID",
            currency="GBP",
            account="pub-222",
        ),
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
    """Return the same summary regardless of input payment order."""
    payments = [
        adsense_payment(
            name="g1",
            amount="500.00",
            status="UNPAID",
            currency="GBP",
            account="pub-222",
        ),
        adsense_payment(
            name="p1", amount="8400.00", status="PAID", account="pub-111"
        ),
        adsense_payment(
            name="e1",
            amount="300.00",
            status="PENDING",
            currency="EUR",
            account="pub-222",
        ),
    ]
    first = build(payments)
    second = build(list(reversed(payments)))
    assert first.to_api() == second.to_api()
    assert [a.source_account_id for a in first.accounts] == ["pub-111", "pub-222"]


def test_amount_serialization_preserves_precision_without_scientific_notation():
    """Serialize decimal precision without scientific notation."""
    summary = build([adsense_payment(name="p1", amount="1234.5678", status="PENDING")])
    assert summary.to_api()["outstanding_totals"] == [
        {"currency": "USD", "amount": "1234.5678"}
    ]


def test_payments_from_other_months_are_ignored():
    """Ignore payments outside the requested month."""
    summary = build(
        [
            adsense_payment(
                name="this", amount="100.00", status="PENDING", month="2026-04"
            ),
            adsense_payment(
                name="other", amount="999.00", status="PENDING", month="2026-03"
            ),
        ],
        month="2026-04",
    )
    assert summary.total_payment_count == 1
    assert [(c.currency, c.amount) for c in summary.outstanding_totals] == [
        ("USD", Decimal("100.00"))
    ]
