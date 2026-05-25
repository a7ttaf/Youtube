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

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.connectors.google_source_rows.dataclasses import (
    GoogleRevenueSourceRowEntry,
)
from ums_smart_revenue.connectors.google_source_rows.repository import (
    SqlAlchemyGoogleRevenueSourceRowRepository,
)
from ums_smart_revenue.db.org_models import YouTubeChannelORM
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


_UNSUPPORTED_VALUE_KINDS: frozenset[str] = frozenset({"tax", "deduction", "adjustment"})


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
        if normalized_channel_ids is not None:
            in_scope_rows = [
                row for row in all_rows
                if row.youtube_channel_id in normalized_channel_ids
            ]
        else:
            in_scope_rows = all_rows

        # Step 4 - Resolve active channels for this tenant in one batched query.
        in_scope_channel_ids = {
            row.youtube_channel_id
            for row in in_scope_rows
            if row.youtube_channel_id is not None
        }
        active_channel_ids: set[str] = set()
        if in_scope_channel_ids:
            active_channel_ids = set(
                self._session.scalars(
                    select(YouTubeChannelORM.youtube_channel_id).where(
                        YouTubeChannelORM.tenant_id == self._tenant_id,
                        YouTubeChannelORM.active.is_(True),
                        YouTubeChannelORM.youtube_channel_id.in_(in_scope_channel_ids),
                    )
                ).all()
            )

        # Step 5 - Bucket by (channel_id, source_system).
        buckets: dict[tuple[str | None, str], list[GoogleRevenueSourceRowEntry]] = {}
        for row in in_scope_rows:
            key = (row.youtube_channel_id, row.source_system)
            buckets.setdefault(key, []).append(row)

        created: list[RevenueFactEntry] = []
        updated: list[RevenueFactEntry] = []
        unchanged: list[RevenueFactEntry] = []
        skipped: list[SkippedSourceRow] = []

        # Step 6 - Per-bucket processing.
        for (channel_id, source_system), bucket_rows in buckets.items():
            if channel_id is None:
                # Step 6(a) - missing channel id.
                skipped.extend(
                    SkippedSourceRow(source_row_id=r.id, reason=SkipReason.MISSING_CHANNEL_ID)
                    for r in bucket_rows
                )
                continue
            if channel_id not in active_channel_ids:
                # Step 6(b) - unknown / inactive channel.
                skipped.extend(
                    SkippedSourceRow(source_row_id=r.id, reason=SkipReason.UNKNOWN_CHANNEL)
                    for r in bucket_rows
                )
                continue
            # Step 6(c) - drop tax/deduction/adjustment rows.
            unsupported_in_bucket = [
                r for r in bucket_rows if r.value_kind in _UNSUPPORTED_VALUE_KINDS
            ]
            for r in unsupported_in_bucket:
                skipped.append(
                    SkippedSourceRow(source_row_id=r.id, reason=SkipReason.UNSUPPORTED_VALUE_KIND)
                )
            remaining = [r for r in bucket_rows if r.value_kind not in _UNSUPPORTED_VALUE_KINDS]
            if not remaining:
                continue
            # Step 6(d) - USD-only filter; runs BEFORE canonical selection so
            # a non-USD row cannot win canonical and starve an eligible USD
            # sibling (spec Section 5 Step 6(d)).
            non_usd = [r for r in remaining if r.currency_code != "USD"]
            for r in non_usd:
                skipped.append(
                    SkippedSourceRow(source_row_id=r.id, reason=SkipReason.NON_USD_CURRENCY)
                )
            usd_rows = [r for r in remaining if r.currency_code == "USD"]
            if not usd_rows:
                continue
            # Step 6(e) - apply pure canonical-metric rule on USD-eligible rows.
            canonical, non_canonical_rest = select_canonical_row(usd_rows)

            if canonical is None:
                # Step 6(f) - USD candidates existed but none matched the
                # preferred metric_keys for this source_system.
                skipped.extend(
                    SkippedSourceRow(
                        source_row_id=r.id, reason=SkipReason.NO_CANONICAL_ROW,
                    )
                    for r in usd_rows
                )
                continue

            # Step 6(g) - non-canonical USD siblings.
            skipped.extend(
                SkippedSourceRow(
                    source_row_id=r.id, reason=SkipReason.NON_CANONICAL_METRIC,
                )
                for r in non_canonical_rest
            )
            # Subsequent step branches (6h-6j) wired in Task 10.

        result = NormalizationResult(
            created=created, updated=updated, unchanged=unchanged, skipped=skipped,
        )
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
