# ============================================================================
# Purpose: Build the dashboard-facing monthly smart-alert summary for the
#   finance command center. Pure functions over pre-aggregated inputs; never
#   reads from the database, never triggers ingestion, never mutates finance
#   numbers or audit state. Each per-alert helper is independently testable.
# Database/ORM: None (pure module — all DB access lives in api/revenue.py and
#   api/exports.py, which pre-aggregate and pass typed inputs here).
# Standards: smart-alerts builder consumes pre-aggregated inputs; rejects
#   negative thresholds/counts at the boundary; preserves reason totals
#   deterministically; keeps highest_severity == None when no alerts fire.
# Blast Radius: Finance dashboard read surface (no auth, no money, no Neo4j).
#   Adds connector-backed alert codes for normalization drops and failed runs.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py -> pre-reads coverage and
#     audit-derived inputs and calls build_monthly_smart_alert_summary.
#   - File: backend/ums_smart_revenue/api/exports.py -> mirrors the API path
#     so workbook/PDF/PPTX exports surface the same alerts.
#   - File: Docs/12_BACKEND_API_SPEC.md -> alert code contract (severity,
#     source, confidence, details shape).
# ============================================================================
"""Pure monthly smart-alert composition for finance dashboard and exports."""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ums_smart_revenue.finance.bank_reconciliation import (
    MonthBankReconciliationSummary,
)
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.payment_matching import MonthlyPaymentMatchSummary
from ums_smart_revenue.finance.reconciliation import SOURCE_PRIORITY
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry

DEFAULT_HIGH_GAP_THRESHOLD_USD = Decimal("100.00")
DEFAULT_REVENUE_TREND_ANOMALY_THRESHOLD_PERCENT = Decimal("0.50")
_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}
MISSING_FACT_CHANNEL_SAMPLE_LIMIT = 20


@dataclass(frozen=True)
class MonthlySmartAlert:
    """Represents one monthly smart alert."""

    code: str
    severity: str
    message: str
    source: str
    confidence: str
    details: dict[str, object]

    def to_api(self) -> dict[str, object]:
        """Convert the smart alert instance into an API dictionary."""
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "source": self.source,
            "confidence": self.confidence,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class MonthlySmartAlertSummary:
    """Summary of smart alerts for one finance month."""

    month: str
    status: str
    highest_severity: str | None
    alerts: list[MonthlySmartAlert]

    def to_api(self) -> dict[str, object]:
        """Convert the monthly smart alert summary into a dictionary for API output."""
        return {
            "month": self.month,
            "status": self.status,
            "highest_severity": self.highest_severity,
            "alert_count": len(self.alerts),
            "alerts": [alert.to_api() for alert in self.alerts],
        }


@dataclass(frozen=True)
class MonthlySmartAlertFinanceInputs:
    """Finance-domain inputs required by monthly smart-alert composition."""

    payment_match: MonthlyPaymentMatchSummary
    bank_reconciliation: MonthBankReconciliationSummary
    close_status: str | None
    manual_overrides: Iterable[RevenueManualOverrideEntry]


@dataclass(frozen=True)
class MonthlySmartAlertAuditSignals:
    """Audit-derived and coverage inputs for monthly smart-alert composition."""

    missing_revenue_fact_channel_count: int = 0
    missing_revenue_fact_channel_sample: Sequence[str] = ()
    skipped_source_row_count: int = 0
    skipped_source_rows_by_reason: Mapping[str, int] | None = None
    failed_connector_run_count: int = 0
    failed_connector_runs_by_status: Mapping[str, int] | None = None


@dataclass(frozen=True)
class MonthlySmartAlertTrendSignals:
    """Revenue trend inputs for monthly smart-alert composition."""

    current_revenue_facts: Iterable[RevenueFactEntry] = ()
    previous_revenue_facts: Iterable[RevenueFactEntry] = ()


@dataclass(frozen=True)
class MonthlySmartAlertThresholds:
    """Thresholds applied by monthly smart-alert composition."""

    high_gap_threshold_usd: Decimal = DEFAULT_HIGH_GAP_THRESHOLD_USD
    revenue_trend_anomaly_threshold_percent: Decimal = (
        DEFAULT_REVENUE_TREND_ANOMALY_THRESHOLD_PERCENT
    )


_DEFAULT_AUDIT_SIGNALS = MonthlySmartAlertAuditSignals()
_DEFAULT_TREND_SIGNALS = MonthlySmartAlertTrendSignals()
_DEFAULT_THRESHOLDS = MonthlySmartAlertThresholds()


