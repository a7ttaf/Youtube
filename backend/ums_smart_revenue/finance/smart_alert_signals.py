# ============================================================================
# Purpose: Service-layer reads behind the smart-alert signals that more than
#   one API surface composes — the missing-facts coverage pair, the
#   connector skipped-source-row signal, the tenant resolution they share,
#   and the previous-month derivation feeding the trend signals. Extracted
#   from api/revenue.py so sibling route modules (exports, reconciliation)
#   stop importing another route module's internals (the api-layering
#   refactor).
# Database/ORM: Read-only SELECTs on YouTubeChannelORM x
#   MonthlyChannelRevenueFactORM (coverage) and AuditLogORM (skipped rows);
#   no locks, no writes. PostgreSQL is the source of truth.
# Standards: Pure reads plus pure month arithmetic; every function is
#   tenant-scoped through resolve_smart_alert_tenant_id, mirroring the
#   finance repositories. No authorization decisions are made here — the
#   routes gate BEFORE calling (skipped-row reads additionally sit behind
#   VIEW_AUDIT_LOG at the callers), and whether a read happens inside the
#   composed-read snapshot is the CALLER's choice of session (the routes
#   pass the platform snapshot session for the coverage pair and the
#   tenant-lane session for the audit-gated signal, per the recorded laning
#   residual).
# Blast Radius: Smart-alert and export signal values only; no finance
#   mutation, no auth, no audit writes.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py -> the smart-alerts
#     route composes these around its permission gates.
#   - File: backend/ums_smart_revenue/api/exports.py -> the finance export
#     builder reads the same signals for artifact parity.
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> the pure
#     alert builder consuming the returned values.
#   - File: backend/ums_smart_revenue/connectors/runs/normalization.py ->
#     emits the ROWS_SKIPPED audit edges read here.
# ============================================================================
"""Shared smart-alert signal reads: coverage pair, skipped rows, month math."""

import re
from uuid import UUID

from sqlalchemy import Integer, literal_column, select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.scopes import ScopeType
from ums_smart_revenue.db.finance_models import MonthlyChannelRevenueFactORM
from ums_smart_revenue.db.org_models import YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM
from ums_smart_revenue.finance.revenue_facts import RevenueFactValidationError
from ums_smart_revenue.finance.smart_alerts import MISSING_FACT_CHANNEL_SAMPLE_LIMIT
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

MONTH_VALUE_PATTERN = re.compile(r"^\d{4}-\d{2}$")


def resolve_smart_alert_tenant_id() -> UUID:
    """Resolve the request tenant id, mirroring the finance repositories."""
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return UUID(UMS_TENANT_ID)


def previous_month(month: str) -> str:
    """Return the YYYY-MM string for the calendar month immediately preceding the given month."""
    if not MONTH_VALUE_PATTERN.fullmatch(month):
        raise RevenueFactValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )
    try:
        year_value, month_value = month.split("-", maxsplit=1)
        year = int(year_value)
        month_number = int(month_value)
    except ValueError as exc:
        raise RevenueFactValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        ) from exc
    if year == 0 or month_number < 1 or month_number > 12:
        raise RevenueFactValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )
    if month_number == 1:
        if year == 1:
            raise RevenueFactValidationError(
                "month must use YYYY-MM with a calendar month from 01 to 12"
            )
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month_number - 1:02d}"


