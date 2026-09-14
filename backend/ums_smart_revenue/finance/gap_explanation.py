# ============================================================================
# Purpose: The month gap-explanation builder (Hard Problem #3) — compose the
#   two existing month gaps into one explained story: each leg of the chain
#   youtube_facts -> adsense_paid -> bank_received is decomposed as
#   gap = sum(evidence-backed components) + unexplained residual, with
#   per-number provenance, explain-shape confidence, and deterministic prose.
# Database/ORM: None. Pure composition over MonthlyPaymentMatchSummary,
#   MonthBankReconciliationSummary, and the month's AdSensePaymentEntry rows
#   the route already fetches for those builders (the smart-alerts idiom:
#   one fetch feeds every builder). No new tables, no migration.
# Standards: MONTH-GRAIN ONLY — the PAYMENT-grain bridge is repo-proven
#   absent (Docs/superpowers/specs/2026-06-03) and nothing here attributes a
#   receipt to an account or channel. NO FX CONVERSION anywhere on this path
#   (Docs/18 rules 1-4): non-USD payment rows are counted and warned about,
#   never converted; the only FX number is the operator-entered signed
#   fx_difference_usd evidence summed by the bank summary. Deterministic
#   prose, no LLM — the reconciliation_explanation.py idiom. Confidence uses
#   the finance-adopted explain wire shape {label, score} with the
#   _NET_CONFIDENCE_TO_EXPLAIN constants (HIGH 0.95 / MEDIUM 0.80 / LOW 0).
# Blast Radius: A new read-only finance API surface; no writes, no schema,
#   no change to the payment-match or bank-reconciliation payloads it
#   composes.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py -> GET
#     /revenue/months/{month}/gap-explanation serves this builder.
#   - File: backend/ums_smart_revenue/finance/payment_matching.py -> the
#     payment-leg operands and source status.
#   - File: backend/ums_smart_revenue/finance/bank_reconciliation.py -> the
#     bank-leg operands, fee/FX evidence sums, and the provenance idiom this
#     module extends to the payment leg.
#   - File: Docs/superpowers/plans/2026-08-23-payment-gap-narrative.md -> the
#     ruled design this implements.
# ============================================================================
"""Compose the month's payment and bank gaps into one explained narrative."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_UP, Decimal

from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry
from ums_smart_revenue.finance.bank_reconciliation import MonthBankReconciliationSummary
from ums_smart_revenue.finance.decimal_formatting import decimal_to_api as _decimal_to_api
from ums_smart_revenue.finance.payment_matching import MonthlyPaymentMatchSummary

DEFAULT_GAP_EXPLANATION_TOLERANCE_USD = Decimal("0.01")

# Worst-of ordering for the month status: INCOMPLETE is worst because finance
# cannot even see the whole chain; MATCHED is best because there is nothing
# left to explain.
_STATUS_ORDER = (
    "MATCHED",
    "FULLY_EXPLAINED",
    "PARTIALLY_EXPLAINED",
    "UNEXPLAINED",
    "INCOMPLETE",
)

# The finance-adopted explain confidence constants — the exact literals of
# explanations._NET_CONFIDENCE_TO_EXPLAIN, so the two surfaces cannot drift
# on the numbers a badge renders.
_CONFIDENCE_HIGH = ("HIGH", "0.95")
_CONFIDENCE_MEDIUM = ("MEDIUM", "0.80")
_CONFIDENCE_LOW = ("LOW", "0")

# AdSense payment statuses treated as money still in flight for the payment
# leg's evidence component. CANCELLED is deliberately excluded — a cancelled
# payment is not money in flight — and surfaces as a warning count instead.
_IN_FLIGHT_PAYMENT_STATUSES = frozenset({"PENDING", "UNPAID"})


@dataclass(frozen=True)
class GapExplanationComponent:
    """One evidence-backed slice of a leg's gap.

    ``amount_usd`` keeps the evidence's own sign (the FX difference is
    signed); ``evidence_count`` is the number of source rows behind it.
    """

    key: str
    label: str
    amount_usd: Decimal
    evidence_count: int
    confidence_label: str
    confidence_score: str

    def to_api(self) -> dict[str, object]:
        """Serialize the component with the explain-shape confidence."""
        return {
            "key": self.key,
            "label": self.label,
            "amount_usd": _decimal_to_api(self.amount_usd),
            "evidence_count": self.evidence_count,
            "confidence": {
                "label": self.confidence_label,
                "score": self.confidence_score,
            },
        }


@dataclass(frozen=True)
class GapExplanationLeg:
    """One leg of the chain, decomposed.

    ``operands`` are the leg's two sides in chain order, keyed by their API
    field names; ``gap_field`` / ``source_status_field`` carry the leg's
    field naming so ``to_api`` emits the exact wire shape the plan fixes.
    """

    key: str
    status: str
    operands: tuple[tuple[str, Decimal], ...]
    gap_field: str
    gap_usd: Decimal | None
    source_status_field: str
    source_status: str
    components: tuple[GapExplanationComponent, ...]
    unexplained_residual_usd: Decimal | None
    residual_confidence_label: str
    residual_confidence_score: str
    narrative: str

    def to_api(self) -> dict[str, object]:
        """Serialize the leg in the plan's field order."""
        payload: dict[str, object] = {"status": self.status}
        for field_name, value in self.operands:
            payload[field_name] = _decimal_to_api(value)
        payload[self.gap_field] = _decimal_to_api(self.gap_usd)
        payload[self.source_status_field] = self.source_status
        payload["components"] = [component.to_api() for component in self.components]
        payload["unexplained_residual_usd"] = _decimal_to_api(self.unexplained_residual_usd)
        payload["unexplained_residual_confidence"] = {
            "label": self.residual_confidence_label,
            "score": self.residual_confidence_score,
        }
        payload["narrative"] = self.narrative
        return payload