# ============================================================================
# Purpose: Compose the dashboard smart-alert list for one finance month from
#   pre-aggregated cross-domain inputs (payment match, bank reconciliation,
#   coverage gap, audit-derived skipped rows, audit-derived failed connector
#   runs, overrides, MoM revenue anomaly, close status). Each per-alert helper
#   is a pure function returning the alert or None, so this builder only orders
#   and ranks them.
# Database/ORM: None (pure).
# Standards: Boundary validation rejects negative thresholds and counts;
#   deterministically orders alerts; assigns the highest severity from the
#   produced set; preserves reason/status totals for connector-backed alerts.
# Blast Radius: Finance dashboard read surface. No auth, no money, no Neo4j.
#   Adding/changing an alert code is an API contract change — keep wire-stable.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py -> primary caller.
#   - File: backend/ums_smart_revenue/api/exports.py -> export mirror caller.
#   - File: Docs/12_BACKEND_API_SPEC.md -> alert code wire contract.
# ============================================================================
def build_monthly_smart_alert_summary(
    *,
    month: str,
    finance: MonthlySmartAlertFinanceInputs,
    audit_signals: MonthlySmartAlertAuditSignals = _DEFAULT_AUDIT_SIGNALS,
    trend_signals: MonthlySmartAlertTrendSignals = _DEFAULT_TREND_SIGNALS,
    thresholds: MonthlySmartAlertThresholds = _DEFAULT_THRESHOLDS,
) -> MonthlySmartAlertSummary:
    """Build a monthly smart alert summary from finance signal inputs.

    The coverage alert takes a (count, sample) pair instead of a full id
    list so the route can bound the read at the SQL LIMIT clause. The
    alert details keep the same wire shape (channel_count + sample_channel_ids).
    """
    _validate_monthly_smart_alert_inputs(
        thresholds=thresholds,
        audit_signals=audit_signals,
    )
    normalized_close_status = finance.close_status or "OPEN"
    overrides = list(finance.manual_overrides)

    # Per-alert builders. Each one is a small pure function that returns a
    # MonthlySmartAlert or None (None is dropped). Splitting them out drops
    # build_monthly_smart_alert_summary's cyclomatic complexity below the
    # PY-R1000 high-risk threshold and makes each branch independently
    # testable / rewritable without touching the others.
    builders: list[Callable[[], MonthlySmartAlert | None]] = [
        lambda: _missing_revenue_source_alert(month, finance.payment_match),
        lambda: _channels_missing_revenue_facts_alert(
            month,
            audit_signals.missing_revenue_fact_channel_count,
            audit_signals.missing_revenue_fact_channel_sample,
        ),
        lambda: _source_rows_skipped_alert(
            month,
            audit_signals.skipped_source_row_count,
            audit_signals.skipped_source_rows_by_reason,
        ),
        lambda: _connector_runs_failed_alert(
            month,
            audit_signals.failed_connector_run_count,
            audit_signals.failed_connector_runs_by_status,
        ),
        lambda: _payment_not_matched_alert(month, finance.payment_match),
        lambda: _bank_reconciliation_alert(month, finance.bank_reconciliation),
        lambda: _unexplained_gap_alert(
            month,
            finance.payment_match,
            finance.bank_reconciliation,
            thresholds.high_gap_threshold_usd,
        ),
        lambda: _revenue_trend_anomaly_alert(
            month,
            trend_signals.current_revenue_facts,
            trend_signals.previous_revenue_facts,
            thresholds.revenue_trend_anomaly_threshold_percent,
        ),
        lambda: _month_not_locked_alert(month, normalized_close_status),
        lambda: _manual_overrides_alert(month, overrides),
    ]
    alerts = [alert for build in builders if (alert := build()) is not None]

    highest_severity = _highest_severity(alerts)
    return MonthlySmartAlertSummary(
        month=month,
        status="ATTENTION_REQUIRED" if alerts else "CLEAR",
        highest_severity=highest_severity,
        alerts=alerts,
    )


