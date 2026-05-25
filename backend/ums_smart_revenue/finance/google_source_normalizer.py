"""C1 normalizer: collapse google_revenue_source_rows to revenue_facts.

Reads tenant-scoped google_revenue_source_rows for one (tenant, month),
applies the per-source_system canonical-metric rule, USD-only filter,
and writes one MonthlyChannelRevenueFactORM entry per eligible
(youtube_channel_id, source_system) group via
SqlAlchemyRevenueFactRepository.record_fact().

See: Docs/superpowers/specs/2026-05-25-spec-c1-google-source-normalizer-design.md
"""

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from uuid import UUID

from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    GoogleRevenueSourceRowEntry,
)
from ums_smart_revenue.connectors.google_source_rows.repository import (
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.finance.month_close import get_or_create_month_close_row
from ums_smart_revenue.finance.revenue_facts import (
    RevenueFactEntry,
    RevenueFactLockedMonthError,
    RevenueFactSourceKind,
    # C1 deliberately reuses the private _validate_month / _resolve_tenant_id
    # helpers from revenue_facts so the normalizer surfaces the SAME
    # RevenueFactValidationError (and the same human-readable message) that
    # record_fact() raises. See spec section 4 "Write path" and section 6.1.
    _resolve_tenant_id,
    _validate_month,
)

# Consumed by Step 1 (refused) and Steps 6-7 (start/complete) logging
# wired in Tasks 4 and 5.
logger = logging.getLogger(__name__)


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


class GoogleSourceNormalizer:
    """Bridge google_revenue_source_rows -> MonthlyChannelRevenueFactORM.

    Writes go exclusively through SqlAlchemyRevenueFactRepository.record_fact();
    no direct ORM writes. Locked-month, active-channel, tenant, and value
    validation are preserved by reusing that write path.
    """

    def __init__(
        self,
        session: Session,
        *,
        tenant_id: UUID | str | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def normalize_month(
        self,
        *,
        month: str,
        channel_ids: list[str] | None = None,
        actor_user_id: str,
    ) -> NormalizationResult:
        # Step 0 - Input normalization.
        _validate_month(month)
        normalized_channel_ids: set[str] | None = (
            set(channel_ids) if channel_ids is not None else None
        )

        logger.info(
            "normalize_month start tenant_id=%s month=%s channel_scope=%s actor_user_id=%s",
            self._tenant_id,
            month,
            (
                "all"
                if normalized_channel_ids is None
                else f"n_channels={len(normalized_channel_ids)}"
            ),
            actor_user_id,
        )

        # Step 1 - Upfront locked-month gate. Acquires the finance-month
        # advisory lock + SELECT ... FOR UPDATE on the close row; may create
        # an OPEN close row when none exists.
        close_row = get_or_create_month_close_row(
            self._session,
            month,
            tenant_id=self._tenant_id,
            for_update=True,
        )
        if close_row.status == "LOCKED":
            logger.info(
                "normalize_month refused tenant_id=%s month=%s reason=month_locked",
                self._tenant_id,
                month,
            )
            raise RevenueFactLockedMonthError(
                "Finance month is locked for revenue fact imports"
            )

        # Step 2 - Fetch source rows for this tenant + month.
        source_repo = SqlAlchemyGoogleRevenueSourceRowRepository(self._session)
        all_rows = source_repo.list(self._tenant_id, report_month=month)

        # Step 3 - Apply channel_ids scope filter.
        # When channel_ids is provided, out-of-scope rows (including null-
        # channel rows) are silently dropped, NOT classified as skips. The
        # caller restricted scope; "not requested" is not "broken".
        # `in_scope_rows` is the input for Step 4 (MISSING_CHANNEL_ID /
        # UNKNOWN_CHANNEL classification) wired in Task 6; the unused-local
        # warning is suppressed here until that consumer lands.
        if normalized_channel_ids is not None:
            in_scope_rows = [  # noqa: F841
                row for row in all_rows
                if row.youtube_channel_id in normalized_channel_ids
            ]
        else:
            in_scope_rows = all_rows  # noqa: F841

        # Subsequent steps wired in later tasks; emit the complete log + return.
        result = NormalizationResult(created=[], updated=[], unchanged=[], skipped=[])
        logger.info(
            "normalize_month complete tenant_id=%s month=%s "
            "created=%d updated=%d unchanged=%d skipped=%d",
            self._tenant_id,
            month,
            len(result.created),
            len(result.updated),
            len(result.unchanged),
            len(result.skipped),
        )
        return result