# ============================================================================
# Purpose: Read the active, revenue-required channels that have no revenue fact
#   for the month, so the smart-alert builder can emit per-channel coverage
#   gaps. Bounded: returns the total COUNT plus an ordered sample capped at
#   `MISSING_FACT_CHANNEL_SAMPLE_LIMIT` ids, so a bad ingestion month cannot
#   turn the alert endpoint into an unbounded scan/transfer.
# Database/ORM: Read-only LEFT JOIN of YouTubeChannelORM x
#   MonthlyChannelRevenueFactORM (no FOR UPDATE), tenant-scoped, source of
#   truth in PostgreSQL. The count uses `COUNT(*)` server-side; the
#   sample is a separate ordered `LIMIT 20` SELECT.
# Standards: Mirrors month_close_readiness._missing_required_revenue_fact_count
#   exactly (active.is_(True) AND revenue_required.is_(True) AND fact.id IS NULL).
#   No write, no lock, no Neo4j.
# Blast Radius: Finance read surface only; no auth/audit/finance mutation.
# Connections:
#   - File: backend/ums_smart_revenue/finance/month_close_readiness.py ->
#     shared query shape (count there; count + bounded sample here).
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> consumes
#     (count, sample) for the coverage alert details.
# ============================================================================
def missing_revenue_fact_channel_count_and_sample(
    session: Session,
    *,
    month: str,
    youtube_channel_ids: set[str] | None = None,
) -> tuple[int, list[str]]:
    """Return (count, sample) of active revenue-required channels with no fact for the month.

    `count` is the total matching channels; `sample` is a sorted list of at
    most `MISSING_FACT_CHANNEL_SAMPLE_LIMIT` channel ids. The two values are
    read by two independent queries so a large factless set never materializes
    a full id list on the application side.

    When `youtube_channel_ids` is provided (non-None), the read is scoped to
    those channels — used by the export helper so a company/sector/group
    export never leaks factless channel ids outside the exported scope. When
    omitted (None), the read is tenant-global — the smart-alerts API
    endpoint stays global by design.
    """
    tenant_id = resolve_smart_alert_tenant_id()
    join_predicates = (
        (MonthlyChannelRevenueFactORM.tenant_id == YouTubeChannelORM.tenant_id)
        & (MonthlyChannelRevenueFactORM.youtube_channel_id == YouTubeChannelORM.youtube_channel_id)
        & (MonthlyChannelRevenueFactORM.tenant_id == tenant_id)
        & (MonthlyChannelRevenueFactORM.month == month),
    )
    where_predicates = [
        YouTubeChannelORM.tenant_id == tenant_id,
        YouTubeChannelORM.active.is_(True),
        YouTubeChannelORM.revenue_required.is_(True),
        MonthlyChannelRevenueFactORM.id.is_(None),
    ]
    if youtube_channel_ids is not None:
        where_predicates.append(YouTubeChannelORM.youtube_channel_id.in_(youtube_channel_ids))
    count_statement = (
        select(literal_column("COUNT(*)", type_=Integer()))
        .select_from(YouTubeChannelORM)
        .outerjoin(MonthlyChannelRevenueFactORM, *join_predicates)
        .where(*where_predicates)
    )
    sample_statement = (
        select(YouTubeChannelORM.youtube_channel_id)
        .select_from(YouTubeChannelORM)
        .outerjoin(MonthlyChannelRevenueFactORM, *join_predicates)
        .where(*where_predicates)
        .order_by(YouTubeChannelORM.youtube_channel_id)
        .limit(MISSING_FACT_CHANNEL_SAMPLE_LIMIT)
    )
    count_value = int(session.execute(count_statement).scalar_one())
    sample_ids = list(session.scalars(sample_statement).all())
    return count_value, sample_ids


