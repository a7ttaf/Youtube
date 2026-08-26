"""C1 normalizer: collapse google_revenue_source_rows to revenue_facts.

Reads tenant-scoped google_revenue_source_rows for one (tenant, month),
applies the per-source_system canonical-metric rule, USD-only filter,
and writes one MonthlyChannelRevenueFactORM entry per eligible
(youtube_channel_id, source_system) group via
SqlAlchemyRevenueFactRepository.record_fact().

See: Docs/superpowers/specs/2026-05-25-spec-c1-google-source-normalizer-design.md
"""

import hashlib
import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
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


@dataclass
class _NormalizationWork:
    """Mutable result accumulator for one normalization pass."""

    facts_repo: SqlAlchemyRevenueFactRepository
    created: list[RevenueFactEntry] = field(default_factory=list)
    updated: list[RevenueFactEntry] = field(default_factory=list)
    unchanged: list[RevenueFactEntry] = field(default_factory=list)
    skipped: list[SkippedSourceRow] = field(default_factory=list)
    facts_by_channel: dict[str, list[RevenueFactEntry]] = field(default_factory=dict)


def _channel_scope_label(channel_ids: set[str] | None) -> str:
    """Return the stable scope label used by normalizer logging."""
    return "all" if channel_ids is None else f"n_channels={len(channel_ids)}"


def _actor_log_label(actor_user_id: str) -> str:
    """Return a non-reversible fingerprint of the acting user's UUID."""
    return hashlib.sha256(actor_user_id.encode("utf-8")).hexdigest()[:12]


def _scoped_source_rows(
    rows: list[GoogleRevenueSourceRowEntry],
    channel_ids: set[str] | None,
) -> list[GoogleRevenueSourceRowEntry]:
    """Drop out-of-scope rows without counting them as skipped records."""
    if channel_ids is None:
        return rows
    return [row for row in rows if row.youtube_channel_id in channel_ids]


def _active_channel_ids(
    session: Session,
    *,
    tenant_id: UUID,
    rows: list[GoogleRevenueSourceRowEntry],
) -> set[str]:
    """Load active channel IDs referenced by scoped source rows in one query."""
    in_scope_channel_ids = {row.youtube_channel_id for row in rows if row.youtube_channel_id}
    if not in_scope_channel_ids:
        return set()
    return set(
        session.scalars(
            select(YouTubeChannelORM.youtube_channel_id).where(
                YouTubeChannelORM.tenant_id == tenant_id,
                YouTubeChannelORM.active.is_(True),
                YouTubeChannelORM.youtube_channel_id.in_(in_scope_channel_ids),
            )
        ).all()
    )


def _source_row_buckets(
    rows: list[GoogleRevenueSourceRowEntry],
) -> dict[tuple[str | None, str], list[GoogleRevenueSourceRowEntry]]:
    """Bucket source rows by normalized fact identity: channel and source system."""
    buckets: dict[tuple[str | None, str], list[GoogleRevenueSourceRowEntry]] = {}
    for row in rows:
        key = (row.youtube_channel_id, row.source_system)
        buckets.setdefault(key, []).append(row)
    return buckets


def _append_skipped(
    skipped: list[SkippedSourceRow],
    rows: list[GoogleRevenueSourceRowEntry],
    reason: SkipReason,
) -> None:
    """Record one skip reason for each source row in a bucket subset."""
    skipped.extend(SkippedSourceRow(source_row_id=row.id, reason=reason) for row in rows)


def _bucket_rejection_reason(
    channel_id: str | None,
    active_channel_ids: set[str],
) -> SkipReason | None:
    """Return the channel-level skip reason for a bucket, if any."""
    if channel_id is None:
        return SkipReason.MISSING_CHANNEL_ID
    if channel_id not in active_channel_ids:
        return SkipReason.UNKNOWN_CHANNEL
    return None


