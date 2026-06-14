import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.explanation_models import NumberExplanationORM
from ums_smart_revenue.finance.account_allocation_read import (
    AllocationProvenance,
    allocation_provenance_to_api,
)
from ums_smart_revenue.finance.allocation import AllocationLine
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.deduction_components import DeductionComponent
from ums_smart_revenue.finance.manual_overrides import RevenueManualOverrideEntry
from ums_smart_revenue.finance.net_revenue import (
    build_channel_net_revenue_summary,
    resolve_applicable_channel_deductions,
)
from ums_smart_revenue.finance.reconciliation import SOURCE_PRIORITY
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry
from ums_smart_revenue.finance.revenue_summary import build_adjusted_revenue_summary
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

ADJUSTED_GROSS_REVENUE_METRIC = "adjusted_gross_revenue_usd"
NET_REVENUE_METRIC = "net_revenue_usd"
REVENUE_RECONCILIATION_METRIC = "revenue_reconciliation_usd"
# FIX: ``revenue_reconciliation_usd`` is intentionally NOT a member of
# ``SUPPORTED_METRICS``. The reconciliation explanation is built and persisted
# only by ``build_reconciliation_explanation`` via the dedicated
# ``ReconciliationWorkflowService`` workflow, which enforces the
# finalized-payment + bank-reconciliation + confidence permission gates. Adding
# it here would expose the generic
# ``POST /revenue/channels/{channel_id}/months/{month}/explain?metric=...``
# path to a caller with only VIEW_REVENUE+VIEW_CONFIDENCE@channel, letting them
# overwrite the smart reconciliation row with an adjusted-gross explanation and
# corrupt the persisted explanation returned by the new GET endpoint.
SUPPORTED_METRICS = frozenset({ADJUSTED_GROSS_REVENUE_METRIC, NET_REVENUE_METRIC})
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)

_NET_CONFIDENCE_TO_EXPLAIN: dict[str, dict[str, str]] = {
    "B_RECONCILED": {"label": "HIGH", "score": "0.95"},
    "D_ESTIMATED": {"label": "MEDIUM", "score": "0.80"},
    "E_MISSING": {"label": "LOW", "score": "0"},
}


