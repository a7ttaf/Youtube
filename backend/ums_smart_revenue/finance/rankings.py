from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.net_revenue import (
    ChannelNetRevenueSummary,
    MonthNetRevenueSummary,
)

# Supported ranking metrics mapped to the RankedEntry attribute the ordering
# value is read from. Default is "gross" when the caller omits a metric.
_METRIC_ATTRIBUTES: dict[str, str] = {
    "gross": "gross_revenue_usd",
    "net": "net_revenue_usd",
    "deduction": "deduction_amount_usd",
}
DEFAULT_RANKING_METRIC = "gross"
DEFAULT_RANKING_LIMIT = 10
MAX_RANKING_LIMIT = 100

# None metrics sort below every real value; a Decimal sink keeps the sort total.
_NONE_METRIC_SINK = Decimal("-Infinity")


class RankingsValidationError(ValueError):
    """Raised for an unsupported ranking metric or an out-of-range limit."""


@dataclass(frozen=True)
class RankedEntry:
    """One ranked entity (channel, company, or sector) for a month ranking."""

    rank: int
    entity_id: str
    entity_name: str
    gross_revenue_usd: Decimal
    net_revenue_usd: Decimal | None
    deduction_amount_usd: Decimal | None

    def to_api(self) -> dict[str, object]:
        """Serialize the ranked entry, money as canonical strings, None preserved."""
        return {
            "rank": self.rank,
            "entity_id": self.entity_id,
            "entity_name": self.entity_name,
            "gross_revenue_usd": _decimal_to_api(self.gross_revenue_usd),
            "net_revenue_usd": _decimal_to_api(self.net_revenue_usd),
            "deduction_amount_usd": _decimal_to_api(self.deduction_amount_usd),
        }


@dataclass(frozen=True)
class MonthRankingsSummary:
    """Top-N rankings of channels, companies, and sectors for one month."""

    month: str
    metric: str
    channels: list[RankedEntry]
    companies: list[RankedEntry]
    sectors: list[RankedEntry]

    def to_api(self) -> dict[str, object]:
        """Serialize the rankings summary to the fixed API response shape."""
        return {
            "month": self.month,
            "metric": self.metric,
            "channels": [entry.to_api() for entry in self.channels],
            "companies": [entry.to_api() for entry in self.companies],
            "sectors": [entry.to_api() for entry in self.sectors],
        }


@dataclass
class _Aggregate:
    """Mutable accumulator for one entity's rolled-up metric contributions."""

    entity_id: str
    gross_revenue_usd: Decimal = Decimal("0")
    net_revenue_usd: Decimal | None = None
    deduction_amount_usd: Decimal | None = None

    def add(self, channel: ChannelNetRevenueSummary) -> None:
        """Add one channel's metrics; None members never poison a non-None sum."""
        self.gross_revenue_usd += channel.adjusted_gross_revenue_usd
        self.net_revenue_usd = _add_optional(
            self.net_revenue_usd, channel.net_revenue_usd
        )
        self.deduction_amount_usd = _add_optional(
            self.deduction_amount_usd, channel.deduction_amount_usd
        )


def _add_optional(current: Decimal | None, addend: Decimal | None) -> Decimal | None:
    """Sum non-None contributions; stay None only until a real value appears."""
    if addend is None:
        return current
    if current is None:
        return addend
    return current + addend


