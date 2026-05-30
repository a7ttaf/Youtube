from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import overload

from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.deduction_components import DeductionComponent
from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.reconciliation import SOURCE_PRIORITY
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry

# ============================================================================
# Purpose: Map a Google source_system to the RevenueFactSourceKind it backs, so
#   a channel-scoped deduction component is only applied to a net derived from
#   the SAME source (no cross-source mixing). Used only on the missing-net path.
# Database/ORM: None.
# Standards: explicit, closed map; unknown source_system -> no match -> ignored.
# Blast Radius: Finance net-revenue derivation (missing-net path only).
# ============================================================================
SOURCE_SYSTEM_TO_SOURCE_KIND: dict[str, str] = {
    "adsense_management": "ADSENSE",
    "youtube_reporting": "YOUTUBE_CMS",
    "youtube_analytics": "YOUTUBE_ANALYTICS",
}
# Only blind, source-labeled reductions reduce a component-derived net; signed
# FX_VARIANCE / TRANSFER_FEE / UNRESOLVED_PAYMENT_GAP kinds never reduce net.
NET_APPLICABLE_COMPONENT_KINDS: frozenset[str] = frozenset({"TAX", "DEDUCTION"})


@dataclass(frozen=True)
class ChannelNetRevenueSummary:
    """Summary of net revenue for one YouTube channel and month."""

    month: str
    youtube_channel_id: str
    status: str
    primary_source_kind: str | None
    baseline_gross_revenue_usd: Decimal
    baseline_net_revenue_usd: Decimal | None
    approved_manual_override_total_usd: Decimal
    adjusted_gross_revenue_usd: Decimal
    net_revenue_usd: Decimal | None
    deduction_amount_usd: Decimal | None
    deduction_percentage: Decimal | None
    confidence: str
    approved_manual_override_count: int
    pending_manual_override_count: int
    issues: list[dict[str, str]]

    def to_api(self) -> dict[str, object]:
        """
        Convert the NetRevenue instance to a dictionary suitable for API consumption.

        Returns:
            dict[str, object]: A mapping of field names to their API-compatible values.
        """
        return {
            "month": self.month,
            "youtube_channel_id": self.youtube_channel_id,
            "status": self.status,
            "primary_source_kind": self.primary_source_kind,
            "baseline_gross_revenue_usd": _decimal_to_api(
                self.baseline_gross_revenue_usd
            ),
            "baseline_net_revenue_usd": _decimal_to_api(self.baseline_net_revenue_usd),
            "approved_manual_override_total_usd": _decimal_to_api(
                self.approved_manual_override_total_usd
            ),
            "adjusted_gross_revenue_usd": _decimal_to_api(
                self.adjusted_gross_revenue_usd
            ),
            "net_revenue_usd": _decimal_to_api(self.net_revenue_usd),
            "deduction_amount_usd": _decimal_to_api(self.deduction_amount_usd),
            "deduction_percentage": _decimal_to_api(self.deduction_percentage),
            "confidence": self.confidence,
            "approved_manual_override_count": self.approved_manual_override_count,
            "pending_manual_override_count": self.pending_manual_override_count,
            "issues": list(self.issues),
        }


@dataclass(frozen=True)
class MonthNetRevenueSummary:
    """Aggregated net revenue summary across channels for one month."""

    month: str
    status: str
    channel_count: int
    calculated_channel_count: int
    missing_net_source_count: int
    pending_manual_override_count: int
    total_adjusted_gross_revenue_usd: Decimal
    total_net_revenue_usd: Decimal
    total_deduction_amount_usd: Decimal
    channels: list[ChannelNetRevenueSummary]

    def to_api(self) -> dict[str, object]:
        """Serialize the channel net revenue summary to a dictionary for API usage."""
        return {
            "month": self.month,
            "status": self.status,
            "channel_count": self.channel_count,
            "calculated_channel_count": self.calculated_channel_count,
            "missing_net_source_count": self.missing_net_source_count,
            "pending_manual_override_count": self.pending_manual_override_count,
            "total_adjusted_gross_revenue_usd": _decimal_to_api(
                self.total_adjusted_gross_revenue_usd
            ),
            "total_net_revenue_usd": _decimal_to_api(self.total_net_revenue_usd),
            "total_deduction_amount_usd": _decimal_to_api(
                self.total_deduction_amount_usd
            ),
            "channels": [channel.to_api() for channel in self.channels],
        }