# ============================================================================
# Purpose: Read connector normalization audit edges for a finance month and
#   derive the current smart-alert source-row skip signal from the newest
#   relevant connector-run edge only. Filtering the JSON lifecycle in Python
#   keeps the read portable across SQLite tests and PostgreSQL production while
#   the SQL predicates stay tenant/month/event scoped.
# Database/ORM: Read-only SELECT on AuditLogORM / audit_logs; no locks or
#   writes. PostgreSQL remains the source of truth for audit observability.
# Standards: Tenant-scoped via resolve_smart_alert_tenant_id; a newer clean
#   connector edge clears older ROWS_SKIPPED history; ignores malformed or
#   zero-count audit details instead of making the dashboard fail on legacy
#   audit rows; preserves valid reason counts exactly.
# Blast Radius: Finance dashboard read surface; no finance calculation,
#   authorization, export, ingestion, or audit-write behavior changes.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/normalization.py ->
#     emits lifecycle=ROWS_SKIPPED with skipped_count/skipped_by_reason.
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> consumes the
#     aggregate as SOURCE_ROWS_SKIPPED.
# ============================================================================
def skipped_source_row_count_and_reasons(
    session: Session,
    *,
    month: str,
    include_sensitive_details: bool = True,
) -> tuple[int, dict[str, int]]:
    """Return skipped source rows + skip reasons for one finance month.

    Reads connector `ROWS_SKIPPED` audit edges scoped by tenant,
    `CONNECTOR_JOB_RUN` event type, `FINANCE_MONTH` scope, and the requested
    month. The function returns the newest connector-run signal only — not
    the sum across re-runs — because fact projection is idempotent and each
    connector run emits its own edge for the same month; aggregating across
    runs would over-count stale or duplicate signals (review threads #1 and
    #10). A newer clean connector edge clears older `ROWS_SKIPPED` history.
    Malformed or zero-count rows are tolerated: `skipped_count` and
    `skipped_by_reason` are reconciled via `max()` so the returned pair is
    internally consistent (review thread #8).

    Args:
        session: Active SQLAlchemy session.
        month: Finance month in `YYYY-MM` format (already validated by caller).
        include_sensitive_details: When False, the returned reason breakdown is
            redacted to an empty dict so callers without
            `VIEW_SENSITIVE_AUDIT_PAYLOADS` cannot learn per-reason counts.
            The total `skipped_count` is still returned because the count is
            operational, not sensitive.

    Returns:
        A (count, reasons_by_label) tuple. `reasons_by_label` is always
        returned in deterministic key-sorted order.
    """
    tenant_id = resolve_smart_alert_tenant_id()
    # FIX: Read only the newest relevant connector edge for the month. If that
    # newest edge is not ROWS_SKIPPED, a clean re-run has superseded the older
    # skipped-row audit history and the dashboard must not show a stale alert.
    details_rows = session.scalars(
        select(AuditLogORM.details)
        .where(
            AuditLogORM.tenant_id == tenant_id,
            AuditLogORM.event_type == AuditEventType.CONNECTOR_JOB_RUN.value,
            AuditLogORM.scope_type == ScopeType.FINANCE_MONTH.value,
            AuditLogORM.scope_id == month,
        )
        .order_by(AuditLogORM.created_at.desc(), AuditLogORM.id.desc())
    ).all()
    latest_details: dict[str, object] | None = None
    for details in details_rows:
        if not isinstance(details, dict):
            continue
        if details.get("lifecycle") != "ROWS_SKIPPED":
            return 0, {}
        latest_details = details
        break
    if latest_details is None:
        return 0, {}
    reason_counts = _skipped_reason_counts_from_details(latest_details)
    skipped_count = _positive_int(latest_details.get("skipped_count"))
    reasons_total = sum(reason_counts.values())
    if skipped_count > 0 and reasons_total > 0:
        effective_count = max(skipped_count, reasons_total)
    elif skipped_count > 0:
        effective_count = skipped_count
    elif reasons_total > 0:
        effective_count = reasons_total
    else:
        effective_count = 0
    if not include_sensitive_details:
        # Redact per-reason breakdown; keep total count visible.
        return effective_count, {}
    return effective_count, dict(sorted(reason_counts.items()))


def _skipped_reason_counts_from_details(details: dict[str, object]) -> dict[str, int]:
    """Extract positive reason counts from one ROWS_SKIPPED audit detail payload."""
    raw_reason_counts = details.get("skipped_by_reason")
    if not isinstance(raw_reason_counts, dict):
        return {}
    reason_counts: dict[str, int] = {}
    for raw_reason, raw_count in raw_reason_counts.items():
        reason = str(raw_reason).strip()
        count = _positive_int(raw_count)
        if reason and count > 0:
            reason_counts[reason] = reason_counts.get(reason, 0) + count
    return reason_counts


def _positive_int(value: object) -> int:
    """Return positive JSON integer-like values; malformed values collapse to zero."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value > 0 else 0
    if isinstance(value, str) and value.strip().isdecimal():
        return int(value)
    return 0