def _validate_monthly_smart_alert_inputs(
    *,
    thresholds: MonthlySmartAlertThresholds,
    audit_signals: MonthlySmartAlertAuditSignals,
) -> None:
    """Reject impossible negative smart-alert thresholds and counts."""
    _ensure_non_negative("high_gap_threshold_usd", thresholds.high_gap_threshold_usd)
    _ensure_non_negative(
        "revenue_trend_anomaly_threshold_percent",
        thresholds.revenue_trend_anomaly_threshold_percent,
    )
    _ensure_non_negative(
        "missing_revenue_fact_channel_count",
        audit_signals.missing_revenue_fact_channel_count,
    )
    _ensure_non_negative("skipped_source_row_count", audit_signals.skipped_source_row_count)
    _ensure_mapping_counts_non_negative(
        "skipped_source_rows_by_reason",
        audit_signals.skipped_source_rows_by_reason,
    )
    _ensure_non_negative(
        "failed_connector_run_count",
        audit_signals.failed_connector_run_count,
    )
    _ensure_mapping_counts_non_negative(
        "failed_connector_runs_by_status",
        audit_signals.failed_connector_runs_by_status,
    )


def _ensure_non_negative(name: str, value: Decimal | int) -> None:
    """Raise a stable ValueError when a count or threshold is negative."""
    if value < 0:
        raise ValueError(f"{name} must be non-negative")


def _ensure_mapping_counts_non_negative(
    name: str,
    counts: Mapping[str, int] | None,
) -> None:
    """Raise a stable ValueError when any mapping count is negative."""
    if counts is not None and any(count < 0 for count in counts.values()):
        raise ValueError(f"{name} counts must be non-negative")


def _missing_revenue_source_alert(
    month: str, payment_match: MonthlyPaymentMatchSummary
) -> MonthlySmartAlert | None:
    """Month-level zero-YouTube-revenue signal."""
    if payment_match.status != "NO_YOUTUBE_REVENUE":
        return None
    return MonthlySmartAlert(
        code="MISSING_REVENUE_SOURCE",
        severity="HIGH",
        message=f"No YouTube revenue source is available for {month}.",
        source="payment_match",
        confidence="E_MISSING",
        details={"payment_match_status": payment_match.status},
    )


def _channels_missing_revenue_facts_alert(
    month: str,
    count: int,
    sample: Sequence[str],
) -> MonthlySmartAlert | None:
    # ========================================================================
    # Purpose: Per-channel coverage gap when active, revenue-required channels
    #   have no revenue fact for the month. Distinct from MISSING_REVENUE_SOURCE
    #   (month-level zero YouTube revenue); mirrors the close-readiness
    #   MISSING_REVENUE_FACTS blocker.
    # Database/ORM: None (pure; count + sample are pre-read by the route).
    # Standards: Cap sample to MISSING_FACT_CHANNEL_SAMPLE_LIMIT; trust the
    #   route's server-side LIMIT 20 + ORDER BY youtube_channel_id. If the
    #   sample is shorter than the count the route pre-cap was tighter (only
    #   possible on a custom-injected sample), so we re-cap here for safety.
    # Blast Radius: Finance read surface. No auth, no money, no Neo4j.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/revenue.py -> pre-reads the
    #     (count, sample) pair via two bounded queries.
    #   - File: backend/ums_smart_revenue/finance/month_close_readiness.py ->
    #     shares the active+revenue_required-without-fact query shape.
    # ========================================================================
    capped_sample = sorted(sample)[:MISSING_FACT_CHANNEL_SAMPLE_LIMIT]
    if count <= 0 and not capped_sample:
        return None
    return MonthlySmartAlert(
        code="CHANNELS_MISSING_REVENUE_FACTS",
        severity="HIGH",
        message=(f"{count} active revenue-required channel(s) have no revenue facts for {month}."),
        source="revenue_facts",
        confidence="E_MISSING",
        details={
            "channel_count": count,
            "sample_channel_ids": capped_sample,
        },
    )