@dataclass(frozen=True)
class GapExplanationWarning:
    """One non-blocking data caveat attached to the explanation."""

    code: str
    message: str

    def to_api(self) -> dict[str, object]:
        """Serialize the warning."""
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True)
class MonthGapExplanation:
    """The composed month explanation: two decomposed legs plus prose."""

    month: str
    currency: str
    close_status: str
    status: str
    tolerance_usd: Decimal
    payment_leg: GapExplanationLeg
    bank_leg: GapExplanationLeg
    warnings: tuple[GapExplanationWarning, ...]
    narrative: str
    # Built by the builder, where the source COUNTS live: an operand backed
    # by zero source rows must say MISSING_SOURCE, which to_api alone could
    # not know.
    money_provenance: dict[str, dict[str, str | None]]

    def to_api(self) -> dict[str, object]:
        """Serialize the explanation, provenance map included."""
        return {
            "month": self.month,
            "currency": self.currency,
            "close_status": self.close_status,
            "status": self.status,
            "tolerance_usd": _decimal_to_api(self.tolerance_usd),
            "payment_leg": self.payment_leg.to_api(),
            "bank_leg": self.bank_leg.to_api(),
            "warnings": [warning.to_api() for warning in self.warnings],
            "money_provenance": self.money_provenance,
            "narrative": self.narrative,
        }


