from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.reconciliation import ReconciliationIssue
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry

DEFAULT_PAYMENT_MATCH_TOLERANCE_USD = Decimal("0.01")
YOUTUBE_REVENUE_SOURCE_PRIORITY = {
    "YOUTUBE_CMS": 0,
    "YOUTUBE_ANALYTICS": 1,
}


class PaymentMatchValidationError(ValueError):
    """Exception raised for errors in payment match validation."""
    pass


@dataclass(frozen=True)
"""
Module for summarizing and matching monthly payments.

This module defines the MonthlyPaymentMatchSummary class for aggregating and reconciling
monthly payment data and provides functions to build these summaries and convert them
to API-ready dictionaries.
"""

class MonthlyPaymentMatchSummary:
    """Represents a summary of payment matching for a month, including totals, counts, and reconciliation issues."""
    month: str
    currency: str
    status: str
    youtube_revenue_total_usd: Decimal
    adsense_paid_amount: Decimal
    payment_gap_usd: Decimal | None
    youtube_source_channel_count: int
    missing_youtube_source_channel_count: int
    payment_count: int
    paid_payment_count: int
    non_paid_payment_count: int
    unsupported_payment_currency_count: int
    tolerance_usd: Decimal
    issues: list[ReconciliationIssue]

    def to_api(self) -> dict[str, object]:
        """Convert the summary into a dictionary suitable for API communication."""
        return {
            "month": self.month,
            "currency": self.currency,
            "status": self.status,
            "youtube_revenue_total_usd": _decimal_to_api(
                self.youtube_revenue_total_usd
            ),
            "adsense_paid_amount": _decimal_to_api(self.adsense_paid_amount),
            "payment_gap_usd": _decimal_to_api(self.payment_gap_usd),
            "youtube_source_channel_count": self.youtube_source_channel_count,
            "missing_youtube_source_channel_count": (
                self.missing_youtube_source_channel_count
            ),
            "payment_count": self.payment_count,
            "paid_payment_count": self.paid_payment_count,
            "non_paid_payment_count": self.non_paid_payment_count,
            "unsupported_payment_currency_count": (
                self.unsupported_payment_currency_count
            ),
            "tolerance_usd": _decimal_to_api(self.tolerance_usd),
            "issues": [issue.to_api() for issue in self.issues],
        }


