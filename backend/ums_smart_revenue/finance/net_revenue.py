from collections import defaultdict
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import overload

from ums_smart_revenue.finance.allocation import AllocationLine, UnallocatedIssue
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.deduction_components import DeductionComponent

# Re-exported from the neutral deduction_policy module so existing
# `from ums_smart_revenue.finance.net_revenue import NET_APPLICABLE_COMPONENT_KINDS`
# (and SOURCE_SYSTEM_TO_SOURCE_KIND) call sites keep working unchanged, while
# finance.allocation imports them from deduction_policy to avoid an import cycle.
from ums_smart_revenue.finance.deduction_policy import (  # noqa: F401  (re-export)
    NET_APPLICABLE_COMPONENT_KINDS,
    SOURCE_SYSTEM_TO_SOURCE_KIND,
)
from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.reconciliation import SOURCE_PRIORITY
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry


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
    channel_direct_deduction_amount_usd: Decimal | None
    account_allocated_deduction_amount_usd: Decimal | None
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
            "channel_direct_deduction_amount_usd": _decimal_to_api(
                self.channel_direct_deduction_amount_usd
            ),
            "account_allocated_deduction_amount_usd": _decimal_to_api(
                self.account_allocated_deduction_amount_usd
            ),
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
    unallocated_account_deduction_total_usd: Decimal | None
    unallocated_account_issues: list[dict[str, str]] | None
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
            "unallocated_account_deduction_total_usd": _decimal_to_api(
                self.unallocated_account_deduction_total_usd
            ),
            "unallocated_account_issues": self.unallocated_account_issues,
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


def _applicable_account_allocations(
    allocations: Iterable[AllocationLine],
    *,
    youtube_channel_id: str,
    primary_source_kind: str,
) -> list[AllocationLine]:
    """Return source-aligned net-applicable account allocations for a channel.

    Same source-alignment rule as _applicable_deduction_components; basis_source_kind
    is provenance only and is deliberately NOT used as a second alignment contract.
    """
    return [
        line
        for line in allocations
        if line.youtube_channel_id == youtube_channel_id
        and line.net_applicable
        and SOURCE_SYSTEM_TO_SOURCE_KIND.get(line.source_system) == primary_source_kind
    ]


