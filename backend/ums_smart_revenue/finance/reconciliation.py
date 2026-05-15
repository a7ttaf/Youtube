from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry

DEFAULT_VARIANCE_TOLERANCE_PERCENT = Decimal("0.02")
SOURCE_PRIORITY = {
    "YOUTUBE_CMS": 0,
    "ADSENSE": 1,
    "YOUTUBE_ANALYTICS": 2,
    "MANUAL_UPLOAD": 3,
    "ALLOCATION": 4,
}


@dataclass(frozen=True)
class ReconciliationIssue:
    issue_type: str
    severity: str
    message: str

    def to_api(self) -> dict[str, str]:
        return {
            "issue_type": self.issue_type,
            "severity": self.severity,
            "message": self.message,
        }


@dataclass(frozen=True)
class RevenueReconciliationPreview:
    month: str
    youtube_channel_id: str
    status: str
    primary_source_kind: str | None
    primary_gross_revenue_usd: Decimal | None
    compared_source_count: int
    gross_revenue_variance_usd: Decimal
    gross_revenue_variance_percent: Decimal
    confidence_score: Decimal
    issues: list[ReconciliationIssue]

    def to_api(self) -> dict[str, object]:
        return {
            "month": self.month,
            "youtube_channel_id": self.youtube_channel_id,
            "status": self.status,
            "primary_source_kind": self.primary_source_kind,
            "primary_gross_revenue_usd": _decimal_to_api(self.primary_gross_revenue_usd),
            "compared_source_count": self.compared_source_count,
            "gross_revenue_variance_usd": _decimal_to_api(self.gross_revenue_variance_usd),
            "gross_revenue_variance_percent": _decimal_to_api(self.gross_revenue_variance_percent),
            "confidence_score": _decimal_to_api(self.confidence_score),
            "issues": [issue.to_api() for issue in self.issues],
        }


@dataclass(frozen=True)
class RevenueReconciliationIssueQueue:
    month: str
    items: list[RevenueReconciliationPreview]

    def to_api(self) -> dict[str, object]:
        return {
            "month": self.month,
            "issue_count": len(self.items),
            "items": [item.to_api() for item in self.items],
        }


def build_revenue_reconciliation_preview(
    facts: Iterable[RevenueFactEntry],
    *,
    month: str | None = None,
    youtube_channel_id: str | None = None,
    variance_tolerance_percent: Decimal = DEFAULT_VARIANCE_TOLERANCE_PERCENT,
) -> RevenueReconciliationPreview:
    sorted_facts = sorted(facts, key=lambda fact: (SOURCE_PRIORITY.get(fact.source_kind, 99), fact.source_kind))
    if not sorted_facts:
        if month is None or youtube_channel_id is None:
            raise ValueError("month and youtube_channel_id are required when no revenue facts are provided")
        return RevenueReconciliationPreview(
            month=month,
            youtube_channel_id=youtube_channel_id,
            status="NO_FACTS",
            primary_source_kind=None,
            primary_gross_revenue_usd=None,
            compared_source_count=0,
            gross_revenue_variance_usd=Decimal("0.0000"),
            gross_revenue_variance_percent=Decimal("0.0000"),
            confidence_score=Decimal("0.0000"),
            issues=[
                ReconciliationIssue(
                    issue_type="NO_REVENUE_FACTS",
                    severity="HIGH",
                    message=f"No revenue facts are available for {youtube_channel_id} in {month}.",
                )
            ],
        )

    primary = sorted_facts[0]
    revenue_values = [fact.gross_revenue_usd for fact in sorted_facts]
    variance_usd = (max(revenue_values) - min(revenue_values)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    variance_percent = _safe_percent(variance_usd, primary.gross_revenue_usd)

    issues: list[ReconciliationIssue] = []
    if len(sorted_facts) == 1:
        status = "INSUFFICIENT_SOURCES"
        confidence_score = (primary.confidence_score * Decimal("0.5")).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        issues.append(
            ReconciliationIssue(
                issue_type="INSUFFICIENT_SOURCES",
                severity="MEDIUM",
                message=f"Only one revenue source is available for {primary.youtube_channel_id} in {primary.month}.",
            )
        )
    elif variance_percent > variance_tolerance_percent:
        status = "VARIANCE_DETECTED"
        severity = "HIGH" if variance_percent >= Decimal("0.05") else "MEDIUM"
        confidence_score = (_average_confidence(sorted_facts) - variance_percent).quantize(
            Decimal("0.0001"),
            rounding=ROUND_HALF_UP,
        )
        issues.append(
            ReconciliationIssue(
                issue_type="GROSS_REVENUE_VARIANCE",
                severity=severity,
                message=(
                    f"Gross revenue sources differ by {_percent_message(variance_percent)} "
                    f"for {primary.youtube_channel_id} in {primary.month}."
                ),
            )
        )
    else:
        status = "RECONCILED"
        confidence_score = (_average_confidence(sorted_facts) - variance_percent).quantize(
            Decimal("0.0001"),
            rounding=ROUND_HALF_UP,
        )

    confidence_score = min(Decimal("1.0000"), max(Decimal("0.0000"), confidence_score))
    return RevenueReconciliationPreview(
        month=primary.month,
        youtube_channel_id=primary.youtube_channel_id,
        status=status,
        primary_source_kind=primary.source_kind,
        primary_gross_revenue_usd=primary.gross_revenue_usd,
        compared_source_count=len(sorted_facts),
        gross_revenue_variance_usd=variance_usd,
        gross_revenue_variance_percent=variance_percent,
        confidence_score=confidence_score,
        issues=issues,
    )


def build_revenue_reconciliation_issue_queue(
    facts: Iterable[RevenueFactEntry],
    *,
    month: str,
) -> RevenueReconciliationIssueQueue:
    grouped: dict[str, list[RevenueFactEntry]] = {}
    for fact in facts:
        if fact.month == month:
            grouped.setdefault(fact.youtube_channel_id, []).append(fact)

    items = [
        preview
        for preview in (
            build_revenue_reconciliation_preview(grouped[channel_id])
            for channel_id in sorted(grouped)
        )
        if preview.issues
    ]
    return RevenueReconciliationIssueQueue(month=month, items=items)


def _average_confidence(facts: list[RevenueFactEntry]) -> Decimal:
    return (sum((fact.confidence_score for fact in facts), Decimal("0")) / Decimal(len(facts))).quantize(
        Decimal("0.0001"),
        rounding=ROUND_HALF_UP,
    )


def _safe_percent(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator == 0:
        # With a zero baseline the ratio is undefined. If the numerator is also
        # zero there is genuinely no variance to flag. If it is non-zero we
        # have positive variance against a zero baseline (a primary source
        # reporting $0 while another source reports a positive value) — return
        # 1.0 = 100% so the reconciliation path flags the channel as a high
        # variance instead of falling through to RECONCILED with 0%.
        return Decimal("0.0000") if numerator == 0 else Decimal("1.0000")
    return (numerator / denominator).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _percent_message(value: Decimal) -> str:
    return f"{(value * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"


def _decimal_to_api(value: Decimal | None) -> str | None:
    if value is None:
        return None
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")