def build_monthly_payment_match_summary(
    *,
    month: str,
    facts: Iterable[RevenueFactEntry],
    payments: Iterable[AdSensePaymentEntry],
    currency: str = "USD",
    tolerance_usd: Decimal = DEFAULT_PAYMENT_MATCH_TOLERANCE_USD,
) -> MonthlyPaymentMatchSummary:
    """Build a MonthlyPaymentMatchSummary for the specified month from revenue facts and AdSense payments.

    Filters facts and payments by the given month and currency, applies reconciliation tolerance,
    and aggregates totals, counts, and reconciliation issues into a summary object.
    """
    if tolerance_usd < 0:
        raise PaymentMatchValidationError("tolerance_usd must be non-negative")
    normalized_currency = normalize_payment_match_currency(currency)
    month_facts = [fact for fact in facts if fact.month == month]
    month_payments = [payment for payment in payments if payment.month == month]
    eligible_currency_payments = [
        payment
        for payment in month_payments
        if payment.payment_currency == normalized_currency
    ]
    unsupported_payment_currency_count = len(month_payments) - len(
        eligible_currency_payments
    )
    youtube_facts_by_channel = _select_youtube_facts_by_channel(month_facts)
    missing_youtube_channels = _channel_ids_without_youtube_sources(month_facts)

    youtube_total = _quantize_money(
        sum(
            (fact.gross_revenue_usd for fact in youtube_facts_by_channel.values()),
            Decimal("0"),
        )
    )
    paid_payments = [
        payment
        for payment in eligible_currency_payments
        if payment.payment_status == "PAID"
    ]
    adsense_paid_amount = _quantize_money(
        sum((payment.payment_amount for payment in paid_payments), Decimal("0"))
    )
    non_paid_payment_count = len(eligible_currency_payments) - len(paid_payments)

    issues: list[ReconciliationIssue] = []
    payment_gap: Decimal | None = None
    if not youtube_facts_by_channel:
        status = "NO_YOUTUBE_REVENUE"
        issues.append(
            ReconciliationIssue(
                issue_type="NO_YOUTUBE_REVENUE_FACTS",
                severity="HIGH",
                message=f"No YouTube revenue facts are available for {month}.",
            )
        )
    elif not paid_payments:
        status = "MISSING_ADSENSE_PAYMENT"
        issues.append(
            ReconciliationIssue(
                issue_type="MISSING_ADSENSE_PAYMENT",
                severity="HIGH",
                message=f"No paid AdSense payment is available for {month}.",
            )
        )
    else:
        payment_gap = _quantize_money(youtube_total - adsense_paid_amount)
        if abs(payment_gap) <= tolerance_usd:
            status = "PAYMENT_MATCHED"
        else:
            status = "PAYMENT_VARIANCE"
            issues.append(
                ReconciliationIssue(
                    issue_type="PAYMENT_GAP",
                    severity="HIGH",
                    message=(
                        "YouTube revenue and paid AdSense payments differ by "
                        f"{_decimal_to_api(abs(payment_gap))} for {month}."
                    ),
                )
            )

    if unsupported_payment_currency_count:
        issues.append(
            ReconciliationIssue(
                issue_type="UNSUPPORTED_PAYMENT_CURRENCY",
                severity="HIGH",
                message=(
                    f"{unsupported_payment_currency_count} AdSense payment "
                    f"record(s) for {month} are not {normalized_currency} and "
                    "were excluded from the match."
                ),
            )
        )

    if non_paid_payment_count:
        issues.append(
            ReconciliationIssue(
                issue_type="NON_PAID_ADSENSE_PAYMENTS",
                severity="MEDIUM",
"""Module for matching payments between AdSense and YouTube revenue facts.
Provides utilities for selecting YouTube revenue facts by channel,
identifying channels without YouTube sources, quantizing monetary values,
and normalizing payment currencies for matching logic.
"""
                message=(
                    f"{non_paid_payment_count} AdSense payment record(s) for "
                    f"{month} are not PAID and were excluded from the match."
                ),
            )
        )

    return MonthlyPaymentMatchSummary(
        month=month,
        currency=normalized_currency,
        status=status,
        youtube_revenue_total_usd=youtube_total,
        adsense_paid_amount=adsense_paid_amount,
        payment_gap_usd=payment_gap,
        youtube_source_channel_count=len(youtube_facts_by_channel),
        missing_youtube_source_channel_count=len(missing_youtube_channels),
        payment_count=len(month_payments),
        paid_payment_count=len(paid_payments),
        non_paid_payment_count=non_paid_payment_count,
        unsupported_payment_currency_count=unsupported_payment_currency_count,
        tolerance_usd=tolerance_usd,
        issues=issues,
    )


def _select_youtube_facts_by_channel(
    facts: list[RevenueFactEntry],
) -> dict[str, RevenueFactEntry]:
    """Selects the highest priority YouTube revenue fact for each channel.
    Returns a mapping from channel ID to the selected RevenueFactEntry
    based on predefined source priority.
    """
    selected: dict[str, RevenueFactEntry] = {}
    for fact in sorted(
        facts,
        key=lambda item: (
            item.youtube_channel_id,
            YOUTUBE_REVENUE_SOURCE_PRIORITY.get(item.source_kind, 99),
            item.source_kind,
        ),
    ):
        if fact.source_kind not in YOUTUBE_REVENUE_SOURCE_PRIORITY:
            continue
        selected.setdefault(fact.youtube_channel_id, fact)
    return selected


def _channel_ids_without_youtube_sources(facts: list[RevenueFactEntry]) -> set[str]:
    """Returns the set of channel IDs that have no YouTube revenue sources.
    Compares all channel IDs against those with recognized YouTube sources.
    """
    all_channel_ids = {fact.youtube_channel_id for fact in facts}
    youtube_channel_ids = {
        fact.youtube_channel_id
        for fact in facts
        if fact.source_kind in YOUTUBE_REVENUE_SOURCE_PRIORITY
    }
    return all_channel_ids - youtube_channel_ids


def _quantize_money(value: Decimal) -> Decimal:
    """Quantizes a Decimal monetary value to four decimal places using half-up rounding.
    Ensures consistent precision for monetary calculations.
    """
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def normalize_payment_match_currency(value: str) -> str:
    """Normalizes and validates a currency string for payment matching.
    Strips whitespace, converts to uppercase, and enforces USD currency.
    """
    normalized = value.strip().upper()
    if not normalized:
        raise PaymentMatchValidationError("currency must not be blank")
    if normalized != "USD":
        raise PaymentMatchValidationError(
            "currency must be USD until exchange-rate support is implemented"
        )
    return normalized
