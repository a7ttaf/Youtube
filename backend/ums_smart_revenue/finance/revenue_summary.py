from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.reconciliation import SOURCE_PRIORITY
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry


@dataclass(frozen=True)
"""
Module for building and converting adjusted revenue summaries for YouTube channels.
Provides data model and utilities to generate summaries with baseline revenue, manual overrides,
and final adjusted revenue, and convert them to API-friendly formats.
"""

class AdjustedRevenueSummary:
    """
    Represents a summary of adjusted revenue for a YouTube channel in a specific month,
    including baseline revenue, approved manual overrides, and final adjusted totals.
    """
    month: str
    youtube_channel_id: str
    status: str
    primary_source_kind: str | None
    baseline_gross_revenue_usd: Decimal
    approved_manual_override_total_usd: Decimal
    adjusted_gross_revenue_usd: Decimal
    approved_manual_override_count: int
    pending_manual_override_count: int

    def to_api(self) -> dict[str, object]:
        """
        Convert this AdjustedRevenueSummary into a dictionary suitable for API responses,
        converting Decimal fields to JSON-friendly formats.
        """
        return {
            "month": self.month,
            "youtube_channel_id": self.youtube_channel_id,
            "status": self.status,
            "primary_source_kind": self.primary_source_kind,
            "baseline_gross_revenue_usd": _decimal_to_api(
                self.baseline_gross_revenue_usd
            ),
            "approved_manual_override_total_usd": _decimal_to_api(
                self.approved_manual_override_total_usd
            ),
            "adjusted_gross_revenue_usd": _decimal_to_api(
                self.adjusted_gross_revenue_usd
            ),
            "approved_manual_override_count": self.approved_manual_override_count,
            "pending_manual_override_count": self.pending_manual_override_count,
        }


def build_adjusted_revenue_summary(
    *,
    facts: Iterable[RevenueFactEntry],
    manual_overrides: Iterable[RevenueManualOverrideEntry],
    month: str | None = None,
    youtube_channel_id: str | None = None,
) -> AdjustedRevenueSummary:
    """
    Build an AdjustedRevenueSummary object from provided revenue facts and manual overrides.

    Parameters:
        facts: Iterable of RevenueFactEntry objects representing revenue data from various sources.
        manual_overrides: Iterable of RevenueManualOverrideEntry objects for approved or pending manual adjustments.
        month: Optional month string to resolve the period if facts are empty.
        youtube_channel_id: Optional channel ID to resolve if facts are empty.

    Returns:
        AdjustedRevenueSummary containing consolidated baseline and adjusted revenue and counts.

    Raises:
        ValueError: If month or youtube_channel_id are not provided when facts list is empty.
    """
    fact_list = sorted(
        facts,
        key=lambda fact: (SOURCE_PRIORITY.get(fact.source_kind, 99), fact.source_kind),
    )
    override_list = list(manual_overrides)
    if fact_list:
        resolved_month = month or fact_list[0].month
        resolved_channel_id = youtube_channel_id or fact_list[0].youtube_channel_id
    else:
        if month is None or youtube_channel_id is None:
            raise ValueError(
                "month and youtube_channel_id are required when "
                "no revenue facts are provided"
            )
        resolved_month = month
        resolved_channel_id = youtube_channel_id

    _validate_same_period_and_channel(
        fact_list, month=resolved_month, youtube_channel_id=resolved_channel_id
    )
    _validate_same_period_and_channel(
        override_list, month=resolved_month, youtube_channel_id=resolved_channel_id
    )

    if fact_list:
        primary = fact_list[0]
        primary_source_kind = primary.source_kind
        baseline = primary.gross_revenue_usd
    else:
        primary_source_kind = None
        baseline = Decimal("0")

    approved = [override for override in override_list if override.status == "APPROVED"]
    pending = [override for override in override_list if override.status == "PENDING"]
    approved_total = sum(
        (override.adjustment_revenue_usd for override in approved), Decimal("0")
    )
    adjusted = baseline + approved_total
    if approved:
        summary_status = "ADJUSTED"
    elif pending:
        summary_status = "PENDING_OVERRIDE_REVIEW"
    elif fact_list:
        summary_status = "BASELINE"
    else:
        summary_status = "NO_FACTS"
    return AdjustedRevenueSummary(
        month=resolved_month,
        youtube_channel_id=resolved_channel_id,
        status=summary_status,
        primary_source_kind=primary_source_kind,
        baseline_gross_revenue_usd=baseline,
        approved_manual_override_total_usd=approved_total,
        adjusted_gross_revenue_usd=adjusted,
        approved_manual_override_count=len(approved),
        pending_manual_override_count=len(pending),
    )



def _validate_same_period_and_channel(
    entries: Iterable[RevenueFactEntry | RevenueManualOverrideEntry],
    *,
    month: str,
    youtube_channel_id: str,
) -> None:
    """Ensure revenue summary inputs share the requested month and channel."""
    for entry in entries:
        if entry.month != month or entry.youtube_channel_id != youtube_channel_id:
            raise ValueError(
                "Cannot aggregate revenue summary with inconsistent month/channel"
            )
