"""C1 normalizer: collapse google_revenue_source_rows to revenue_facts.

Reads tenant-scoped google_revenue_source_rows for one (tenant, month),
applies the per-source_system canonical-metric rule, USD-only filter,
and writes one MonthlyChannelRevenueFactORM entry per eligible
(youtube_channel_id, source_system) group via
SqlAlchemyRevenueFactRepository.record_fact().

See: Docs/superpowers/specs/2026-05-25-spec-c1-google-source-normalizer-design.md
"""

import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
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
    RevenueFactValidationError,
    SqlAlchemyRevenueFactRepository,
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


def _payload_matches(
    existing: RevenueFactEntry,
    *,
    proposed_gross: Decimal,
    proposed_source_report_id: str | None,
) -> bool:
    """Compare the fields the normalizer writes against the existing fact.

    Excludes actor_user_id / imported_by / timestamps per spec Section 5
    Step 6(j) so a rerun by a different actor with identical payload stays
    UNCHANGED.
    """
    return (
        existing.gross_revenue_usd == proposed_gross
        and existing.source_report_id == proposed_source_report_id
        and existing.net_revenue_usd is None
        and existing.shorts_revenue_usd is None
        and existing.longform_revenue_usd is None
        and existing.subscription_revenue_usd is None
        and existing.views == 0
        and existing.watch_time_minutes == Decimal("0")
        and existing.confidence_score == Decimal("1.0")
    )


