from collections.abc import Iterable, Sequence
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


def build_monthly_smart_alert_summary(
    *,
    month: str,
    payment_match: MonthlyPaymentMatchSummary,
    bank_reconciliation: MonthBankReconciliationSummary,
    close_status: str | None,
    manual_overrides: Iterable[RevenueManualOverrideEntry],
    missing_revenue_fact_channel_count: int = 0,
    missing_revenue_fact_channel_sample: Sequence[str] = (),
    current_revenue_facts: Iterable[RevenueFactEntry] = (),
    previous_revenue_facts: Iterable[RevenueFactEntry] = (),
    high_gap_threshold_usd: Decimal = DEFAULT_HIGH_GAP_THRESHOLD_USD,
    revenue_trend_anomaly_threshold_percent: Decimal = (
        DEFAULT_REVENUE_TREND_ANOMALY_THRESHOLD_PERCENT
    ),
) -> MonthlySmartAlertSummary:
    """Build a monthly smart alert summary from finance signal inputs.

    The coverage alert takes a (count, sample) pair instead of a full id
    list so the route can bound the read at the SQL LIMIT clause. The
    alert details keep the same wire shape (channel_count + sample_channel_ids).
    """
    if high_gap_threshold_usd < 0:
        raise ValueError("high_gap_threshold_usd must be non-negative")
    if revenue_trend_anomaly_threshold_percent < 0:
        raise ValueError(
            "revenue_trend_anomaly_threshold_percent must be non-negative"
        )
    if missing_revenue_fact_channel_count < 0:
        raise ValueError("missing_revenue_fact_channel_count must be non-negative")
    alerts: list[MonthlySmartAlert] = []
    normalized_close_status = close_status or "OPEN"
    overrides = list(manual_overrides)

    if payment_match.status == "NO_YOUTUBE_REVENUE":
        alerts.append(
            MonthlySmartAlert(
                code="MISSING_REVENUE_SOURCE",
                severity="HIGH",
                message=f"No YouTube revenue source is available for {month}.",
                source="payment_match",
                confidence="E_MISSING",
                details={"payment_match_status": payment_match.status},
            )
        )

    # ========================================================================
    # Purpose: Emit a per-channel coverage gap alert when active,
    #   revenue-required channels have no revenue fact for the month. Distinct
    #   from the month-level MISSING_REVENUE_SOURCE (zero-YouTube-revenue);
    #   mirrors the close-readiness MISSING_REVENUE_FACTS blocker.
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
    sample = sorted(missing_revenue_fact_channel_sample)[:MISSING_FACT_CHANNEL_SAMPLE_LIMIT]
    if missing_revenue_fact_channel_count > 0 or sample:
        alerts.append(
            MonthlySmartAlert(
                code="CHANNELS_MISSING_REVENUE_FACTS",
                severity="HIGH",
                message=(
                    f"{missing_revenue_fact_channel_count} active revenue-required "
                    f"channel(s) have no revenue facts for {month}."
                ),
                source="revenue_facts",
                confidence="E_MISSING",
                details={
                    "channel_count": missing_revenue_fact_channel_count,
                    "sample_channel_ids": sample,
                },
            )
        )

    if payment_match.status != "PAYMENT_MATCHED":
        alerts.append(
            MonthlySmartAlert(
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
        )

    if bank_reconciliation.status == "MISSING_BANK_RECEIPT":
        alerts.append(
            MonthlySmartAlert(
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
        )
    elif bank_reconciliation.status != "BANK_CONFIRMED":
        alerts.append(
            MonthlySmartAlert(
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
        )

    gap_details = _high_gap_details(
        payment_gap_usd=payment_match.payment_gap_usd,
        bank_gap_usd=bank_reconciliation.bank_gap_usd,
        threshold_usd=high_gap_threshold_usd,
    )
    if gap_details:
        alerts.append(
            MonthlySmartAlert(
                code="UNEXPLAINED_GAP_HIGH",
                severity="HIGH",
                message=f"One or more finance gaps exceed threshold for {month}.",
                source="reconciliation",
                confidence="E_MISSING",
                details=gap_details,
            )
        )

    trend_details = _revenue_trend_anomaly_details(
        current_revenue_facts=current_revenue_facts,
        previous_revenue_facts=previous_revenue_facts,
        threshold_percent=revenue_trend_anomaly_threshold_percent,
    )
    if trend_details:
        alerts.append(
            MonthlySmartAlert(
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
        )

    if normalized_close_status != "LOCKED":
        alerts.append(
            MonthlySmartAlert(
                code="MONTH_NOT_LOCKED",
                severity="MEDIUM",
                message=f"Finance month {month} is not locked.",
                source="finance_close",
                confidence="D_ESTIMATED",
                details={"close_status": normalized_close_status},
            )
        )

    approved_count = sum(1 for override in overrides if override.status == "APPROVED")
    if approved_count:
        alerts.append(
            MonthlySmartAlert(
                code="MANUAL_OVERRIDE_USED",
                severity="MEDIUM",
                message=f"{approved_count} approved manual override(s) affect {month}.",
                source="manual_overrides",
                confidence="B_RECONCILED",
                details={"approved_override_count": approved_count},
            )
        )

    highest_severity = _highest_severity(alerts)
    return MonthlySmartAlertSummary(
        month=month,
        status="ATTENTION_REQUIRED" if alerts else "CLEAR",
        highest_severity=highest_severity,
        alerts=alerts,
    )


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
        A dictionary with gap details if any gaps exceed the threshold,
        otherwise an empty dict.
    """
    if threshold_usd < 0:
        raise ValueError("high_gap_threshold_usd must be non-negative")
    details: dict[str, object] = {"threshold_usd": _decimal_to_api(threshold_usd)}
    if payment_gap_usd is not None and abs(payment_gap_usd) >= threshold_usd:
        details["payment_gap_usd"] = _decimal_to_api(payment_gap_usd)
    if bank_gap_usd is not None and abs(bank_gap_usd) >= threshold_usd:
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
        current = current_by_channel.get(channel_id)
        previous = previous_by_channel[channel_id]
        if previous.gross_revenue_usd == 0:
            continue
        current_gross_revenue_usd = (
            current.gross_revenue_usd if current is not None else Decimal("0")
        )
        change_ratio = (
            current_gross_revenue_usd - previous.gross_revenue_usd
        ) / previous.gross_revenue_usd
        if abs(change_ratio) <= threshold_percent:
            continue
        channels.append(
            {
                "youtube_channel_id": channel_id,
                "current_gross_revenue_usd": _decimal_to_api(
                    current_gross_revenue_usd
                ),
                "previous_gross_revenue_usd": _decimal_to_api(
                    previous.gross_revenue_usd
                ),
                "change_percent": _decimal_to_api(_to_percent(change_ratio)),
            }
        )
    if not channels:
        return {}
    return {
        "threshold_percent": _decimal_to_api(_to_percent(threshold_percent)),
        "channel_count": len(channels),
        "channels": channels,
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