def _component_derived_channel_summary(
    *,
    primary: RevenueFactEntry,
    month: str,
    youtube_channel_id: str,
    approved_total: Decimal,
    adjusted_gross: Decimal,
    channel_direct_total: Decimal,
    account_allocated_total: Decimal,
    approved_count: int,
    pending_count: int,
) -> ChannelNetRevenueSummary:
    """Build a channel summary whose missing net is derived from components."""
    component_total = channel_direct_total + account_allocated_total
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
        channel_direct_deduction_amount_usd=channel_direct_total,
        account_allocated_deduction_amount_usd=account_allocated_total,
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
        channel_direct_deduction_amount_usd=None,
        account_allocated_deduction_amount_usd=None,
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
        channel_direct_deduction_amount_usd=None,
        account_allocated_deduction_amount_usd=None,
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
    account_allocations: Iterable[AllocationLine] = (),
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
        channel_direct = _applicable_deduction_components(
            deduction_components,
            month=resolved_month,
            youtube_channel_id=resolved_channel_id,
            primary_source_kind=primary.source_kind,
        )
        applied_keys = {component.component_key for component in channel_direct}
        account_allocated = [
            line
            for line in _applicable_account_allocations(
                account_allocations,
                youtube_channel_id=resolved_channel_id,
                primary_source_kind=primary.source_kind,
            )
            # Safety dedup (defensive; disjoint by construction): never apply an
            # allocated line whose component_key already applied as channel-direct.
            if line.component_key not in applied_keys
        ]
        if channel_direct or account_allocated:
            channel_direct_total = sum(
                (component.amount_usd for component in channel_direct),
                Decimal("0"),
            )
            account_allocated_total = sum(
                (line.allocated_amount_usd for line in account_allocated),
                Decimal("0"),
            )
            return _component_derived_channel_summary(
                primary=primary,
                month=resolved_month,
                youtube_channel_id=resolved_channel_id,
                approved_total=approved_total,
                adjusted_gross=adjusted_gross,
                channel_direct_total=channel_direct_total,
                account_allocated_total=account_allocated_total,
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


def _account_allocations_by_channel(
    account_allocations: Iterable[AllocationLine],
) -> dict[str, list[AllocationLine]]:
    """Group account-allocation lines by YouTube channel."""
    grouped: dict[str, list[AllocationLine]] = defaultdict(list)
    for line in account_allocations:
        grouped[line.youtube_channel_id].append(line)
    return grouped


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


# ============================================================================
# Purpose: Restrict month-wide account-allocation lines to an authorized channel
#   set so scoped net-revenue reads and finance exports never surface allocation
#   rows (and totals) for channels outside the caller's scope.
# Database/ORM: None (operates on already-loaded AllocationLine values).
# Standards: Pure, typed boundary; fail-closed on scoped reads (only explicitly
#   authorized channels survive); global reads (channel_ids is None) pass through.
# Blast Radius: Finance numbers + authorization — prevents cross-scope leakage of
#   account-allocated channels into scoped responses/exports.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py -> net-revenue route scope.
#   - File: backend/ums_smart_revenue/api/exports.py -> finance export scope.
# ============================================================================
def filter_account_allocations_to_scope(
    account_allocations: Iterable[AllocationLine],
    channel_ids: Collection[str] | None,
) -> list[AllocationLine]:
    """Restrict account-allocation lines to an authorized channel set.

    ``compute_month_account_allocation`` always resolves month-wide, so a scoped
    (company/sector/channel/group) read must drop lines for channels outside the
    authorized set before they reach the summary builder; otherwise a caller
    authorized for one scope could receive other channels' allocation-derived
    rows and totals. ``channel_ids is None`` marks a global read and keeps every
    line unchanged.
    """
    if channel_ids is None:
        return list(account_allocations)
    allowed = set(channel_ids)
    return [
        line for line in account_allocations if line.youtube_channel_id in allowed
    ]


def build_month_net_revenue_summary(
    *,
    month: str,
    facts: Iterable[RevenueFactEntry],
    manual_overrides: Iterable[RevenueManualOverrideEntry],
    deduction_components: Iterable[DeductionComponent] = (),
    account_allocations: Iterable[AllocationLine] = (),
    unallocated_account_issues: Iterable[UnallocatedIssue] | None = None,
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
    allocations_by_channel = _account_allocations_by_channel(account_allocations)

    # FIX: include allocations_by_channel so channels with allocated deductions
    # but no revenue facts or overrides are not silently dropped from the summary.
    channel_ids = sorted(
        set(facts_by_channel) | set(overrides_by_channel) | set(allocations_by_channel)
    )
    # FIX: use .get(channel_id, ()) for facts/overrides to match the sibling
    # component/allocation lookups below and the declared dict return type;
    # allocation-only channel_ids are absent from facts_by_channel/
    # overrides_by_channel, so direct subscripting relied on an undocumented
    # defaultdict side effect that a future plain-dict refactor would break.
    channels = [
        build_channel_net_revenue_summary(
            facts=facts_by_channel.get(channel_id, ()),
            manual_overrides=overrides_by_channel.get(channel_id, ()),
            month=month,
            youtube_channel_id=channel_id,
            deduction_components=components_by_channel.get(channel_id, ()),
            account_allocations=allocations_by_channel.get(channel_id, ()),
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
    if unallocated_account_issues is None:
        unallocated_total: Decimal | None = None
        unallocated_api: list[dict[str, str]] | None = None
    else:
        net_applicable_issues = [
            issue
            for issue in unallocated_account_issues
            if issue.component_kind in NET_APPLICABLE_COMPONENT_KINDS
        ]
        unallocated_total = sum(
            (issue.amount_usd for issue in net_applicable_issues),
            Decimal("0"),
        )
        unallocated_api = [
            {
                "scope_id": issue.scope_id,
                "component_kind": issue.component_kind,
                "component_key": issue.component_key,
                "amount_usd": _decimal_to_api(issue.amount_usd),
                "issue_code": issue.issue_code,
                "detail": issue.detail,
            }
            for issue in net_applicable_issues
        ]
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
        unallocated_account_deduction_total_usd=unallocated_total,
        unallocated_account_issues=unallocated_api,
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
        channel_direct_deduction_amount_usd=None,
        account_allocated_deduction_amount_usd=None,
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