# ============================================================================
# Purpose: Build the composed month gap explanation from the two existing
#   summaries plus the month's raw payment rows (for the in-flight amount the
#   summaries only count).
# Database/ORM: None (pure function).
# Standards: Leg decomposition is gap = sum(components) + residual with the
#   evidence's own signs; statuses are MATCHED / FULLY_EXPLAINED /
#   PARTIALLY_EXPLAINED / UNEXPLAINED / INCOMPLETE per leg and worst-of for
#   the month. The summaries are trusted as already normalized (currency and
#   month filtering happened in their builders); the raw payments are
#   re-filtered here only for the in-flight sum, with the same month,
#   currency, and status semantics the payment-match builder applies.
# Blast Radius: Read-only composition; wrong numbers here mislead finance but
#   write nothing.
# Connections:
#   - File: backend/ums_smart_revenue/finance/payment_matching.py -> the
#     filtering semantics the in-flight sum mirrors.
# ============================================================================
def build_month_gap_explanation(
    *,
    month: str,
    payment_summary: MonthlyPaymentMatchSummary,
    bank_summary: MonthBankReconciliationSummary,
    payments: Iterable[AdSensePaymentEntry],
    close_status: str,
    tolerance_usd: Decimal = DEFAULT_GAP_EXPLANATION_TOLERANCE_USD,
) -> MonthGapExplanation:
    """Compose both month gaps into one explained, provenance-carrying story."""
    month_payments = [payment for payment in payments if payment.month == month]
    currency = payment_summary.currency
    in_flight_payments = [
        payment
        for payment in month_payments
        if payment.payment_currency == currency
        and payment.payment_status in _IN_FLIGHT_PAYMENT_STATUSES
    ]
    in_flight_amount = _quantize_money(
        sum((payment.payment_amount for payment in in_flight_payments), Decimal("0"))
    )
    cancelled_count = sum(
        1
        for payment in month_payments
        if payment.payment_currency == currency and payment.payment_status == "CANCELLED"
    )

    payment_leg = _build_leg(
        key="payment_leg",
        operands=(
            ("youtube_revenue_total_usd", payment_summary.youtube_revenue_total_usd),
            ("adsense_paid_amount_usd", payment_summary.adsense_paid_amount),
        ),
        gap_field="payment_gap_usd",
        gap_usd=payment_summary.payment_gap_usd,
        source_status_field="payment_match_status",
        source_status=payment_summary.status,
        component_inputs=(
            (
                "non_paid_adsense_payments",
                "AdSense payments not yet PAID",
                in_flight_amount,
                len(in_flight_payments),
            ),
        ),
        tolerance_usd=tolerance_usd,
        narrative_builder=_payment_leg_narrative,
    )
    bank_leg = _build_leg(
        key="bank_leg",
        operands=(
            ("adsense_paid_amount_usd", bank_summary.adsense_paid_amount_usd),
            ("bank_received_amount_usd", bank_summary.bank_received_amount_usd),
        ),
        gap_field="bank_gap_usd",
        gap_usd=bank_summary.bank_gap_usd,
        source_status_field="bank_reconciliation_status",
        source_status=bank_summary.status,
        component_inputs=(
            (
                "transfer_fee",
                "Bank transfer fees",
                bank_summary.transfer_fee_usd,
                bank_summary.entry_count,
            ),
            (
                "fx_difference",
                "Bank-side FX difference",
                bank_summary.fx_difference_usd,
                bank_summary.entry_count,
            ),
        ),
        tolerance_usd=tolerance_usd,
        narrative_builder=_bank_leg_narrative,
    )

    warnings = _build_warnings(
        month=month,
        payment_summary=payment_summary,
        bank_summary=bank_summary,
        cancelled_count=cancelled_count,
    )
    status = _worst_status(payment_leg.status, bank_leg.status)
    money_provenance = _build_money_provenance(
        payment_leg=payment_leg,
        bank_leg=bank_leg,
        payment_summary=payment_summary,
        bank_summary=bank_summary,
        tolerance_usd=tolerance_usd,
    )
    return MonthGapExplanation(
        month=month,
        currency=currency,
        close_status=close_status,
        status=status,
        tolerance_usd=tolerance_usd,
        payment_leg=payment_leg,
        bank_leg=bank_leg,
        warnings=warnings,
        narrative=_month_narrative(month=month, payment_leg=payment_leg, bank_leg=bank_leg),
        money_provenance=money_provenance,
    )