# ============================================================================
# Purpose: Surface source revenue rows that connector normalization dropped
#   from monthly fact projection. The adapter already records this as a
#   finance-month-scoped ROWS_SKIPPED audit edge; this turns that same
#   source-backed signal into a dashboard smart alert.
# Database/ORM: None (pure; count + reason totals are pre-read by the API).
# Standards: Preserve reason counts, omit zero-count reasons, and do not
#   change ingestion, matching, close, or exported finance numbers.
#   Reconcile count vs sum(reason_counts) via max() so the alert is internally
#   consistent even when an upstream audit row is malformed or legacy.
# Blast Radius: Finance dashboard read surface only; audit-derived signal.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/runs/normalization.py ->
#     emits ROWS_SKIPPED audit edges with skipped_count/skipped_by_reason.
#   - File: backend/ums_smart_revenue/api/revenue.py -> aggregates the
#     current month audit edges into this pure builder input.
# ============================================================================
def _source_rows_skipped_alert(
    month: str,
    count: int,
    skipped_by_reason: Mapping[str, int] | None,
) -> MonthlySmartAlert | None:
    reason_counts = {
        reason: reason_count
        for reason, reason_count in sorted((skipped_by_reason or {}).items())
        if reason_count > 0
    }
    # FIX: reconcile count vs sum(reason_counts) so the reported total always
    # matches the breakdown. A legacy/malformed audit row may carry one without
    # the other; we never report a count that disagrees with the reasons map.
    reasons_total = sum(reason_counts.values())
    if count > 0 and reasons_total > 0:
        effective_count = max(count, reasons_total)
    elif count > 0:
        effective_count = count
    elif reasons_total > 0:
        effective_count = reasons_total
    else:
        effective_count = 0
    if effective_count <= 0:
        return None
    return MonthlySmartAlert(
        code="SOURCE_ROWS_SKIPPED",
        severity="HIGH",
        message=(
            f"{effective_count} source revenue row(s) were skipped during "
            f"fact projection for {month}."
        ),
        source="connector_job_run",
        confidence="E_MISSING",
        details={
            "skipped_count": effective_count,
            "skipped_by_reason": reason_counts,
        },
    )


# ============================================================================
# Purpose: Surface connector runs whose latest terminal audit edge for a month
#   is FAILED or PARTIAL. This catches missing upstream reports and per-report
#   failures even when no source rows were normalized.
# Database/ORM: None (pure; count + status totals are pre-read by the API).
# Standards: Preserve status counts, omit zero-count statuses, and reconcile
#   total vs breakdown via max() so legacy/malformed audit rows cannot produce
#   internally inconsistent details.
# Blast Radius: Finance dashboard read surface only; audit-derived signal.
# Connections:
#   - File: backend/ums_smart_revenue/connectors/google/audit.py -> emits
#     CONNECTOR_JOB_RUN/FINISHED audit edges with status and report_month.
#   - File: backend/ums_smart_revenue/api/revenue.py -> aggregates latest
#     terminal connector-run audit edges into this pure builder input.
# ============================================================================
def _connector_runs_failed_alert(
    month: str,
    count: int,
    failed_by_status: Mapping[str, int] | None,
) -> MonthlySmartAlert | None:
    status_counts = {
        status: status_count
        for status, status_count in sorted((failed_by_status or {}).items())
        if status_count > 0
    }
    statuses_total = sum(status_counts.values())
    if count > 0 and statuses_total > 0:
        effective_count = max(count, statuses_total)
    elif count > 0:
        effective_count = count
    elif statuses_total > 0:
        effective_count = statuses_total
    else:
        effective_count = 0
    if effective_count <= 0:
        return None
    return MonthlySmartAlert(
        code="CONNECTOR_RUNS_FAILED",
        severity="HIGH",
        message=(f"{effective_count} connector run(s) failed or partially failed for {month}."),
        source="connector_job_run",
        confidence="E_MISSING",
        details={
            "failed_run_count": effective_count,
            "failed_by_status": status_counts,
        },
    )


def _payment_not_matched_alert(
    month: str, payment_match: MonthlyPaymentMatchSummary
) -> MonthlySmartAlert | None:
    """AdSense payment not matched for the month."""
    if payment_match.status == "PAYMENT_MATCHED":
        return None
    return MonthlySmartAlert(
        code="PAYMENT_NOT_MATCHED",
        severity="HIGH",
        message=f"AdSense payment is not matched for {month}.",
        source="payment_match",
        confidence="E_MISSING",
        details={
            "payment_match_status": payment_match.status,
            "payment_gap_usd": _decimal_to_api(payment_match.payment_gap_usd),
            "issue_count": len(payment_match.issues),
        },
    )


def _bank_reconciliation_alert(
    month: str, bank_reconciliation: MonthBankReconciliationSummary
) -> MonthlySmartAlert | None:
    """No bank receipt OR non-confirmed bank reconciliation for the month."""
    if bank_reconciliation.status == "MISSING_BANK_RECEIPT":
        return MonthlySmartAlert(
            code="BANK_AMOUNT_MISSING",
            severity="HIGH",
            message=f"No bank receipt is recorded for {month}.",
            source="bank_reconciliation",
            confidence="E_MISSING",
            details={
                "bank_reconciliation_status": bank_reconciliation.status,
                "paid_payment_count": bank_reconciliation.paid_payment_count,
            },
        )
    if bank_reconciliation.status != "BANK_CONFIRMED":
        return MonthlySmartAlert(
            code="BANK_RECONCILIATION_NOT_CONFIRMED",
            severity="HIGH",
            message=f"Bank reconciliation is not confirmed for {month}.",
            source="bank_reconciliation",
            confidence="E_MISSING",
            details={
                "bank_reconciliation_status": bank_reconciliation.status,
                "bank_gap_usd": _decimal_to_api(bank_reconciliation.bank_gap_usd),
            },
        )
    return None


