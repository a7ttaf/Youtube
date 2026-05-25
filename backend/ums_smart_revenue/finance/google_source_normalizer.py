"""C1 normalizer: collapse google_revenue_source_rows to revenue_facts.

Reads tenant-scoped google_revenue_source_rows for one (tenant, month),
applies the per-source_system canonical-metric rule, USD-only filter,
and writes one MonthlyChannelRevenueFactORM entry per eligible
(youtube_channel_id, source_system) group via
SqlAlchemyRevenueFactRepository.record_fact().

See: Docs/superpowers/specs/2026-05-25-spec-c1-google-source-normalizer-design.md
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    GoogleRevenueSourceRowEntry,
)
from ums_smart_revenue.finance.revenue_facts import (
    RevenueFactEntry,
    RevenueFactSourceKind,
)


class SkipReason(StrEnum):
    NON_USD_CURRENCY = "non_usd_currency"
    MISSING_CHANNEL_ID = "missing_channel_id"
    UNSUPPORTED_VALUE_KIND = "unsupported_value_kind"
    NON_CANONICAL_METRIC = "non_canonical_metric"
    UNKNOWN_CHANNEL = "unknown_channel"
    NO_CANONICAL_ROW = "no_canonical_row"


@dataclass(frozen=True)
class SkippedSourceRow:
    source_row_id: str
    reason: SkipReason


@dataclass(frozen=True)
class NormalizationResult:
    created: list[RevenueFactEntry]
    updated: list[RevenueFactEntry]
    unchanged: list[RevenueFactEntry]
    skipped: list[SkippedSourceRow]


SOURCE_SYSTEM_TO_SOURCE_KIND: Mapping[str, RevenueFactSourceKind] = MappingProxyType(
    {
        "youtube_reporting": RevenueFactSourceKind.YOUTUBE_CMS,
        "youtube_analytics": RevenueFactSourceKind.YOUTUBE_ANALYTICS,
        "adsense_management": RevenueFactSourceKind.ADSENSE,
    }
)


CANONICAL_METRIC_RULE: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "youtube_reporting": ("estimatedRevenue",),
        "youtube_analytics": ("estimatedRevenue",),
        "adsense_management": ("PAID_AMOUNT", "ESTIMATED_EARNINGS"),
    }
)


def select_canonical_row(
    rows: list[GoogleRevenueSourceRowEntry],
) -> tuple[GoogleRevenueSourceRowEntry | None, list[GoogleRevenueSourceRowEntry]]:
    """Apply the per-source_system canonical-metric rule to a homogeneous group.

    Currency-blind by design: the caller must pre-filter to USD before
    invoking this function. Tie-break across multiple rows with the same
    metric_key is deterministic by source_row_key ascending (multiple
    same-metric rows can arise from dimension breakdowns, distinct
    source_account_id, or parallel report shapes; repository ingested_at
    order is not a stable contract).

    Returns (canonical_or_None, non_canonical_rest).
    """
    if not rows:
        return None, []
    preference = CANONICAL_METRIC_RULE[rows[0].source_system]
    for metric_key in preference:
        candidates = sorted(
            (r for r in rows if r.metric_key == metric_key),
            key=lambda r: r.source_row_key,
        )
        if candidates:
            canonical = candidates[0]
            return canonical, [r for r in rows if r is not canonical]
    return None, list(rows)