# ============================================================================
# Purpose: The central leg calculation — decompose one chain leg into
#   residual (gap - sum of evidence components, 4dp), the five-status
#   classification, per-component and residual explain-shape confidence, and
#   the attached deterministic narrative.
# Database/ORM: None — pure arithmetic over the already-fetched summary
#   numbers and component inputs; every Decimal stays exact (no float).
# Standards: The residual keeps its sign; component amounts keep the
#   evidence's own sign (signed FX). Status semantics: UNEXPLAINED is
#   reserved for a leg with NO nonzero component (offsetting evidence still
#   counts); INCOMPLETE whenever the gap is uncomputable. Confidence is
#   per-component — zero evidence rows read LOW regardless of leg state.
# Blast Radius: Every number, status, badge, and narrative the
#   gap-explanation endpoint and Command Center panel show for a leg.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py -> route serialization.
#   - File: tests/finance/test_gap_explanation.py -> the per-status pins.
# ============================================================================
def _build_leg(
    *,
    key: str,
    operands: tuple[tuple[str, Decimal], ...],
    gap_field: str,
    gap_usd: Decimal | None,
    source_status_field: str,
    source_status: str,
    component_inputs: tuple[tuple[str, str, Decimal, int], ...],
    tolerance_usd: Decimal,
    narrative_builder: Callable[["GapExplanationLeg"], str],
) -> GapExplanationLeg:
    """Decompose one leg: residual, status, component confidence, prose."""
    explained_total = _quantize_money(
        sum((amount for _, _, amount, _ in component_inputs), Decimal("0"))
    )
    residual = None if gap_usd is None else _quantize_money(gap_usd - explained_total)
    status = _leg_status(
        gap_usd=gap_usd,
        # UNEXPLAINED is reserved for a leg with NO nonzero component, so the
        # check is per-component: offsetting evidence (fee +5, FX -5) still
        # counts as evidence even though its net sum is zero.
        has_nonzero_component=any(amount != 0 for _, _, amount, _ in component_inputs),
        residual=residual,
        tolerance_usd=tolerance_usd,
    )
    residual_confidence_label, residual_confidence_score = _residual_confidence(
        residual=residual, tolerance_usd=tolerance_usd
    )
    components: list[GapExplanationComponent] = []
    for component_key, label, amount, evidence_count in component_inputs:
        # Per-component, not per-leg: a zero-evidence component is LOW even
        # on an otherwise-explained leg.
        confidence_label, confidence_score = _component_confidence(
            status=status,
            residual=residual,
            tolerance_usd=tolerance_usd,
            evidence_count=evidence_count,
        )
        components.append(
            GapExplanationComponent(
                key=component_key,
                label=label,
                amount_usd=_quantize_money(amount),
                evidence_count=evidence_count,
                confidence_label=confidence_label,
                confidence_score=confidence_score,
            )
        )
    leg = GapExplanationLeg(
        key=key,
        status=status,
        operands=operands,
        gap_field=gap_field,
        gap_usd=gap_usd,
        source_status_field=source_status_field,
        source_status=source_status,
        components=tuple(components),
        unexplained_residual_usd=residual,
        residual_confidence_label=residual_confidence_label,
        residual_confidence_score=residual_confidence_score,
        narrative="",
    )
    # The narrative reads the finished numbers, so it is attached last onto
    # the frozen instance.
    return replace(leg, narrative=narrative_builder(leg))


def _leg_status(
    *,
    gap_usd: Decimal | None,
    has_nonzero_component: bool,
    residual: Decimal | None,
    tolerance_usd: Decimal,
) -> str:
    """Classify one leg per the plan's five statuses.

    The PARTIALLY/UNEXPLAINED split checks whether ANY component is nonzero,
    not the components' net sum: offsetting evidence (a fee and an equal
    opposite FX difference) is still evidence, and UNEXPLAINED is reserved
    for a leg with no nonzero component at all.
    """
    if gap_usd is None or residual is None:
        return "INCOMPLETE"
    if abs(gap_usd) <= tolerance_usd:
        return "MATCHED"
    if abs(residual) <= tolerance_usd:
        return "FULLY_EXPLAINED"
    if has_nonzero_component:
        return "PARTIALLY_EXPLAINED"
    return "UNEXPLAINED"


def _component_confidence(
    *,
    status: str,
    residual: Decimal | None,
    tolerance_usd: Decimal,
    evidence_count: int,
) -> tuple[str, str]:
    """Confidence for ONE evidence component, per the plan's table.

    A component backed by zero source rows is LOW regardless of leg state:
    "evidence-backed" is earned by rows, and the badge must never contradict
    the component's own MISSING_SOURCE provenance.
    """
    if evidence_count == 0:
        return _CONFIDENCE_LOW
    if status == "INCOMPLETE":
        return _CONFIDENCE_LOW
    if residual is not None and abs(residual) <= tolerance_usd:
        return _CONFIDENCE_HIGH
    return _CONFIDENCE_MEDIUM


def _residual_confidence(*, residual: Decimal | None, tolerance_usd: Decimal) -> tuple[str, str]:
    """Confidence for a residual: HIGH when it is explained away, LOW when not."""
    if residual is None:
        return _CONFIDENCE_LOW
    if abs(residual) <= tolerance_usd:
        return _CONFIDENCE_HIGH
    return _CONFIDENCE_LOW


def _worst_status(*statuses: str) -> str:
    """Return the worst status per the module's ordering."""
    return max(statuses, key=_STATUS_ORDER.index)


