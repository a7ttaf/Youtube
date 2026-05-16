from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ums_smart_revenue.finance.bank_reconciliation import (
    MonthBankReconciliationSummary,
)
from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.payment_matching import MonthlyPaymentMatchSummary
from ums_smart_revenue.finance.reconciliation import SOURCE_PRIORITY
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry

DEFAULT_HIGH_GAP_THRESHOLD_USD = Decimal("100.00")
DEFAULT_REVENUE_TREND_ANOMALY_THRESHOLD_PERCENT = Decimal("0.50")
_SEVERITY_RANK = {"LOW": 1, "MEDIUM": 2, "HIGH": 3}


@dataclass(frozen=True)
class MonthlySmartAlert:
    code: str
    severity: str
    message: str
    source: str
    confidence: str
    details: dict[str, object]

    def to_api(self) -> dict[str, object]:
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
    month: str
    status: str
    highest_severity: str | None
    alerts: list[MonthlySmartAlert]

    def to_api(self) -> dict[str, object]:
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
    current_revenue_facts: Iterable[RevenueFactEntry] = (),
    previous_revenue_facts: Iterable[RevenueFactEntry] = (),
    high_gap_threshold_usd: Decimal = DEFAULT_HIGH_GAP_THRESHOLD_USD,
    revenue_trend_anomaly_threshold_percent: Decimal = (
        DEFAULT_REVENUE_TREND_ANOMALY_THRESHOLD_PERCENT
    ),
) -> MonthlySmartAlertSummary:
    if high_gap_threshold_usd < 0:
        raise ValueError("high_gap_threshold_usd must be non-negative")
    if revenue_trend_anomaly_threshold_percent < 0:
        raise ValueError(
            "revenue_trend_anomaly_threshold_percent must be non-negative"
        )
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
                    "payment_gap_usd": _decimal_to_api(
                        payment_match.payment_gap_usd
                    ),
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
                    "bank_gap_usd": _decimal_to_api(
                        bank_reconciliation.bank_gap_usd
                    ),
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
    if not alerts:
        return None
    return max(alerts, key=lambda alert: _SEVERITY_RANK[alert.severity]).severity


def _to_percent(value: Decimal) -> Decimal:
    return (value * Decimal("100")).quantize(
        Decimal("0.0001"),
        rounding=ROUND_HALF_UP,
    )


def _decimal_to_api(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")
