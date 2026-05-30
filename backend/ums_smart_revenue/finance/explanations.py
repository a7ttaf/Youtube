from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.explanation_models import NumberExplanationORM
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.reconciliation import SOURCE_PRIORITY
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry
from ums_smart_revenue.finance.revenue_summary import build_adjusted_revenue_summary
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

ADJUSTED_GROSS_REVENUE_METRIC = "adjusted_gross_revenue_usd"
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)


@dataclass(frozen=True)
class NumberExplanationEntry:
    """Explains a monthly metric value, formula, components, and warnings."""

    month: str
    entity_type: str
    entity_id: str
    metric: str
    value: Decimal
    currency: str
    formula: str
    confidence: dict[str, object]
    components: list[dict[str, object]]
    warnings: list[dict[str, object]]

    def to_api(self) -> dict[str, object]:
        """
        Convert the instance attributes into a dictionary formatted for API
        consumption.

        Returns:
            dict[str, object]: A mapping of the object's data ready for API usage.
        """
        return {
            "month": self.month,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "metric": self.metric,
            "value": _decimal_to_api(self.value),
            "currency": self.currency,
            "formula": self.formula,
            "confidence": self.confidence,
            "components": self.components,
            "warnings": self.warnings,
        }


class NumberExplanationError(ValueError):
    """Base exception for errors related to number explanations."""


class NumberExplanationValidationError(NumberExplanationError):
    """Exception raised when a NumberExplanationEntry fails validation checks."""

# This module provides functionality to build and record detailed
# revenue explanations for YouTube channels, using SQLAlchemy for
# persistence and domain models for revenue facts and overrides.


class SqlAlchemyNumberExplanationRepository:
    """Repository for persisting and retrieving NumberExplanationEntry objects
    using SQLAlchemy.
    """

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind number explanation upserts to an explicit or request tenant."""
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def record_explanation(
        self, explanation: NumberExplanationEntry
    ) -> NumberExplanationEntry:
        """Record or update a NumberExplanationEntry in the database and
        return the provided explanation entry."""
        row = self._session.scalars(
            select(NumberExplanationORM).where(
                NumberExplanationORM.tenant_id == self._tenant_id,
                NumberExplanationORM.month == explanation.month,
                NumberExplanationORM.entity_type == explanation.entity_type,
                NumberExplanationORM.entity_id == explanation.entity_id,
                NumberExplanationORM.metric == explanation.metric,
            )
        ).one_or_none()
        now = datetime.now(UTC)
        if row is None:
            row = NumberExplanationORM(
                id=uuid4(),
                tenant_id=self._tenant_id,
                month=explanation.month,
                entity_type=explanation.entity_type,
                entity_id=explanation.entity_id,
                metric=explanation.metric,
                created_at=now,
            )
            self._session.add(row)

        row.value = explanation.value
        row.currency = explanation.currency
        row.formula = explanation.formula
        row.confidence = str(explanation.confidence["label"])
        row.components = explanation.components
        row.warnings = explanation.warnings
        row.updated_at = now
        self._session.flush()
        return explanation


def build_channel_month_revenue_explanation(
    *,
    facts: list[RevenueFactEntry],
    manual_overrides: list[RevenueManualOverrideEntry],
    month: str,
    youtube_channel_id: str,
    metric: str,
) -> NumberExplanationEntry:
    """Build a detailed revenue explanation for a YouTube channel for a specific month by combining
    revenue facts and manual overrides, validating that the metric is adjusted gross revenue.
    """
    if metric != ADJUSTED_GROSS_REVENUE_METRIC:
        raise NumberExplanationValidationError(
            f"Unsupported explanation metric: {metric}"
        )

    summary = build_adjusted_revenue_summary(
        facts=facts,
        manual_overrides=manual_overrides,
        month=month,
        youtube_channel_id=youtube_channel_id,
    )
    primary_fact = _primary_fact(facts)
    approved_count = sum(
        1 for override in manual_overrides if override.status == "APPROVED"
    )
    pending_count = summary.pending_manual_override_count

    components: list[dict[str, object]] = [
        {
            "key": "baseline_gross_revenue_usd",
            "label": "Baseline gross revenue",
            "value": _decimal_to_api(summary.baseline_gross_revenue_usd),
            "source_kind": primary_fact.source_kind if primary_fact else None,
            "source_report_id": primary_fact.source_report_id if primary_fact else None,
        },
        {
            "key": "approved_manual_override_total_usd",
            "label": "Approved manual overrides",
            "value": _decimal_to_api(summary.approved_manual_override_total_usd),
            "count": approved_count,
        },
    ]
    warnings: list[dict[str, object]] = []
    if pending_count:
        subject = "override is" if pending_count == 1 else "overrides are"
        warnings.append(
            {
                "code": "PENDING_MANUAL_OVERRIDES",
                "message": (
                    f"{pending_count} pending manual {subject} "
                    f"not included in {metric}."
                ),
            }
        )
    if primary_fact is None:
        warnings.append(
            {
                "code": "NO_REVENUE_FACTS",
                "message": (
                    f"No revenue facts are available for {youtube_channel_id} "
                    f"in {month}."
                ),
            }
        )

    return NumberExplanationEntry(
        month=month,
        entity_type="channel",
        entity_id=youtube_channel_id,
        metric=metric,
        value=summary.adjusted_gross_revenue_usd,
        currency="USD",
        formula="baseline_gross_revenue_usd + approved_manual_override_total_usd",
        confidence=_confidence(primary_fact, warnings),
        components=components,
        warnings=warnings,
    )


def _primary_fact(facts: list[RevenueFactEntry]) -> RevenueFactEntry | None:
    """
    Select the primary revenue fact from a list.

    Sorts the provided list of RevenueFactEntry objects by their source priority
    and returns the first item.
    Returns None if the list is empty.
    """
    if not facts:
        return None
    return sorted(
        facts,
        key=lambda fact: (SOURCE_PRIORITY.get(fact.source_kind, 99), fact.source_kind),
    )[0]


def _confidence(
    primary_fact: RevenueFactEntry | None, warnings: list[dict[str, object]]
) -> dict[str, object]:
    """
    Compute confidence metrics for a revenue fact.

    Calculates the confidence score for the given primary_fact, applies a
    maximum cap if warnings are present, determines a confidence label (HIGH,
    MEDIUM, or LOW), and returns the formatted result.
    """
    score = primary_fact.confidence_score if primary_fact else Decimal("0")
    if warnings and score > Decimal("0.9000"):
        score = Decimal("0.9000")
    label = (
        "HIGH"
        if score >= Decimal("0.9000")
        else "MEDIUM"
        if score >= Decimal("0.7000")
        else "LOW"
    )
    return {
        "label": label,
        "score": _decimal_to_api(score),
    }


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    """Resolve tenant id from explicit param, request context, or bootstrap."""
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    """Normalize tenant constructor input into a UUID object."""
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise NumberExplanationValidationError(
            "tenant_id must be a valid UUID"
        ) from exc