def map_net_confidence(net_confidence_label: str) -> dict[str, str]:
    """Map a net-revenue confidence label to the explain HIGH/MEDIUM/LOW + score shape."""
    # ========================================================================
    # Purpose: Map a net-revenue confidence label (B_RECONCILED/D_ESTIMATED/
    #   E_MISSING) to the explain HIGH/MEDIUM/LOW+score shape. Net explanations
    #   reject E_MISSING before persistence; the E_MISSING/default rows fail safe.
    # Database/ORM: None.
    # Standards: Pure; total function (unknown labels -> LOW/0).
    # Blast Radius: Finance confidence labels on persisted net explanations.
    # ========================================================================
    return _NET_CONFIDENCE_TO_EXPLAIN.get(net_confidence_label, {"label": "LOW", "score": "0"})


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
        """Convert the explanation entry into an API dictionary."""
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
    """Repository for persisting number explanation entries."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        """Bind number explanation upserts to an explicit or request tenant."""
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def get_explanation(
        self, *, month: str, entity_type: str, entity_id: str, metric: str
    ) -> NumberExplanationEntry | None:
        """Read a persisted explanation for the tenant, or None if absent.

        Tenant-scoped read keyed on the unique
        (tenant_id, month, entity_type, entity_id, metric) identity.
        """
        row = self._session.scalars(
            select(NumberExplanationORM).where(
                NumberExplanationORM.tenant_id == self._tenant_id,
                NumberExplanationORM.month == month,
                NumberExplanationORM.entity_type == entity_type,
                NumberExplanationORM.entity_id == entity_id,
                NumberExplanationORM.metric == metric,
            )
        ).one_or_none()
        if row is None:
            return None
        return NumberExplanationEntry(
            month=row.month,
            entity_type=row.entity_type,
            entity_id=row.entity_id,
            metric=row.metric,
            value=row.value,
            currency=row.currency,
            formula=row.formula,
            confidence=_load_confidence(row.confidence),
            components=list(row.components),
            warnings=list(row.warnings),
        )

    def record_explanation(self, explanation: NumberExplanationEntry) -> NumberExplanationEntry:
        """Record or update a number explanation entry."""
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
        # FIX: Preserve the full confidence dict (label + score + any future keys)
        # on persistence so the GET endpoint can return the operator-visible score
        # that the build path computed. Legacy rows stored only the label string
        # and the reader falls back to a label-only dict for those.
        row.confidence = _dump_confidence(explanation.confidence)
        row.components = explanation.components
        row.warnings = explanation.warnings
        row.updated_at = now
        self._session.flush()
        return explanation

    # ============================================================================
    # Purpose: Prune stale explanations for one month/entity/metric while keeping
    #   the live entity ids from the current recomputation.
    # Database/ORM: number_explanations / NumberExplanationORM.
    # Standards: Tenant-scoped delete; caller owns transaction and recomputation.
    # Blast Radius: Finance explanation rows only. No authorization or Neo4j.
    # Connections:
    #   - File: backend/ums_smart_revenue/finance/reconciliation_service.py ->
    #     Removes channel reconciliation explanations that left result.channels.
    # ============================================================================
    def delete_month_metric_explanations_except(
        self,
        *,
        month: str,
        entity_type: str,
        metric: str,
        keep_entity_ids: set[str],
    ) -> int:
        """Delete stale explanation rows for one metric in a tenant month."""
        statement = delete(NumberExplanationORM).where(
            NumberExplanationORM.tenant_id == self._tenant_id,
            NumberExplanationORM.month == month,
            NumberExplanationORM.entity_type == entity_type,
            NumberExplanationORM.metric == metric,
        )
        if keep_entity_ids:
            statement = statement.where(
                ~NumberExplanationORM.entity_id.in_(sorted(keep_entity_ids))
            )
        result = self._session.execute(statement)
        self._session.flush()
        return int(result.rowcount or 0)


def build_channel_month_revenue_explanation(
    *,
    facts: list[RevenueFactEntry],
    manual_overrides: list[RevenueManualOverrideEntry],
    month: str,
    youtube_channel_id: str,
    metric: str,
    deduction_components: Iterable[DeductionComponent] = (),
    account_allocations: Iterable[AllocationLine] = (),
    account_allocation_provenance: AllocationProvenance | None = None,
) -> NumberExplanationEntry:
    """Build an adjusted-gross-revenue or net-revenue explanation for one channel month."""
    if metric == NET_REVENUE_METRIC:
        return _build_net_revenue_explanation(
            facts=facts,
            manual_overrides=manual_overrides,
            month=month,
            youtube_channel_id=youtube_channel_id,
            deduction_components=deduction_components,
            account_allocations=account_allocations,
            account_allocation_provenance=account_allocation_provenance,
        )
    if metric not in SUPPORTED_METRICS:
        raise NumberExplanationValidationError(f"Unsupported explanation metric: {metric}")

    summary = build_adjusted_revenue_summary(
        facts=facts,
        manual_overrides=manual_overrides,
        month=month,
        youtube_channel_id=youtube_channel_id,
    )
    primary_fact = _primary_fact(facts)
    approved_count = sum(1 for override in manual_overrides if override.status == "APPROVED")
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
                "message": (f"{pending_count} pending manual {subject} not included in {metric}."),
            }
        )
    if primary_fact is None:
        warnings.append(
            {
                "code": "NO_REVENUE_FACTS",
                "message": (f"No revenue facts are available for {youtube_channel_id} in {month}."),
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


def _build_net_revenue_explanation(
    *,
    facts: list[RevenueFactEntry],
    manual_overrides: list[RevenueManualOverrideEntry],
    month: str,
    youtube_channel_id: str,
    deduction_components: Iterable[DeductionComponent],
    account_allocations: Iterable[AllocationLine],
    account_allocation_provenance: AllocationProvenance | None = None,
) -> NumberExplanationEntry:
    """Explain net_revenue_usd for one channel-month with deduction provenance.

    Raises:
        NumberExplanationValidationError: If net revenue is indeterminate (no
            facts or a missing source net) for the given channel-month, so no
            value is fabricated.
    """
    # ========================================================================
    # Purpose: Explain net_revenue_usd for one channel-month, reusing the PR-2
    #   net builder for the value/status/confidence and the shared applicable-
    #   deduction helper for channel-direct + account-allocated provenance.
    #   Indeterminate net (E_MISSING) is rejected (422) -- no fabricated value.
    # Database/ORM: None (operates on loaded read models; persisted by caller).
    # Standards: Typed; raises NumberExplanationValidationError on indeterminate net.
    # Blast Radius: Finance number explanations; net provenance + confidence.
    # ========================================================================
    deduction_components = list(deduction_components)
    account_allocations = list(account_allocations)
    summary = build_channel_net_revenue_summary(
        facts=facts,
        manual_overrides=manual_overrides,
        month=month,
        youtube_channel_id=youtube_channel_id,
        deduction_components=deduction_components,
        account_allocations=account_allocations,
    )
    if summary.net_revenue_usd is None:
        raise NumberExplanationValidationError(
            f"net_revenue_usd is indeterminate for {youtube_channel_id} in {month} "
            f"(status {summary.status}); no net explanation is emitted."
        )
    # Determinate net implies facts present, so _primary_fact is non-None here; it
    # carries source_report_id (the summary exposes only primary_source_kind), which
    # the baseline component must include per spec §5.5 (parity with the gross path).
    primary_fact = _primary_fact(facts)

    components: list[dict[str, object]] = [
        {
            "key": "baseline_gross_revenue_usd",
            "label": "Baseline gross revenue",
            "value": _decimal_to_api(summary.baseline_gross_revenue_usd),
            "source_kind": primary_fact.source_kind,
            "source_report_id": primary_fact.source_report_id,
        },
        {
            "key": "approved_manual_override_total_usd",
            "label": "Approved manual overrides",
            "value": _decimal_to_api(summary.approved_manual_override_total_usd),
            "count": summary.approved_manual_override_count,
        },
    ]

    if summary.status == "COMPONENT_DERIVED":
        channel_direct, account_allocated = resolve_applicable_channel_deductions(
            deduction_components=deduction_components,
            account_allocations=account_allocations,
            month=month,
            youtube_channel_id=youtube_channel_id,
            primary_source_kind=summary.primary_source_kind,
        )
        channel_direct = sorted(
            channel_direct, key=lambda component: (component.source_system, component.component_key)
        )
        account_allocated = sorted(
            account_allocated, key=lambda line: (line.adsense_account_id, line.component_key)
        )
        components.append(
            {
                "key": "channel_direct_deduction_usd",
                "label": "Channel-direct deductions",
                "value": _decimal_to_api(summary.channel_direct_deduction_amount_usd),
                "count": len(channel_direct),
                "components": [
                    {
                        "component_kind": component.component_kind,
                        "source_system": component.source_system,
                        "component_key": component.component_key,
                        "amount_usd": _decimal_to_api(component.amount_usd),
                    }
                    for component in channel_direct
                ],
            }
        )
        account_component = {
            "key": "account_allocated_deduction_usd",
            "label": "Account-allocated deductions",
            "value": _decimal_to_api(summary.account_allocated_deduction_amount_usd),
            "count": len(account_allocated),
            "allocations": [
                {
                    "adsense_account_id": line.adsense_account_id,
                    "component_kind": line.component_kind,
                    "source_system": line.source_system,
                    "component_key": line.component_key,
                    "basis_source_kind": line.basis_source_kind,
                    "basis_share": _decimal_to_api(line.basis_share),
                    "allocated_amount_usd": _decimal_to_api(line.allocated_amount_usd),
                }
                for line in account_allocated
            ],
        }
        if account_allocation_provenance is not None:
            account_component.update(allocation_provenance_to_api(account_allocation_provenance))
        components.append(account_component)
        formula = (
            "net_revenue_usd = adjusted_gross_revenue_usd "
            "- channel_direct_deduction_amount_usd "
            "- account_allocated_deduction_amount_usd"
        )
    else:
        components.append(
            {
                "key": "source_reported_deduction_usd",
                "label": "Source-reported deductions",
                "value": _decimal_to_api(summary.deduction_amount_usd),
                "source_kind": summary.primary_source_kind,
            }
        )
        # FIX: the source-net value is adjusted_net = source-reported net +
        # approved_manual_override_total_usd, so a literal "net = source-reported
        # net" formula no longer reconciles to the persisted value once an approved
        # override exists. State the value purely in terms of emitted components:
        # baseline_gross + approved_override - source_reported_deduction == adjusted_net.
        formula = (
            "net_revenue_usd = baseline_gross_revenue_usd "
            "+ approved_manual_override_total_usd "
            "- source_reported_deduction_usd"
        )

    warnings: list[dict[str, object]] = [
        {"code": issue.get("issue_type", "ISSUE"), "message": issue.get("message", "")}
        for issue in summary.issues
    ]
    if summary.pending_manual_override_count:
        warnings.append(
            {
                "code": "PENDING_MANUAL_OVERRIDES",
                "message": (
                    f"{summary.pending_manual_override_count} pending manual override(s) "
                    f"not included in {NET_REVENUE_METRIC}."
                ),
            }
        )

    return NumberExplanationEntry(
        month=month,
        entity_type="channel",
        entity_id=youtube_channel_id,
        metric=NET_REVENUE_METRIC,
        value=summary.net_revenue_usd,
        currency="USD",
        formula=formula,
        confidence=map_net_confidence(summary.confidence),
        components=components,
        warnings=warnings,
    )


def _primary_fact(facts: list[RevenueFactEntry]) -> RevenueFactEntry | None:
    """Select the highest-priority revenue fact, if one exists."""
    if not facts:
        return None
    return sorted(
        facts,
        key=lambda fact: (SOURCE_PRIORITY.get(fact.source_kind, 99), fact.source_kind),
    )[0]


def _confidence(
    primary_fact: RevenueFactEntry | None, warnings: list[dict[str, object]]
) -> dict[str, object]:
    """Compute confidence metadata for a revenue fact."""
    score = primary_fact.confidence_score if primary_fact else Decimal("0")
    if warnings and score > Decimal("0.9000"):
        score = Decimal("0.9000")
    label = (
        "HIGH" if score >= Decimal("0.9000") else "MEDIUM" if score >= Decimal("0.7000") else "LOW"
    )
    return {
        "label": label,
        "score": _decimal_to_api(score),
    }


def _dump_confidence(confidence: dict[str, object]) -> str:
    """Serialize a confidence dict to the persistence column (JSON text).

    The ``number_explanations.confidence`` column is TEXT (see
    ``explanation_models.NumberExplanationORM``), so the dict is encoded as
    JSON. ``sort_keys`` keeps the on-disk representation deterministic for
    golden tests and debugging.
    """
    return json.dumps(dict(confidence), sort_keys=True, default=str)


def _load_confidence(stored: str) -> dict[str, object]:
    """Reconstruct the confidence dict from the persistence column.

    Backward compatible with rows persisted before the score was kept: those
    rows contain only the label string, so any non-dict payload falls back to
    ``{"label": <stored>}`` instead of crashing the GET path.
    """
    if not stored:
        return {"label": "LOW", "score": "0"}
    try:
        parsed = json.loads(stored)
    except (TypeError, ValueError):
        return {"label": str(stored), "score": "0"}
    if isinstance(parsed, dict):
        return parsed
    return {"label": str(parsed), "score": "0"}


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
        raise NumberExplanationValidationError("tenant_id must be a valid UUID") from exc