class NetRevenueValidationError(ValueError):
    """Exception raised for errors during net revenue validation, e.g., unsupported currency."""


def normalize_net_revenue_currency(currency: str) -> str:
    """Normalize the currency string to uppercase USD and validate that only USD is supported."""
    normalized = currency.strip().upper()
    if normalized != "USD":
        raise NetRevenueValidationError(
            "currency must be USD until exchange-rate support is implemented"
        )
    return normalized


def _resolved_period_and_channel(
    fact_list: list[RevenueFactEntry],
    *,
    month: str | None,
    youtube_channel_id: str | None,
) -> tuple[str, str]:
    """Resolve target month/channel from facts or required explicit arguments.

    Raises:
        NetRevenueValidationError: If no facts are provided and either month
            or youtube_channel_id is None.
    """
    if fact_list:
        return (
            month or fact_list[0].month,
            youtube_channel_id or fact_list[0].youtube_channel_id,
        )
    if month is None or youtube_channel_id is None:
        raise NetRevenueValidationError(
            "month and youtube_channel_id are required when no revenue facts exist"
        )
    return month, youtube_channel_id


def _manual_override_summary(
    override_list: list[RevenueManualOverrideEntry],
) -> tuple[list[RevenueManualOverrideEntry], list[RevenueManualOverrideEntry], Decimal]:
    """Split manual overrides by status and total approved adjustments."""
    approved = [override for override in override_list if override.status == "APPROVED"]
    pending = [override for override in override_list if override.status == "PENDING"]
    approved_total = sum(
        (override.adjustment_revenue_usd for override in approved),
        Decimal("0"),
    )
    return approved, pending, approved_total


def _applicable_deduction_components(
    components: Iterable[DeductionComponent],
    *,
    month: str,
    youtube_channel_id: str,
    primary_source_kind: str,
) -> list[DeductionComponent]:
    """Return source-aligned channel components that can reduce derived net."""
    return [
        component
        for component in components
        if component.month == month
        and component.scope_kind == "CHANNEL"
        and component.scope_id == youtube_channel_id
        and component.component_kind in NET_APPLICABLE_COMPONENT_KINDS
        and SOURCE_SYSTEM_TO_SOURCE_KIND.get(component.source_system)
        == primary_source_kind
    ]


def _component_derived_channel_summary(
    *,
    primary: RevenueFactEntry,
    month: str,
    youtube_channel_id: str,
    approved_total: Decimal,
    adjusted_gross: Decimal,
    component_total: Decimal,
    approved_count: int,
    pending_count: int,
) -> ChannelNetRevenueSummary:
    """Build a channel summary whose missing net is derived from components."""
    component_derived_net = adjusted_gross - component_total
    return ChannelNetRevenueSummary(
        month=month,
        youtube_channel_id=youtube_channel_id,
        status="COMPONENT_DERIVED",
        primary_source_kind=primary.source_kind,
        baseline_gross_revenue_usd=primary.gross_revenue_usd,
        baseline_net_revenue_usd=None,
        approved_manual_override_total_usd=approved_total,
        adjusted_gross_revenue_usd=adjusted_gross,
        net_revenue_usd=component_derived_net,
        deduction_amount_usd=component_total,
        deduction_percentage=_deduction_percentage(
            deduction_amount=component_total,
            gross_revenue_usd=adjusted_gross,
        ),
        confidence="D_ESTIMATED",
        approved_manual_override_count=approved_count,
        pending_manual_override_count=pending_count,
        issues=[],
    )


