# ============================================================================
# Purpose: Service-layer reads behind the smart-alert signals that more than
#   one API surface composes — the missing-facts coverage pair, the
#   connector skipped-source-row signal, the tenant resolution they share,
#   and the previous-month derivation feeding the trend signals. Extracted
#   from api/revenue.py so sibling route modules (exports, reconciliation)
#   stop importing another route module's internals (the api-layering
#   refactor).
# Database/ORM: None directly — the SQL lives behind the repositories
#   (SqlAlchemyRevenueFactRepository.missing_required_fact_channel_count_and_sample,
#   SqlAlchemyAuditLogRepository.connector_run_details_for_finance_month);
#   this module orchestrates their typed results. Read-only; no locks, no
#   writes. PostgreSQL is the source of truth.
# Standards: Repository-delegated reads plus pure month arithmetic; every
#   read is tenant-scoped through resolve_smart_alert_tenant_id, passed
#   explicitly to the repository constructors. No authorization decisions
#   are made here — the routes gate BEFORE calling (skipped-row reads
#   additionally sit behind VIEW_AUDIT_LOG at the callers), and whether a
#   read happens inside the composed-read snapshot is the CALLER's choice
#   of session (the routes pass the platform snapshot session for the
#   coverage pair and the tenant-lane session for the audit-gated signal,
#   per the recorded laning residual).
# Blast Radius: Smart-alert and export signal values only; no finance
#   mutation, no auth, no audit writes.
# Connections:
#   - File: backend/ums_smart_revenue/finance/revenue_facts.py -> owns the
#     coverage-pair SQL (channels LEFT JOIN facts).
#   - File: backend/ums_smart_revenue/auth/audit_log.py -> owns the
#     connector-run audit-details SQL read newest-first here.
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

from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit_log import SqlAlchemyAuditLogRepository
from ums_smart_revenue.finance.revenue_facts import (
    RevenueFactValidationError,
    SqlAlchemyRevenueFactRepository,
)
from ums_smart_revenue.finance.smart_alerts import MISSING_FACT_CHANNEL_SAMPLE_LIMIT
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

MONTH_VALUE_PATTERN = re.compile(r"^\d{4}-\d{2}$")


# ============================================================================
# Purpose: Resolve which tenant's data the smart-alert signal reads select —
#   the request tenant when one is bound, else the bootstrap UMS tenant.
# Database/ORM: None; reads the tenancy contextvar only.
# Standards: Mirrors the finance repositories' tenant fallback exactly, and
#   the signal readers pass the resolved id EXPLICITLY to the repository
#   constructors so signal reads and repository reads cannot diverge on
#   tenant.
# Blast Radius: Tenant selection for every smart-alert/export signal read;
#   a change here re-scopes which tenant's finance data those surfaces see.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/context.py ->
#     get_current_tenant, the request-scoped tenant binding read here.
#   - File: backend/ums_smart_revenue/api/exports.py -> stamps the resolved
#     tenant id into export artifacts for parity with the dashboard.
# ============================================================================
def resolve_smart_alert_tenant_id() -> UUID:
    """Resolve the request tenant id, mirroring the finance repositories."""
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return UUID(UMS_TENANT_ID)