def _unexplained_gap_alert(
    month: str,
    payment_match: MonthlyPaymentMatchSummary,
    bank_reconciliation: MonthBankReconciliationSummary,
    threshold_usd: Decimal,
) -> MonthlySmartAlert | None:
    """Payment or bank gap above threshold for the month."""
    gap_details = _high_gap_details(
        payment_gap_usd=payment_match.payment_gap_usd,
        bank_gap_usd=bank_reconciliation.bank_gap_usd,
        threshold_usd=threshold_usd,
    )
    if not gap_details:
        return None
    return MonthlySmartAlert(
        code="UNEXPLAINED_GAP_HIGH",
        severity="HIGH",
        message=f"One or more finance gaps exceed threshold for {month}.",
        source="reconciliation",
        confidence="E_MISSING",
        details=gap_details,
    )


def _revenue_trend_anomaly_alert(
    month: str,
    current_facts: Iterable[RevenueFactEntry],
    previous_facts: Iterable[RevenueFactEntry],
    threshold_percent: Decimal,
) -> MonthlySmartAlert | None:
    """One or more channels moved month-over-month above threshold."""
    trend_details = _revenue_trend_anomaly_details(
        current_revenue_facts=current_facts,
        previous_revenue_facts=previous_facts,
        threshold_percent=threshold_percent,
    )
    if not trend_details:
        return None
    return MonthlySmartAlert(
        code="REVENUE_TREND_ANOMALY",
        severity="HIGH",
        message=(
            "One or more channels have month-over-month revenue "
            f"movement above threshold for {month}."
        ),
        source="revenue_facts",
        confidence="D_ESTIMATED",
        details=trend_details,
    )


def _month_not_locked_alert(month: str, normalized_close_status: str) -> MonthlySmartAlert | None:
    """Finance month is not yet LOCKED."""
    if normalized_close_status == "LOCKED":
        return None
    return MonthlySmartAlert(
        code="MONTH_NOT_LOCKED",
        severity="MEDIUM",
        message=f"Finance month {month} is not locked.",
        source="finance_close",
        confidence="D_ESTIMATED",
        details={"close_status": normalized_close_status},
    )


def _manual_overrides_alert(
    month: str, overrides: Sequence[RevenueManualOverrideEntry]
) -> MonthlySmartAlert | None:
    """At least one APPROVED manual override affects the month."""
    approved_count = sum(1 for override in overrides if override.status == "APPROVED")
    if approved_count <= 0:
        return None
    return MonthlySmartAlert(
        code="MANUAL_OVERRIDE_USED",
        severity="MEDIUM",
        message=f"{approved_count} approved manual override(s) affect {month}.",
        source="manual_overrides",
        confidence="B_RECONCILED",
        details={"approved_override_count": approved_count},
    )


def _gap_above_threshold(gap_usd: Decimal | None, threshold_usd: Decimal) -> bool:
    """True when the absolute gap is at or above the threshold."""
    return gap_usd is not None and abs(gap_usd) >= threshold_usd


def _high_gap_details(
    *,
    payment_gap_usd: Decimal | None,
    bank_gap_usd: Decimal | None,
    threshold_usd: Decimal,
) -> dict[str, object]:
    """
    Return details of payment and bank gaps that exceed the specified threshold.

    Args:
        payment_gap_usd: Difference between expected and actual payments in USD.
        bank_gap_usd: Difference between expected and actual bank amounts in USD.
        threshold_usd: Minimum gap value to include in the details.

    Raises:
        ValueError: If threshold_usd is negative.

    Returns:
        A dictionary containing gap details if any gaps exceed the threshold,
        otherwise an empty dict.
    """
    if threshold_usd < 0:
        raise ValueError("high_gap_threshold_usd must be non-negative")
    details: dict[str, object] = {"threshold_usd": _decimal_to_api(threshold_usd)}
    if _gap_above_threshold(payment_gap_usd, threshold_usd):
        details["payment_gap_usd"] = _decimal_to_api(payment_gap_usd)
    if _gap_above_threshold(bank_gap_usd, threshold_usd):
        details["bank_gap_usd"] = _decimal_to_api(bank_gap_usd)
    return details if len(details) > 1 else {}