def _eligible_usd_rows(
    bucket_rows: list[GoogleRevenueSourceRowEntry],
    skipped: list[SkippedSourceRow],
) -> list[GoogleRevenueSourceRowEntry]:
    """Filter unsupported value kinds and non-USD rows, recording exact skips."""
    usd_rows: list[GoogleRevenueSourceRowEntry] = []
    for row in bucket_rows:
        if row.value_kind in _UNSUPPORTED_VALUE_KINDS:
            skipped.append(
                SkippedSourceRow(source_row_id=row.id, reason=SkipReason.UNSUPPORTED_VALUE_KIND)
            )
            continue
        if row.currency_code != "USD":
            skipped.append(
                SkippedSourceRow(source_row_id=row.id, reason=SkipReason.NON_USD_CURRENCY)
            )
            continue
        usd_rows.append(row)
    return usd_rows


def _canonical_source_row(
    usd_rows: list[GoogleRevenueSourceRowEntry],
    skipped: list[SkippedSourceRow],
) -> GoogleRevenueSourceRowEntry | None:
    """Select the canonical USD row and record non-canonical USD siblings."""
    canonical, non_canonical_rest = select_canonical_row(usd_rows)
    if canonical is None:
        _append_skipped(skipped, usd_rows, SkipReason.NO_CANONICAL_ROW)
        return None
    _append_skipped(skipped, non_canonical_rest, SkipReason.NON_CANONICAL_METRIC)
    return canonical


def _source_kind_for_system(source_system: str) -> RevenueFactSourceKind:
    """Map source-system keys to the revenue fact source kind enum."""
    mapped_source_kind = SOURCE_SYSTEM_TO_SOURCE_KIND.get(source_system)
    if mapped_source_kind is None:
        raise RevenueFactValidationError(
            f"Unsupported source_system for source kind mapping: {source_system!r}"
        )
    return mapped_source_kind


def _existing_fact_for_bucket(
    work: _NormalizationWork,
    *,
    month: str,
    channel_id: str,
    source_kind: RevenueFactSourceKind,
) -> RevenueFactEntry | None:
    """Read and cache existing revenue facts for a channel-month bucket."""
    if channel_id not in work.facts_by_channel:
        work.facts_by_channel[channel_id] = work.facts_repo.list_channel_month_facts(
            month=month,
            youtube_channel_id=channel_id,
        )
    existing_facts = work.facts_by_channel[channel_id]
    return next(
        (fact for fact in existing_facts if fact.source_kind == source_kind.value),
        None,
    )