# ============================================================================
# Purpose: Derive the prior YYYY-MM finance month feeding the dashboard and
#   export trend signals (previous-month fact comparisons).
# Database/ORM: None; pure string/calendar arithmetic.
# Standards: Validates shape then range, raising RevenueFactValidationError
#   (translated to 4xx at the route boundary) for malformed months and for
#   the no-predecessor edges (year 0000, month 0001-01); zero-pads results
#   so callers can use them directly as fact-month keys.
# Blast Radius: Trend-signal month selection for smart alerts and exports;
#   no finance mutation.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py -> previous-month fact
#     listing for the smart-alerts trend signal.
#   - File: backend/ums_smart_revenue/api/exports.py -> the export builder's
#     previous-month comparison reads.
#   - File: backend/ums_smart_revenue/finance/revenue_facts.py ->
#     RevenueFactValidationError, the typed error raised here.
# ============================================================================
def previous_month(month: str) -> str:
    """Return the YYYY-MM string for the calendar month immediately preceding the given month.

    Raises:
        RevenueFactValidationError: If `month` is not a YYYY-MM string with a
            calendar month from 01 to 12, or names 0000 or 0001-01 (which have
            no preceding calendar month).
    """
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
# Purpose: Serve the missing-facts coverage pair (total count + bounded
#   sample) that the smart-alert builder and the finance exports compose.
# Database/ORM: Delegated — SqlAlchemyRevenueFactRepository
#   .missing_required_fact_channel_count_and_sample owns the read-only LEFT
#   JOIN of YouTubeChannelORM x MonthlyChannelRevenueFactORM.
# Standards: Tenant-scoped via resolve_smart_alert_tenant_id passed
#   explicitly to the repository; the sample is bounded by
#   MISSING_FACT_CHANNEL_SAMPLE_LIMIT so a bad ingestion month cannot turn
#   the alert endpoint into an unbounded scan/transfer. Callers gate
#   permissions BEFORE calling and choose the session lane (the routes pass
#   the platform snapshot session per the composed-read rulings).
# Blast Radius: Smart-alert and export signal values only; no finance
#   mutation, no auth, no audit writes.
# Connections:
#   - File: backend/ums_smart_revenue/finance/revenue_facts.py -> the
#     repository method owning the SQL.
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
    most `MISSING_FACT_CHANNEL_SAMPLE_LIMIT` channel ids, both read through
    the revenue-fact repository so a bad ingestion month cannot turn the
    alert endpoint into an unbounded scan/transfer.

    When `youtube_channel_ids` is provided (non-None), the read is scoped to
    those channels — used by the export helper so a company/sector/group
    export never leaks factless channel ids outside the exported scope. When
    omitted (None), the read is tenant-global — the smart-alerts API
    endpoint stays global by design.
    """
    repository = SqlAlchemyRevenueFactRepository(session, tenant_id=resolve_smart_alert_tenant_id())
    return repository.missing_required_fact_channel_count_and_sample(
        month=month,
        sample_limit=MISSING_FACT_CHANNEL_SAMPLE_LIMIT,
        youtube_channel_ids=youtube_channel_ids,
    )


# ============================================================================
# Purpose: Derive the smart-alert source-row skip signal for one finance
#   month from the newest relevant connector-run audit edge only.
# Database/ORM: Delegated — SqlAlchemyAuditLogRepository
#   .connector_run_details_for_finance_month owns the newest-first
#   CONNECTOR_JOB_RUN details SELECT, streamed in small batches; read-only.
#   The early break below bounds what a month's re-run history transfers.
# Standards: Tenant-scoped via resolve_smart_alert_tenant_id passed
#   explicitly to the repository. The JSON lifecycle interpretation happens
#   here (portable across SQLite tests and PostgreSQL): a newer clean edge
#   clears older ROWS_SKIPPED history, malformed or zero-count rows are
#   tolerated, and the reason breakdown is redacted unless the caller holds
#   VIEW_SENSITIVE_AUDIT_PAYLOADS. Callers gate VIEW_AUDIT_LOG first and
#   keep this read on the tenant lane per the recorded laning residual.
# Blast Radius: Finance dashboard/export read surface only; no finance
#   mutation, no auth, no audit writes.
# Connections:
#   - File: backend/ums_smart_revenue/auth/audit_log.py -> the repository
#     method owning the SQL.
#   - File: backend/ums_smart_revenue/connectors/runs/normalization.py ->
#     emits the ROWS_SKIPPED edges interpreted here.
#   - File: backend/ums_smart_revenue/finance/smart_alerts.py -> consumes
#     the aggregate as SOURCE_ROWS_SKIPPED.
# ============================================================================
def skipped_source_row_count_and_reasons(
    session: Session,
    *,
    month: str,
    include_sensitive_details: bool = True,
) -> tuple[int, dict[str, int]]:
    """Return skipped source rows + skip reasons for one finance month.

    Reads connector `ROWS_SKIPPED` audit edges through the audit-log
    repository (tenant, `CONNECTOR_JOB_RUN` event type, `FINANCE_MONTH`
    scope, the requested month, newest first). The function returns the
    newest connector-run signal only — not the sum across re-runs — because
    fact projection is idempotent and each connector run emits its own edge
    for the same month; aggregating across runs would over-count stale or
    duplicate signals (review threads #1 and #10). A newer clean connector
    edge clears older `ROWS_SKIPPED` history. Malformed or zero-count rows
    are tolerated: `skipped_count` and `skipped_by_reason` are reconciled
    via `max()` so the returned pair is internally consistent (review
    thread #8). Filtering the JSON lifecycle in Python keeps the read
    portable across SQLite tests and PostgreSQL production while the SQL
    predicates stay tenant/month/event scoped in the repository.

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
    repository = SqlAlchemyAuditLogRepository(session, tenant_id=resolve_smart_alert_tenant_id())
    # FIX: Read only the newest relevant connector edge for the month. If that
    # newest edge is not ROWS_SKIPPED, a clean re-run has superseded the older
    # skipped-row audit history and the dashboard must not show a stale alert.
    details_rows = repository.connector_run_details_for_finance_month(month)
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