def _missing_net_source_summary(
    *,
    primary: RevenueFactEntry,
    month: str,
    youtube_channel_id: str,
    approved_total: Decimal,
    adjusted_gross: Decimal,
    approved_count: int,
    pending_count: int,
) -> ChannelNetRevenueSummary:
    """Build a channel summary for a primary source with no usable net value."""
    return ChannelNetRevenueSummary(
        month=month,
        youtube_channel_id=youtube_channel_id,
        status="NET_REVENUE_SOURCE_MISSING",
        primary_source_kind=primary.source_kind,
        baseline_gross_revenue_usd=primary.gross_revenue_usd,
        baseline_net_revenue_usd=None,
        approved_manual_override_total_usd=approved_total,
        adjusted_gross_revenue_usd=adjusted_gross,
        net_revenue_usd=None,
        deduction_amount_usd=None,
        deduction_percentage=None,
        confidence="E_MISSING",
        approved_manual_override_count=approved_count,
        pending_manual_override_count=pending_count,
        issues=[
            {
                "issue_type": "NET_REVENUE_SOURCE_MISSING",
                "severity": "HIGH",
                "message": (
                    f"Primary revenue source {primary.source_kind} has no "
                    f"net revenue for {youtube_channel_id} in {month}."
                ),
            }
        ],
    )


def _calculated_channel_summary(
    *,
    primary: RevenueFactEntry,
    month: str,
    youtube_channel_id: str,
    approved_total: Decimal,
    adjusted_gross: Decimal,
    approved_count: int,
    pending_count: int,
) -> ChannelNetRevenueSummary:
    """Build a channel summary when the primary source already has net revenue."""
    adjusted_net = primary.net_revenue_usd + approved_total
    deduction_amount = adjusted_gross - adjusted_net
    return ChannelNetRevenueSummary(
        month=month,
        youtube_channel_id=youtube_channel_id,
        status="PENDING_OVERRIDE_REVIEW" if pending_count else "CALCULATED",
        primary_source_kind=primary.source_kind,
        baseline_gross_revenue_usd=primary.gross_revenue_usd,
        baseline_net_revenue_usd=primary.net_revenue_usd,
        approved_manual_override_total_usd=approved_total,
        adjusted_gross_revenue_usd=adjusted_gross,
        net_revenue_usd=adjusted_net,
        deduction_amount_usd=deduction_amount,
        deduction_percentage=_deduction_percentage(
            deduction_amount=deduction_amount,
            gross_revenue_usd=adjusted_gross,
        ),
        confidence="D_ESTIMATED" if pending_count else "B_RECONCILED",
        approved_manual_override_count=approved_count,
        pending_manual_override_count=pending_count,
        issues=[],
    )


def build_channel_net_revenue_summary(
    *,
    facts: Iterable[RevenueFactEntry],
    manual_overrides: Iterable[RevenueManualOverrideEntry],
    month: str | None = None,
    youtube_channel_id: str | None = None,
    deduction_components: Iterable[DeductionComponent] = (),
) -> ChannelNetRevenueSummary:
    """Construct a ChannelNetRevenueSummary from provided revenue facts and manual overrides,
    resolving the target month and channel ID.

    Raises:
        NetRevenueValidationError: If no facts are provided and month or
            youtube_channel_id is None, or if facts/overrides span multiple
            months or channels.
    """
    fact_list = sorted(
        facts,
        key=lambda fact: (SOURCE_PRIORITY.get(fact.source_kind, 99), fact.source_kind),
    )
    override_list = list(manual_overrides)
    resolved_month, resolved_channel_id = _resolved_period_and_channel(
        fact_list,
        month=month,
        youtube_channel_id=youtube_channel_id,
    )

    _validate_same_period_and_channel(
        fact_list,
        month=resolved_month,
        youtube_channel_id=resolved_channel_id,
    )
    _validate_same_period_and_channel(
        override_list,
        month=resolved_month,
        youtube_channel_id=resolved_channel_id,
    )

    approved, pending, approved_total = _manual_override_summary(override_list)
    if not fact_list:
        return _empty_channel_summary(
            month=resolved_month,
            youtube_channel_id=resolved_channel_id,
            approved_total=approved_total,
            approved_count=len(approved),
            pending_count=len(pending),
        )

    primary = fact_list[0]
    adjusted_gross = primary.gross_revenue_usd + approved_total
    # ============================================================================
    # Purpose: When the primary source has no net, derive a channel net from
    #   same-month, same-channel, source-aligned TAX/DEDUCTION components only.
    #   The net-present path below is left unchanged (anti-double-count).
    # Database/ORM: None (pure over already-read facts + deduction components).
    # Standards: unknown source_system or non-CHANNEL/non-TAX/DEDUCTION scope is
    #   ignored; signed FX/fee/gap kinds never reduce net.
    # Blast Radius: Finance net-revenue derivation, missing-net branch only.
    # ============================================================================
    if primary.net_revenue_usd is None:
        applicable = _applicable_deduction_components(
            deduction_components,
            month=resolved_month,
            youtube_channel_id=resolved_channel_id,
            primary_source_kind=primary.source_kind,
        )
        if applicable:
            component_total = sum(
                (component.amount_usd for component in applicable),
                Decimal("0"),
            )
            return _component_derived_channel_summary(
                primary=primary,
                month=resolved_month,
                youtube_channel_id=resolved_channel_id,
                approved_total=approved_total,
                adjusted_gross=adjusted_gross,
                component_total=component_total,
                approved_count=len(approved),
                pending_count=len(pending),
            )
        return _missing_net_source_summary(
            primary=primary,
            month=resolved_month,
            youtube_channel_id=resolved_channel_id,
            approved_total=approved_total,
            adjusted_gross=adjusted_gross,
            approved_count=len(approved),
            pending_count=len(pending),
        )

    return _calculated_channel_summary(
        primary=primary,
        month=resolved_month,
        youtube_channel_id=resolved_channel_id,
        approved_total=approved_total,
        adjusted_gross=adjusted_gross,
        approved_count=len(approved),
        pending_count=len(pending),
    )


