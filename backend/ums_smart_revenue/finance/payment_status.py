from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api

# Fixed display + iteration order for the four write-validated statuses
# (see ALLOWED_PAYMENT_STATUSES in finance/adsense_payments.py).
CANONICAL_PAYMENT_STATUSES: tuple[str, ...] = ("PAID", "PENDING", "UNPAID", "CANCELLED")
OUTSTANDING_STATUSES: frozenset[str] = frozenset({"PENDING", "UNPAID"})


@dataclass(frozen=True)
class CurrencyAmount:
    """One currency's summed amount (no FX; amounts only added within a currency)."""

    currency: str
    amount: Decimal

    def to_api(self) -> dict[str, object]:
        """Convert this CurrencyAmount to a dict suitable for API usage."""
        return {"currency": self.currency, "amount": _decimal_to_api(self.amount)}


@dataclass(frozen=True)
class PaymentStatusBucket:
    """Count + per-currency totals for one payment status."""

    status: str
    count: int
    currency_totals: list[CurrencyAmount]

    def to_api(self) -> dict[str, object]:
        """Serialize this status bucket into the API response shape."""
        return {
            "status": self.status,
            "count": self.count,
            "currency_totals": [total.to_api() for total in self.currency_totals],
        }


@dataclass(frozen=True)
class AccountPaymentStatus:
    """Per-source_account_id status breakdown."""

    source_account_id: str
    total_payment_count: int
    status_totals: list[PaymentStatusBucket]
    outstanding_totals: list[CurrencyAmount]

    def to_api(self) -> dict[str, object]:
        """Serialize this account breakdown into the API response shape."""
        return {
            "source_account_id": self.source_account_id,
            "total_payment_count": self.total_payment_count,
            "status_totals": [bucket.to_api() for bucket in self.status_totals],
            "outstanding_totals": [
                total.to_api() for total in self.outstanding_totals
            ],
        }


@dataclass(frozen=True)
class MonthlyPaymentStatusSummary:
    """Month-wide rollup plus per-account breakdown of AdSense payment status."""

    month: str
    total_payment_count: int
    status_totals: list[PaymentStatusBucket]
    outstanding_totals: list[CurrencyAmount]
    accounts: list[AccountPaymentStatus]

    def to_api(self) -> dict[str, object]:
        """Convert the payment status summary into a JSON-serializable dictionary."""
        return {
            "month": self.month,
            "total_payment_count": self.total_payment_count,
            "status_totals": [bucket.to_api() for bucket in self.status_totals],
            "outstanding_totals": [
                total.to_api() for total in self.outstanding_totals
            ],
            "accounts": [account.to_api() for account in self.accounts],
        }


# ============================================================================
# Purpose: Aggregate one month's AdSense payments into a paid/unpaid status
#   breakdown (month rollup + per-account), grouping amounts by reported
#   currency with no FX. outstanding = PENDING + UNPAID; CANCELLED is reported
#   for evidence but excluded from outstanding.
# Database/ORM: None (pure function over already-read AdSensePaymentEntry rows).
# Standards: Pure/total; deterministic ordering (canonical status order,
#   alphabetical currencies, ascending source_account_id) so output is testable.
# Blast Radius: Finance read-model only. No writes, no auth, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/adsense_payments.py -> consumes
#     AdSensePaymentEntry; status domain guaranteed by ALLOWED_PAYMENT_STATUSES.
#   - File: backend/ums_smart_revenue/api/adsense.py -> GET /adsense/payments/status.
# ============================================================================
def build_monthly_payment_status_summary(
    *,
    month: str,
    payments: Iterable[AdSensePaymentEntry],
) -> MonthlyPaymentStatusSummary:
    """Build a summary of payment statuses for a given month.

    Filters payments by the specified month and returns a MonthlyPaymentStatusSummary
    containing overall counts, status buckets, outstanding totals, and per-account data.
    """
    month_payments = [payment for payment in payments if payment.month == month]
    return MonthlyPaymentStatusSummary(
        month=month,
        total_payment_count=len(month_payments),
        status_totals=_status_buckets(month_payments, include_all_statuses=True),
        outstanding_totals=_outstanding_totals(month_payments),
        accounts=_accounts(month_payments),
    )


def _status_buckets(
    payments: list[AdSensePaymentEntry],
    *,
    include_all_statuses: bool,
) -> list[PaymentStatusBucket]:
    """Generate buckets of payments grouped by status.

    Returns a list of PaymentStatusBucket, including all canonical statuses if
    include_all_statuses is True, otherwise only statuses present in the data.
    """
    by_status: dict[str, list[AdSensePaymentEntry]] = {}
    for payment in payments:
        by_status.setdefault(payment.payment_status, []).append(payment)
    statuses = (
        CANONICAL_PAYMENT_STATUSES
        if include_all_statuses
        else tuple(
            status for status in CANONICAL_PAYMENT_STATUSES if status in by_status
        )
    )
    return [
        PaymentStatusBucket(
            status=status,
            count=len(by_status.get(status, [])),
            currency_totals=_currency_totals(by_status.get(status, [])),
        )
        for status in statuses
    ]


def _outstanding_totals(payments: list[AdSensePaymentEntry]) -> list[CurrencyAmount]:
    """Compute totals for outstanding payments (pending and unpaid only)."""
    return _currency_totals(
        [
            payment
            for payment in payments
            if payment.payment_status in OUTSTANDING_STATUSES
        ]
    )


def _accounts(payments: list[AdSensePaymentEntry]) -> list[AccountPaymentStatus]:
    """Aggregate payment data per account into AccountPaymentStatus objects."""
    by_account: dict[str, list[AdSensePaymentEntry]] = {}
    for payment in payments:
        by_account.setdefault(payment.source_account_id, []).append(payment)
    return [
        AccountPaymentStatus(
            source_account_id=source_account_id,
            total_payment_count=len(by_account[source_account_id]),
            status_totals=_status_buckets(
                by_account[source_account_id], include_all_statuses=False
            ),
            outstanding_totals=_outstanding_totals(by_account[source_account_id]),
        )
        for source_account_id in sorted(by_account)
    ]


def _currency_totals(payments: list[AdSensePaymentEntry]) -> list[CurrencyAmount]:
    """Sum payment amounts by currency and return a sorted list of results."""
    sums: dict[str, Decimal] = {}
    for payment in payments:
        sums[payment.payment_currency] = (
            sums.get(payment.payment_currency, Decimal("0")) + payment.payment_amount
        )
    return [
        CurrencyAmount(currency=currency, amount=sums[currency])
        for currency in sorted(sums)
    ]