# ============================================================================
# Purpose: Select one canonical row from a homogeneous group using the
#          per-source_system metric-key preference rule.
# Database/ORM: None (pure function operating on in-memory dataclasses).
# Standards: Deterministic tie-breaking by source_row_key ascending.
# Blast Radius: Incorrect selection propagates to all CREATED/UPDATED facts.
# ============================================================================
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
    preference = CANONICAL_METRIC_RULE.get(rows[0].source_system)
    if preference is None:
        raise RevenueFactValidationError(
            f"Unsupported source_system for canonical selection: {rows[0].source_system!r}"
        )
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

    # ============================================================================
    # Purpose: Orchestrate the full normalization pipeline for one (tenant, month):
    #          fetch source rows, filter by scope/currency/canonical, and write
    #          one revenue fact per eligible (channel, source_system) bucket.
    # Database/ORM: MonthlyChannelRevenueFactORM via record_fact(); read-only on
    #               GoogleRevenueSourceRowORM, YouTubeChannelORM, FinanceMonthCloseORM.
    # Standards: Raises RevenueFactLockedMonthError if month is LOCKED; raises
    #            RevenueFactValidationError on unsupported source_system.
    # Blast Radius: Finance totals, audit trail, month-lock integrity.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/revenue_facts.py -> write path.
    #   - File: Docs/superpowers/specs/2026-05-25-spec-c1-google-source-normalizer-design.md
    # ============================================================================
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
            raise RevenueFactLockedMonthError("Finance month is locked for revenue fact imports")

        # Step 2 - Fetch source rows for this tenant + month.
        source_repo = SqlAlchemyGoogleRevenueSourceRowRepository(self._session)
        all_rows = source_repo.list(self._tenant_id, report_month=month)

        # Step 3 - Apply channel_ids scope filter.
        # When channel_ids is provided, out-of-scope rows (including null-
        # channel rows) are silently dropped, NOT classified as skips. The
        # caller restricted scope; "not requested" is not "broken".
        if normalized_channel_ids is not None:
            in_scope_rows = [
                row for row in all_rows if row.youtube_channel_id in normalized_channel_ids
            ]
        else:
            in_scope_rows = all_rows

        # Step 4 - Resolve active channels for this tenant in one batched query.
        in_scope_channel_ids = {
            row.youtube_channel_id for row in in_scope_rows if row.youtube_channel_id is not None
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
        facts_repo = SqlAlchemyRevenueFactRepository(
            self._session,
            tenant_id=self._tenant_id,
        )
        facts_by_channel: dict[str, list[RevenueFactEntry]] = {}

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
                        source_row_id=r.id,
                        reason=SkipReason.NO_CANONICAL_ROW,
                    )
                    for r in usd_rows
                )
                continue

            # Step 6(g) - non-canonical USD siblings.
            skipped.extend(
                SkippedSourceRow(
                    source_row_id=r.id,
                    reason=SkipReason.NON_CANONICAL_METRIC,
                )
                for r in non_canonical_rest
            )
            # Step 6(h) - build proposed payload from canonical row + defaults.
            mapped_source_kind = SOURCE_SYSTEM_TO_SOURCE_KIND.get(source_system)
            if mapped_source_kind is None:
                raise RevenueFactValidationError(
                    f"Unsupported source_system for source kind mapping: {source_system!r}"
                )

            # Step 6(i) - read existing fact for (tenant, month, channel, source_kind).
            # Cache facts per channel to avoid N+1 queries when the same
            # channel_id appears across multiple source_system buckets.
            if channel_id not in facts_by_channel:
                facts_by_channel[channel_id] = facts_repo.list_channel_month_facts(
                    month=month,
                    youtube_channel_id=channel_id,
                )
            existing_facts = facts_by_channel[channel_id]
            existing = next(
                (fact for fact in existing_facts if fact.source_kind == mapped_source_kind.value),
                None,
            )

            # ============================================================================
            # Purpose: Classify the canonical row as CREATED, UPDATED, or UNCHANGED
            #          and write to MonthlyChannelRevenueFactORM via record_fact().
            #          Byte-identical payloads (even from different actors) yield UNCHANGED.
            # Database/ORM: Writes via SqlAlchemyRevenueFactRepository.record_fact().
            # Standards: Fails closed on locked months. Actor-insensitive for UNCHANGED.
            # Blast Radius: Revenue totals, payment matching, audit trail.
            # ============================================================================
            # Step 6(j) - classify via payload-only comparison.
            if existing is None:
                # CREATED path.
                written = facts_repo.record_fact(
                    month=month,
                    youtube_channel_id=channel_id,
                    source_kind=mapped_source_kind.value,
                    source_report_id=canonical.source_report_id,
                    gross_revenue_usd=canonical.amount_native,
                    net_revenue_usd=None,
                    shorts_revenue_usd=None,
                    longform_revenue_usd=None,
                    subscription_revenue_usd=None,
                    views=0,
                    watch_time_minutes=Decimal("0"),
                    confidence_score=Decimal("1.0"),
                    actor_user_id=actor_user_id,
                )
                created.append(written)
                continue
            # Existing fact present: compare and classify.
            if _payload_matches(
                existing,
                proposed_gross=canonical.amount_native,
                proposed_source_report_id=canonical.source_report_id,
            ):
                unchanged.append(existing)
                continue
            written = facts_repo.record_fact(
                month=month,
                youtube_channel_id=channel_id,
                source_kind=mapped_source_kind.value,
                source_report_id=canonical.source_report_id,
                gross_revenue_usd=canonical.amount_native,
                net_revenue_usd=None,
                shorts_revenue_usd=None,
                longform_revenue_usd=None,
                subscription_revenue_usd=None,
                views=0,
                watch_time_minutes=Decimal("0"),
                confidence_score=Decimal("1.0"),
                actor_user_id=actor_user_id,
            )
            updated.append(written)

        result = NormalizationResult(
            created=created,
            updated=updated,
            unchanged=unchanged,
            skipped=skipped,
        )
        # Aggregate skip-reason distribution (counts only, no source_row_id
        # values) per spec Section 6.5 "Observability". Empty dict when
        # nothing was skipped — kept in the line so log-parsers see a
        # stable structure regardless of outcome.
        reason_counts = Counter(s.reason.value for s in result.skipped)
        logger.info(
            "normalize_month complete tenant_id=%s month=%s "
            "created=%d updated=%d unchanged=%d skipped=%d "
            "skipped_by_reason=%s",
            self._tenant_id,
            month,
            len(result.created),
            len(result.updated),
            len(result.unchanged),
            len(result.skipped),
            dict(reason_counts),
        )
        return result