@overload
def _group_by_channel(
    items: Iterable[RevenueFactEntry],
    *,
    month: str,
) -> dict[str, list[RevenueFactEntry]]: ...


@overload
def _group_by_channel(
    items: Iterable[RevenueManualOverrideEntry],
    *,
    month: str,
) -> dict[str, list[RevenueManualOverrideEntry]]: ...


def _group_by_channel(
    items: Iterable[RevenueFactEntry] | Iterable[RevenueManualOverrideEntry],
    *,
    month: str,
) -> dict[str, list[RevenueFactEntry]] | dict[str, list[RevenueManualOverrideEntry]]:
    """Group entries for the requested month by YouTube channel ID."""
    grouped = defaultdict(list)
    for item in items:
        if item.month == month:
            grouped[item.youtube_channel_id].append(item)
    return grouped


def _facts_by_channel(
    facts: Iterable[RevenueFactEntry],
    *,
    month: str,
) -> dict[str, list[RevenueFactEntry]]:
    """Group revenue facts for the requested month by channel."""
    return _group_by_channel(facts, month=month)


def _overrides_by_channel(
    manual_overrides: Iterable[RevenueManualOverrideEntry],
    *,
    month: str,
) -> dict[str, list[RevenueManualOverrideEntry]]:
    """Group manual overrides for the requested month by channel."""
    return _group_by_channel(manual_overrides, month=month)


def _deduction_components_by_channel(
    deduction_components: Iterable[DeductionComponent],
    *,
    month: str,
) -> dict[str, list[DeductionComponent]]:
    """Group channel-scoped deduction components for the requested month."""
    components_by_channel: dict[str, list[DeductionComponent]] = defaultdict(list)
    for component in deduction_components:
        if component.month == month and component.scope_kind == "CHANNEL":
            components_by_channel[component.scope_id].append(component)
    return components_by_channel


def _month_net_revenue_counts(
    channels: list[ChannelNetRevenueSummary],
) -> tuple[list[ChannelNetRevenueSummary], int, int]:
    """Return calculated channels, missing-net count, and pending override count."""
    calculated = [
        channel for channel in channels if channel.net_revenue_usd is not None
    ]
    missing_count = sum(
        1 for channel in channels if channel.status == "NET_REVENUE_SOURCE_MISSING"
    )
    pending_count = sum(channel.pending_manual_override_count for channel in channels)
    return calculated, missing_count, pending_count