def _build_warnings(
    *,
    month: str,
    payment_summary: MonthlyPaymentMatchSummary,
    bank_summary: MonthBankReconciliationSummary,
    cancelled_count: int,
) -> tuple[GapExplanationWarning, ...]:
    """Collect the data caveats finance should read next to the story."""
    warnings: list[GapExplanationWarning] = []
    unsupported_count = max(
        payment_summary.unsupported_payment_currency_count,
        bank_summary.unsupported_payment_currency_count,
    )
    if unsupported_count:
        warnings.append(
            GapExplanationWarning(
                code="UNSUPPORTED_PAYMENT_CURRENCY",
                message=(
                    f"{unsupported_count} AdSense payment record(s) for {month} "
                    "are not USD and are excluded from every number here; no FX "
                    "conversion is applied."
                ),
            )
        )
    if cancelled_count:
        warnings.append(
            GapExplanationWarning(
                code="CANCELLED_ADSENSE_PAYMENTS",
                message=(
                    f"{cancelled_count} CANCELLED AdSense payment record(s) for "
                    f"{month} are excluded from the in-flight component."
                ),
            )
        )
    if payment_summary.missing_youtube_source_channel_count:
        warnings.append(
            GapExplanationWarning(
                code="CHANNELS_WITHOUT_YOUTUBE_SOURCE",
                message=(
                    f"{payment_summary.missing_youtube_source_channel_count} "
                    "channel(s) have revenue facts from non-YouTube sources "
                    "only; their revenue is outside the YouTube total."
                ),
            )
        )
    return tuple(warnings)


def _money(value: Decimal) -> str:
    """Render money for prose at 2dp, unsigned handling left to callers."""
    return f"${value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def _signed_money(value: Decimal) -> str:
    """Render signed money for prose, keeping the evidence's own sign."""
    if value < 0:
        return f"-{_money(-value)}"
    return _money(value)


def _payment_leg_narrative(leg: GapExplanationLeg) -> str:
    """Deterministic prose for the payment leg. No LLM."""
    if leg.status == "INCOMPLETE":
        return f"The payment leg cannot be reconciled: {_incomplete_reason(leg.source_status)}"
    operands = dict(leg.operands)
    youtube_total = operands["youtube_revenue_total_usd"]
    paid = operands["adsense_paid_amount_usd"]
    if leg.status == "MATCHED":
        return (
            f"YouTube revenue of {_money(youtube_total)} matches paid AdSense "
            f"payments of {_money(paid)} within tolerance."
        )
    gap = leg.gap_usd if leg.gap_usd is not None else Decimal("0")
    direction = "exceeds" if gap >= 0 else "trails"
    in_flight = next((c for c in leg.components if c.key == "non_paid_adsense_payments"), None)
    in_flight_clause = ""
    if in_flight is not None and in_flight.amount_usd != 0:
        payment_word = "payment" if in_flight.evidence_count == 1 else "payments"
        in_flight_clause = (
            f" {_signed_money(in_flight.amount_usd)} is not yet PAID "
            f"({in_flight.evidence_count} {payment_word});"
        )
    residual = leg.unexplained_residual_usd
    residual_value = residual if residual is not None else Decimal("0")
    return (
        f"YouTube revenue of {_money(youtube_total)} {direction} paid AdSense "
        f"payments of {_money(paid)} by {_money(abs(gap))}.{in_flight_clause} "
        f"{_signed_money(residual_value)} remains unexplained."
    )


def _bank_leg_narrative(leg: GapExplanationLeg) -> str:
    """Deterministic prose for the bank leg. No LLM."""
    if leg.status == "INCOMPLETE":
        return f"The bank leg cannot be reconciled: {_incomplete_reason(leg.source_status)}"
    operands = dict(leg.operands)
    paid = operands["adsense_paid_amount_usd"]
    received = operands["bank_received_amount_usd"]
    if leg.status == "MATCHED":
        return (
            f"Paid AdSense payments of {_money(paid)} match bank receipts of "
            f"{_money(received)} within tolerance."
        )
    gap = leg.gap_usd if leg.gap_usd is not None else Decimal("0")
    direction = "exceed" if gap >= 0 else "trail"
    clauses: list[str] = []
    for component in leg.components:
        if component.amount_usd != 0:
            entry_word = "bank entry" if component.evidence_count == 1 else "bank entries"
            clauses.append(
                f"{_signed_money(component.amount_usd)} in "
                f"{component.label.lower()} ({component.evidence_count} {entry_word})"
            )
    evidence_clause = f" Evidence: {'; '.join(clauses)}." if clauses else ""
    residual = leg.unexplained_residual_usd
    residual_value = residual if residual is not None else Decimal("0")
    return (
        f"Paid AdSense payments of {_money(paid)} {direction} normalized bank "
        f"receipts of {_money(received)} by {_money(abs(gap))}."
        f"{evidence_clause} {_signed_money(residual_value)} remains unexplained."
    )