def _revenue_trend_anomaly_details(
    *,
    current_revenue_facts: Iterable[RevenueFactEntry],
    previous_revenue_facts: Iterable[RevenueFactEntry],
    threshold_percent: Decimal,
) -> dict[str, object]:
    """
    Compute details for channels where revenue trend changes exceed a
    percentage threshold.

    Args:
        current_revenue_facts: Iterable of RevenueFactEntry for the current period.
        previous_revenue_facts: Iterable of RevenueFactEntry for the previous period.
        threshold_percent: Decimal percent change threshold to report anomalies.

    Returns:
        A dictionary containing threshold, channel count, and list of
        channel-specific change details,
        or an empty dict if no channels exceed the threshold.
    """
    current_by_channel = _select_primary_facts_by_channel(current_revenue_facts)
    previous_by_channel = _select_primary_facts_by_channel(previous_revenue_facts)
    channels: list[dict[str, object]] = []
    # Iterate previous_by_channel so channels that disappear in the current
    # month are still surfaced as a 100% drop. Skipping them would silently
    # mask one of the highest-signal regressions for revenue trend alerts.
    for channel_id in sorted(previous_by_channel):
        detail = _channel_trend_detail(
            channel_id=channel_id,
            current=current_by_channel.get(channel_id),
            previous=previous_by_channel[channel_id],
            threshold_percent=threshold_percent,
        )
        if detail is not None:
            channels.append(detail)
    if not channels:
        return {}
    return {
        "threshold_percent": _decimal_to_api(_to_percent(threshold_percent)),
        "channel_count": len(channels),
        "channels": channels,
    }


def _channel_trend_detail(
    *,
    channel_id: str,
    current: RevenueFactEntry | None,
    previous: RevenueFactEntry,
    threshold_percent: Decimal,
) -> dict[str, object] | None:
    """Return the trend anomaly detail for one channel, or None when not anomalous."""
    if previous.gross_revenue_usd == 0:
        return None
    current_gross_revenue_usd = current.gross_revenue_usd if current is not None else Decimal("0")
    change_ratio = (
        current_gross_revenue_usd - previous.gross_revenue_usd
    ) / previous.gross_revenue_usd
    if abs(change_ratio) <= threshold_percent:
        return None
    return {
        "youtube_channel_id": channel_id,
        "current_gross_revenue_usd": _decimal_to_api(current_gross_revenue_usd),
        "previous_gross_revenue_usd": _decimal_to_api(previous.gross_revenue_usd),
        "change_percent": _decimal_to_api(_to_percent(change_ratio)),
    }


def _select_primary_facts_by_channel(
    facts: Iterable[RevenueFactEntry],
) -> dict[str, RevenueFactEntry]:
    """
    Select the primary RevenueFactEntry for each channel based on defined source priority.

    Args:
        facts: Iterable of RevenueFactEntry objects to select from.

    Returns:
        A dict mapping channel IDs to the chosen RevenueFactEntry with highest priority.
    """
    selected: dict[str, RevenueFactEntry] = {}
    for fact in sorted(
        facts,
        key=lambda item: (
            item.youtube_channel_id,
            SOURCE_PRIORITY.get(item.source_kind, 99),
            item.source_kind,
            item.source_report_id or "",
            item.id,
        ),
    ):
        selected.setdefault(fact.youtube_channel_id, fact)
    return selected


def _highest_severity(alerts: list[MonthlySmartAlert]) -> str | None:
    """
    Determine the highest severity among a list of MonthlySmartAlert instances.

    Args:
        alerts: List of MonthlySmartAlert objects.

    Returns:
        The severity string of the highest-severity alert, or None if the list is empty.
    """
    if not alerts:
        return None
    return max(alerts, key=lambda alert: _SEVERITY_RANK[alert.severity]).severity


def _to_percent(value: Decimal) -> Decimal:
    """
    Convert a decimal ratio to a percentage with four decimal places, rounding half up.

    Args:
        value: Decimal ratio to convert (e.g., 0.05 for 5%).

    Returns:
        Decimal percentage value quantized to four decimal places.
    """
    return (value * Decimal("100")).quantize(
        Decimal("0.0001"),
        rounding=ROUND_HALF_UP,
    )