def build_month_net_revenue_summary(
    *,
    month: str,
    facts: Iterable[RevenueFactEntry],
    manual_overrides: Iterable[RevenueManualOverrideEntry],
    deduction_components: Iterable[DeductionComponent] = (),
) -> MonthNetRevenueSummary:
    """
    Build a summary of net revenue for a given month across all channels.

    Aggregates revenue facts and manual overrides for the specified month,
    computes per-channel net revenue summaries, and returns a consolidated
    MonthNetRevenueSummary.
    """
    facts_by_channel = _facts_by_channel(facts, month=month)
    overrides_by_channel = _overrides_by_channel(manual_overrides, month=month)
    components_by_channel = _deduction_components_by_channel(
        deduction_components,
        month=month,
    )

    channel_ids = sorted(set(facts_by_channel) | set(overrides_by_channel))
    channels = [
        build_channel_net_revenue_summary(
            facts=facts_by_channel[channel_id],
            manual_overrides=overrides_by_channel[channel_id],
            month=month,
            youtube_channel_id=channel_id,
            deduction_components=components_by_channel.get(channel_id, ()),
        )
        for channel_id in channel_ids
    ]
    calculated, missing_count, pending_count = _month_net_revenue_counts(channels)
    status = _month_status(
        channel_count=len(channels),
        calculated_count=len(calculated),
        missing_count=missing_count,
        pending_count=pending_count,
    )
    return MonthNetRevenueSummary(
        month=month,
        status=status,
        channel_count=len(channels),
        calculated_channel_count=len(calculated),
        missing_net_source_count=missing_count,
        pending_manual_override_count=pending_count,
        total_adjusted_gross_revenue_usd=sum(
            (channel.adjusted_gross_revenue_usd for channel in channels),
            Decimal("0"),
        ),
        total_net_revenue_usd=sum(
            (channel.net_revenue_usd for channel in calculated),
            Decimal("0"),
        ),
        total_deduction_amount_usd=sum(
            (channel.deduction_amount_usd for channel in calculated),
            Decimal("0"),
        ),
        channels=channels,
    )


def _empty_channel_summary(
    *,
    month: str,
    youtube_channel_id: str,
    approved_total: Decimal,
    approved_count: int,
    pending_count: int,
) -> ChannelNetRevenueSummary:
    """
    Create an empty channel summary with NO_FACTS status and default revenue values
    for a specified month and YouTube channel.
    """
    return ChannelNetRevenueSummary(
        month=month,
        youtube_channel_id=youtube_channel_id,
        status="NO_FACTS",
        primary_source_kind=None,
        baseline_gross_revenue_usd=Decimal("0"),
        baseline_net_revenue_usd=None,
        approved_manual_override_total_usd=approved_total,
        adjusted_gross_revenue_usd=approved_total,
        net_revenue_usd=None,
        deduction_amount_usd=None,
        deduction_percentage=None,
        confidence="E_MISSING",
        approved_manual_override_count=approved_count,
        pending_manual_override_count=pending_count,
        issues=[
            {
                "issue_type": "NO_REVENUE_FACTS",
                "severity": "HIGH",
                "message": (
                    f"No revenue facts exist for {youtube_channel_id} in {month}."
                ),
            }
        ],
    )


def _month_status(
    *,
    channel_count: int,
    calculated_count: int,
    missing_count: int,
    pending_count: int,
) -> str:
    """
    Determine the overall status of a month based on channel counts,
    missing data, and pending manual overrides.
    """
    if channel_count == 0:
        return "NO_FACTS"
    if missing_count:
        return "PARTIAL"
    if pending_count:
        return "PENDING_OVERRIDE_REVIEW"
    if calculated_count == channel_count:
        return "CALCULATED"
    return "PARTIAL"


def _validate_same_period_and_channel(
    entries: Iterable[RevenueFactEntry | RevenueManualOverrideEntry],
    *,
    month: str,
    youtube_channel_id: str,
) -> None:
    """
    Validate that all entries share the same month and YouTube channel,
    raising an error if any inconsistency is found.
    """
    for entry in entries:
        if entry.month != month or entry.youtube_channel_id != youtube_channel_id:
            raise NetRevenueValidationError(
                "Cannot calculate net revenue with inconsistent month/channel"
            )


def _deduction_percentage(
    *,
    deduction_amount: Decimal,
    gross_revenue_usd: Decimal,
) -> Decimal:
    """
    Calculate the deduction percentage relative to gross revenue,
    formatted to four decimal places with HALF_UP rounding.
    """
    if gross_revenue_usd == 0:
        return Decimal("0.0000")
    return ((deduction_amount / gross_revenue_usd) * Decimal("100")).quantize(
        Decimal("0.0001"),
        rounding=ROUND_HALF_UP,
    )