def _incomplete_reason(source_status: str) -> str:
    """Map a source summary status to the missing-operand prose."""
    reasons = {
        "NO_YOUTUBE_REVENUE": "no YouTube revenue facts exist for the month.",
        "MISSING_ADSENSE_PAYMENT": "no paid USD AdSense payment exists for the month.",
        "MISSING_BANK_RECEIPT": "no bank receipt is recorded for the month.",
    }
    return reasons.get(source_status, "a source side of the leg is missing.")


def _month_narrative(
    *, month: str, payment_leg: GapExplanationLeg, bank_leg: GapExplanationLeg
) -> str:
    """One deterministic summary line for the whole month."""
    return f"{month}: payment leg {payment_leg.status}, bank leg {bank_leg.status}."


# ============================================================================
# Purpose: Provenance entries for ONE leg's numeric fields, dotted-key
#   namespaced (leg.field, leg.components.key.amount_usd) — operands with
#   caller-supplied source/formula/confidence, the gap, each component, and
#   the residual.
# Database/ORM: None — pure mapping over the built leg.
# Standards: Count-honest and computability-honest: component confidence is
#   SOURCE_BACKED only when evidence rows exist, and an uncomputable gap or
#   residual (None) says MISSING_SOURCE — RECONCILED/VARIANCE are reserved
#   for values that were actually computed (the bank-reconciliation
#   _bank_gap_confidence rule).
# Blast Radius: The money_provenance map for both legs — every audit-facing
#   claim about where a leg number came from.
# Connections:
#   - File: backend/ums_smart_revenue/finance/bank_reconciliation.py -> the
#     provenance idiom and confidence-token vocabulary this extends.
#   - File: tests/finance/test_gap_explanation.py -> the programmatic
#     completeness walk and the MISSING_SOURCE pins.
# ============================================================================
def _leg_provenance(
    leg: GapExplanationLeg,
    *,
    operand_sources: dict[str, tuple[str, str, str]],
    gap_formula: str,
    tolerance_usd: Decimal,
) -> dict[str, dict[str, str | None]]:
    """Provenance entries for one leg's numeric fields, dotted-key namespaced."""
    entries: dict[str, dict[str, str | None]] = {}
    for field_name, value in leg.operands:
        source, formula, confidence = operand_sources[field_name]
        entries[f"{leg.key}.{field_name}"] = _provenance_entry(
            source=source, formula=formula, confidence=confidence, export_value=value
        )
    # An uncomputable gap (an operand source is missing entirely) must say
    # MISSING_SOURCE, never masquerade as a calculated VARIANCE — the
    # bank-reconciliation _bank_gap_confidence rule.
    if leg.gap_usd is None:
        gap_confidence = "MISSING_SOURCE"
    elif leg.status == "MATCHED":
        gap_confidence = "RECONCILED"
    else:
        gap_confidence = "VARIANCE"
    entries[f"{leg.key}.{leg.gap_field}"] = _provenance_entry(
        source="composed",
        formula=gap_formula,
        confidence=gap_confidence,
        export_value=leg.gap_usd,
    )
    for component in leg.components:
        entries[f"{leg.key}.components.{component.key}.amount_usd"] = _provenance_entry(
            source=(
                "adsense_payments"
                if component.key == "non_paid_adsense_payments"
                else "bank_reconciliation_entries"
            ),
            formula=_COMPONENT_FORMULAS[component.key],
            confidence=("SOURCE_BACKED" if component.evidence_count > 0 else "MISSING_SOURCE"),
            export_value=component.amount_usd,
        )
    residual_label, _ = _residual_confidence(
        residual=leg.unexplained_residual_usd, tolerance_usd=tolerance_usd
    )
    # Same rule for the residual: a None residual is missing, not a variance.
    if leg.unexplained_residual_usd is None:
        residual_confidence = "MISSING_SOURCE"
    elif residual_label == "HIGH":
        residual_confidence = "RECONCILED"
    else:
        residual_confidence = "VARIANCE"
    entries[f"{leg.key}.unexplained_residual_usd"] = _provenance_entry(
        source="composed",
        formula=f"{leg.gap_field} - sum(component amounts)",
        confidence=residual_confidence,
        export_value=leg.unexplained_residual_usd,
    )
    return entries