def _record_canonical_fact(
    work: _NormalizationWork,
    *,
    month: str,
    channel_id: str,
    source_kind: RevenueFactSourceKind,
    canonical: GoogleRevenueSourceRowEntry,
    actor_user_id: str,
) -> RevenueFactEntry:
    """Write the normalized canonical row through the finance repository."""
    return work.facts_repo.record_fact(
        month=month,
        youtube_channel_id=channel_id,
        source_kind=source_kind.value,
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


def _classify_canonical_fact(
    work: _NormalizationWork,
    *,
    month: str,
    channel_id: str,
    source_kind: RevenueFactSourceKind,
    canonical: GoogleRevenueSourceRowEntry,
    actor_user_id: str,
) -> None:
    """Classify the canonical row as created, updated, or unchanged."""
    existing = _existing_fact_for_bucket(
        work,
        month=month,
        channel_id=channel_id,
        source_kind=source_kind,
    )
    if existing is None:
        work.created.append(
            _record_canonical_fact(
                work,
                month=month,
                channel_id=channel_id,
                source_kind=source_kind,
                canonical=canonical,
                actor_user_id=actor_user_id,
            )
        )
        return
    if _payload_matches(
        existing,
        proposed_gross=canonical.amount_native,
        proposed_source_report_id=canonical.source_report_id,
    ):
        work.unchanged.append(existing)
        return
    work.updated.append(
        _record_canonical_fact(
            work,
            month=month,
            channel_id=channel_id,
            source_kind=source_kind,
            canonical=canonical,
            actor_user_id=actor_user_id,
        )
    )


# ============================================================================
# Purpose: Process one source-row bucket through skip classification,
#          canonical selection, and revenue-fact write classification.
# Database/ORM: Writes only through SqlAlchemyRevenueFactRepository.record_fact().
# Standards: Exact skip reasons, actor-insensitive unchanged comparison, typed
#            RevenueFactValidationError for unsupported source systems.
# Blast Radius: Finance totals, audit trail, month-lock integrity.
# ============================================================================
def _process_source_bucket(
    work: _NormalizationWork,
    *,
    month: str,
    channel_id: str | None,
    source_system: str,
    bucket_rows: list[GoogleRevenueSourceRowEntry],
    active_channel_ids: set[str],
    actor_user_id: str,
) -> None:
    """Normalize one homogeneous channel/source-system bucket."""
    rejection_reason = _bucket_rejection_reason(channel_id, active_channel_ids)
    if rejection_reason is not None:
        _append_skipped(work.skipped, bucket_rows, rejection_reason)
        return
    assert channel_id is not None
    usd_rows = _eligible_usd_rows(bucket_rows, work.skipped)
    if not usd_rows:
        return
    canonical = _canonical_source_row(usd_rows, work.skipped)
    if canonical is None:
        return
    _classify_canonical_fact(
        work,
        month=month,
        channel_id=channel_id,
        source_kind=_source_kind_for_system(source_system),
        canonical=canonical,
        actor_user_id=actor_user_id,
    )


def _normalization_result(work: _NormalizationWork) -> NormalizationResult:
    """Freeze accumulated normalization lists into the public result shape."""
    return NormalizationResult(
        created=work.created,
        updated=work.updated,
        unchanged=work.unchanged,
        skipped=work.skipped,
    )


def _log_normalization_complete(
    *,
    tenant_id: UUID,
    month: str,
    result: NormalizationResult,
) -> None:
    """Emit stable completion telemetry without source-row identifiers."""
    reason_counts = Counter(s.reason.value for s in result.skipped)
    logger.info(
        "normalize_month complete tenant_id=%s month=%s "
        "created=%d updated=%d unchanged=%d skipped=%d "
        "skipped_by_reason=%s",
        tenant_id,
        month,
        len(result.created),
        len(result.updated),
        len(result.unchanged),
        len(result.skipped),
        dict(reason_counts),
    )


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
        _validate_month(month)
        normalized_channel_ids: set[str] | None = (
            set(channel_ids) if channel_ids is not None else None
        )

        # FIX: UMS_LOG_LEVEL defaults to INFO, so this first-party INFO line
        # reaches the retained Docker logs; emitting the raw triggering user's
        # UUID there leaked a private user identifier (PR #210 review). The log
        # carries a short SHA-256 fingerprint instead -- the persisted audit
        # rows remain the attribution source of record.
        logger.info(
            "normalize_month start tenant_id=%s month=%s channel_scope=%s actor_fp=%s",
            self._tenant_id,
            month,
            _channel_scope_label(normalized_channel_ids),
            _actor_log_label(actor_user_id),
        )

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

        source_repo = SqlAlchemyGoogleRevenueSourceRowRepository(self._session)
        in_scope_rows = _scoped_source_rows(
            source_repo.list(self._tenant_id, report_month=month),
            normalized_channel_ids,
        )
        active_channel_ids = _active_channel_ids(
            self._session,
            tenant_id=self._tenant_id,
            rows=in_scope_rows,
        )
        buckets = _source_row_buckets(in_scope_rows)
        work = _NormalizationWork(
            facts_repo=SqlAlchemyRevenueFactRepository(
                self._session,
                tenant_id=self._tenant_id,
            )
        )

        for (channel_id, source_system), bucket_rows in buckets.items():
            _process_source_bucket(
                work,
                month=month,
                channel_id=channel_id,
                source_system=source_system,
                bucket_rows=bucket_rows,
                active_channel_ids=active_channel_ids,
                actor_user_id=actor_user_id,
            )

        result = _normalization_result(work)
        _log_normalization_complete(tenant_id=self._tenant_id, month=month, result=result)
        return result