# ============================================================================
# Purpose: Roll the per-channel net-revenue summary up to company and sector and
#   rank every dimension by a chosen metric, producing the month rankings view.
# Database/ORM: None (pure). Consumes an already-built MonthNetRevenueSummary.
# Standards: Pure finance service; typed RankingsValidationError on bad input;
#   money preserved as Decimal/None (never coalesced to 0); decimal_to_api at
#   the serialization boundary only.
# Blast Radius: Finance read/compute surface only. No persistence, no auth, no
#   audit, no Neo4j. Scope filtering happens upstream (the route restricts the
#   channel set before the summary is built).
# Connections:
#   - File: backend/ums_smart_revenue/finance/net_revenue.py ->
#       build_month_net_revenue_summary supplies the channel rows ranked here.
#   - File: backend/ums_smart_revenue/api/revenue.py -> GET
#       /months/{month}/rankings calls this after the finance gates + scope read.
# ============================================================================
def build_month_rankings(
    *,
    summary: MonthNetRevenueSummary,
    channel_company: Mapping[str, str],
    channel_sector: Mapping[str, str],
    company_names: Mapping[str, str],
    sector_names: Mapping[str, str],
    metric: str,
    limit: int,
) -> MonthRankingsSummary:
    """Rank channels, companies, and sectors for the month by the chosen metric."""
    metric_attribute = _METRIC_ATTRIBUTES.get(metric)
    if metric_attribute is None:
        raise RankingsValidationError(
            f"metric must be one of {sorted(_METRIC_ATTRIBUTES)}; got {metric!r}"
        )
    if limit < 1 or limit > MAX_RANKING_LIMIT:
        raise RankingsValidationError(
            f"limit must be between 1 and {MAX_RANKING_LIMIT}; got {limit}"
        )

    channel_entries = [
        RankedEntry(
            rank=0,
            entity_id=channel.youtube_channel_id,
            entity_name=channel.youtube_channel_id,
            gross_revenue_usd=channel.adjusted_gross_revenue_usd,
            net_revenue_usd=channel.net_revenue_usd,
            deduction_amount_usd=channel.deduction_amount_usd,
        )
        for channel in summary.channels
    ]
    company_entries = _roll_up(
        summary.channels, channel_company, company_names
    )
    sector_entries = _roll_up(
        summary.channels, channel_sector, sector_names
    )

    return MonthRankingsSummary(
        month=summary.month,
        metric=metric,
        channels=_rank(channel_entries, metric_attribute, limit),
        companies=_rank(company_entries, metric_attribute, limit),
        sectors=_rank(sector_entries, metric_attribute, limit),
    )


def _roll_up(
    channels: list[ChannelNetRevenueSummary],
    channel_group: Mapping[str, str],
    group_names: Mapping[str, str],
) -> list[RankedEntry]:
    """Aggregate channel metrics into per-group RankedEntry rows (rank unset)."""
    aggregates: dict[str, _Aggregate] = {}
    for channel in channels:
        group_id = channel_group.get(channel.youtube_channel_id)
        if group_id is None:
            continue
        aggregate = aggregates.get(group_id)
        if aggregate is None:
            aggregate = _Aggregate(entity_id=group_id)
            aggregates[group_id] = aggregate
        aggregate.add(channel)
    return [
        RankedEntry(
            rank=0,
            entity_id=aggregate.entity_id,
            entity_name=group_names.get(aggregate.entity_id, aggregate.entity_id),
            gross_revenue_usd=aggregate.gross_revenue_usd,
            net_revenue_usd=aggregate.net_revenue_usd,
            deduction_amount_usd=aggregate.deduction_amount_usd,
        )
        for aggregate in aggregates.values()
    ]


def _rank(
    entries: list[RankedEntry],
    metric_attribute: str,
    limit: int,
) -> list[RankedEntry]:
    """Sort entries desc by metric (None sinks), tie-break id asc, take top N."""
    # Stable two-pass sort: id asc first, then metric desc keeps id asc in ties.
    ordered = sorted(entries, key=lambda entry: entry.entity_id)
    ordered.sort(
        key=lambda entry: _metric_value(entry, metric_attribute),
        reverse=True,
    )
    ranked: list[RankedEntry] = []
    for position, entry in enumerate(ordered[:limit], start=1):
        ranked.append(
            RankedEntry(
                rank=position,
                entity_id=entry.entity_id,
                entity_name=entry.entity_name,
                gross_revenue_usd=entry.gross_revenue_usd,
                net_revenue_usd=entry.net_revenue_usd,
                deduction_amount_usd=entry.deduction_amount_usd,
            )
        )
    return ranked


def _metric_value(entry: RankedEntry, metric_attribute: str) -> Decimal:
    """Read the selected metric, sinking None to -Infinity for total ordering."""
    value = getattr(entry, metric_attribute)
    if value is None:
        return _NONE_METRIC_SINK
    return value