_COMPONENT_FORMULAS = {
    "non_paid_adsense_payments": (
        "sum(payment_amount) for records in the month where "
        "payment_currency = USD and payment_status in (PENDING, UNPAID)"
    ),
    "transfer_fee": "sum(transfer_fee_usd) for bank entries in the month",
    "fx_difference": "sum(fx_difference_usd) for bank entries in the month",
}


def _provenance_entry(
    *, source: str, formula: str, confidence: str, export_value: Decimal | None
) -> dict[str, str | None]:
    """One provenance record, the bank-reconciliation wire idiom."""
    return {
        "source": source,
        "formula": formula,
        "confidence": confidence,
        "export_value": _decimal_to_api(export_value),
    }


def _build_money_provenance(
    *,
    payment_leg: GapExplanationLeg,
    bank_leg: GapExplanationLeg,
    payment_summary: MonthlyPaymentMatchSummary,
    bank_summary: MonthBankReconciliationSummary,
    tolerance_usd: Decimal,
) -> dict[str, dict[str, str | None]]:
    """Provenance for EVERY numeric field, with count-honest confidence.

    An operand backed by zero source rows says MISSING_SOURCE, exactly as the
    bank-reconciliation provenance does — SOURCE_BACKED is earned by rows,
    never assumed.
    """
    entries: dict[str, dict[str, str | None]] = {}
    entries.update(
        _leg_provenance(
            payment_leg,
            operand_sources={
                "youtube_revenue_total_usd": (
                    "monthly_channel_revenue_facts",
                    (
                        "sum(gross_revenue_usd) over one YouTube-sourced fact "
                        "per channel (CMS preferred over Analytics)"
                    ),
                    _source_backed(payment_summary.youtube_source_channel_count),
                ),
                "adsense_paid_amount_usd": (
                    "adsense_payments",
                    (
                        "sum(payment_amount) for records in the month where "
                        "payment_currency = USD and payment_status = PAID"
                    ),
                    _source_backed(payment_summary.paid_payment_count),
                ),
            },
            gap_formula=(
                "youtube_revenue_total_usd - adsense_paid_amount_usd when both sides exist"
            ),
            tolerance_usd=tolerance_usd,
        )
    )
    entries.update(
        _leg_provenance(
            bank_leg,
            operand_sources={
                "adsense_paid_amount_usd": (
                    "adsense_payments",
                    (
                        "sum(payment_amount) for records in the month where "
                        "payment_currency = USD and payment_status = PAID"
                    ),
                    _source_backed(bank_summary.paid_payment_count),
                ),
                "bank_received_amount_usd": (
                    "bank_reconciliation_entries",
                    "sum(bank_received_amount_usd) for bank entries in the month",
                    _source_backed(bank_summary.entry_count),
                ),
            },
            gap_formula=(
                "adsense_paid_amount_usd - bank_received_amount_usd when both "
                "paid AdSense and bank receipt sources exist"
            ),
            tolerance_usd=tolerance_usd,
        )
    )
    entries["tolerance_usd"] = _provenance_entry(
        source="gap_explanation_policy",
        formula="configured month gap-explanation tolerance",
        confidence="POLICY",
        export_value=tolerance_usd,
    )
    return entries


def _source_backed(source_count: int) -> str:
    """SOURCE_BACKED when rows exist, MISSING_SOURCE when none do."""
    if source_count > 0:
        return "SOURCE_BACKED"
    return "MISSING_SOURCE"


def _quantize_money(value: Decimal) -> Decimal:
    """4dp ROUND_HALF_UP, the shared money precision of both source builders."""
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
