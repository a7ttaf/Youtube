from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from ums_smart_revenue.finance.bank_reconciliation import (
    MonthBankReconciliationSummary,
)
from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.payment_matching import MonthlyPaymentMatchSummary

DEFAULT_HIGH_GAP_THRESHOLD_USD = Decimal("100.00")
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
    high_gap_threshold_usd: Decimal = DEFAULT_HIGH_GAP_THRESHOLD_USD,
) -> MonthlySmartAlertSummary:
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
    details: dict[str, object] = {"threshold_usd": _decimal_to_api(threshold_usd)}
    if payment_gap_usd is not None and abs(payment_gap_usd) >= threshold_usd:
        details["payment_gap_usd"] = _decimal_to_api(payment_gap_usd)
    if bank_gap_usd is not None and abs(bank_gap_usd) >= threshold_usd:
        details["bank_gap_usd"] = _decimal_to_api(bank_gap_usd)
    return details if len(details) > 1 else {}


def _highest_severity(alerts: list[MonthlySmartAlert]) -> str | None:
    if not alerts:
        return None
    return max(alerts, key=lambda alert: _SEVERITY_RANK[alert.severity]).severity


def _decimal_to_api(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")
