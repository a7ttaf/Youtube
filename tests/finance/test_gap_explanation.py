# ============================================================================
# Purpose: Unit pins for the month gap-explanation builder (Hard Problem #3)
#   — leg decomposition arithmetic, the five statuses and their worst-of
#   month ordering, the confidence table, warning emission, provenance
#   completeness, and narrative determinism.
# Database/ORM: None — pure-builder tests; the two source summaries are
#   built by their REAL builders from factory entries, so the composition is
#   pinned against genuine summary semantics, not hand-rolled stand-ins.
# Standards: Every test builds its own inputs; no shared fixtures, no
#   cross-test state. Exact-value assertions on money strings.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/finance/gap_explanation.py -> the
#     builder under test.
#   - File: Docs/superpowers/plans/2026-08-23-payment-gap-narrative.md ->
#     the ruled design these pins hold the build to.
# ============================================================================
"""Unit pins for the month gap-explanation builder."""

from datetime import date
from decimal import Decimal

from ums_smart_revenue.finance.adsense_payments import AdSensePaymentEntry
from ums_smart_revenue.finance.bank_reconciliation import (
    BankReconciliationEntry,
    build_month_bank_reconciliation_summary,
)
from ums_smart_revenue.finance.gap_explanation import build_month_gap_explanation
from ums_smart_revenue.finance.payment_matching import build_monthly_payment_match_summary
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry

MONTH = "2026-03"


def _fact(*, channel_id: str = "channel-a", amount: str, source_kind: str = "YOUTUBE_CMS"):
    return RevenueFactEntry(
        id=f"{channel_id}-{source_kind}",
        month=MONTH,
        youtube_channel_id=channel_id,
        source_kind=source_kind,
        source_report_id=f"{source_kind}-{MONTH}",
        gross_revenue_usd=Decimal(amount),
        net_revenue_usd=None,
        views=0,
        watch_time_minutes=Decimal("0"),
        confidence_score=Decimal("1"),
        imported_by=None,
    )


def _payment(*, amount: str, status: str = "PAID", currency: str = "USD", name: str | None = None):
    return AdSensePaymentEntry(
        id=f"payment-{status}-{amount}-{currency}",
        month=MONTH,
        payment_name=name or f"AdSense {status} {amount} {currency}",
        payment_date=date(2026, 4, 21),
        payment_amount=Decimal(amount),
        payment_currency=currency,
        payment_status=status,
        raw_payload={"paymentId": f"{status}-{amount}"},
        source_report_id=f"adsense-payment-{MONTH}",
        source_account_id="pub-1",
        imported_by=None,
    )


def _bank_entry(*, received: str, fee: str = "0", fx: str = "0", reference: str = "bank-1"):
    return BankReconciliationEntry(
        id=f"entry-{reference}",
        month=MONTH,
        bank_reference=reference,
        bank_received_date=date(2026, 4, 22),
        bank_received_amount=Decimal(received),
        bank_received_currency="USD",
        bank_received_amount_usd=Decimal(received),
        transfer_fee_usd=Decimal(fee),
        fx_difference_usd=Decimal(fx),
        notes=None,
        source_report_id=None,
        recorded_by="00000000-0000-0000-0000-000000009901",
    )


def _explain(*, facts, payments, bank_entries, close_status: str = "OPEN"):
    payment_summary = build_monthly_payment_match_summary(
        month=MONTH, facts=facts, payments=payments
    )
    bank_summary = build_month_bank_reconciliation_summary(
        month=MONTH, payments=payments, bank_entries=bank_entries
    )
    return build_month_gap_explanation(
        month=MONTH,
        payment_summary=payment_summary,
        bank_summary=bank_summary,
        payments=payments,
        close_status=close_status,
    )


def test_both_legs_decompose_with_signed_evidence_and_residuals():
    """The headline scenario: explained payment leg, partial bank leg."""
    explanation = _explain(
        facts=[_fact(amount="930")],
        payments=[_payment(amount="900"), _payment(amount="30", status="PENDING")],
        bank_entries=[_bank_entry(received="880", fee="12", fx="5")],
    )

    payload = explanation.to_api()
    payment_leg = payload["payment_leg"]
    assert payment_leg["status"] == "FULLY_EXPLAINED"
    assert payment_leg["payment_gap_usd"] == "30"
    assert payment_leg["components"][0]["key"] == "non_paid_adsense_payments"
    assert payment_leg["components"][0]["amount_usd"] == "30"
    assert payment_leg["components"][0]["evidence_count"] == 1
    assert payment_leg["components"][0]["confidence"] == {"label": "HIGH", "score": "0.95"}
    assert payment_leg["unexplained_residual_usd"] == "0"
    # A residual explained away to zero is HIGH-confidence reconciled.
    assert payment_leg["unexplained_residual_confidence"] == {"label": "HIGH", "score": "0.95"}

    bank_leg = payload["bank_leg"]
    assert bank_leg["status"] == "PARTIALLY_EXPLAINED"
    assert bank_leg["bank_gap_usd"] == "20"
    amounts = {c["key"]: c["amount_usd"] for c in bank_leg["components"]}
    assert amounts == {"transfer_fee": "12", "fx_difference": "5"}
    for component in bank_leg["components"]:
        assert component["confidence"] == {"label": "MEDIUM", "score": "0.80"}
    assert bank_leg["unexplained_residual_usd"] == "3"
    # A residual beyond tolerance carries LOW confidence per the plan's table.
    assert bank_leg["unexplained_residual_confidence"] == {"label": "LOW", "score": "0"}

    # Month status is the worst leg.
    assert payload["status"] == "PARTIALLY_EXPLAINED"
    assert payload["close_status"] == "OPEN"


def test_matched_legs_and_deterministic_narratives():
    """Within-tolerance legs read MATCHED and the prose is stable."""
    explanation = _explain(
        facts=[_fact(amount="930")],
        payments=[_payment(amount="930")],
        bank_entries=[_bank_entry(received="930")],
    )

    payload = explanation.to_api()
    assert payload["status"] == "MATCHED"
    assert payload["payment_leg"]["narrative"] == (
        "YouTube revenue of $930.00 matches paid AdSense payments of $930.00 within tolerance."
    )
    assert payload["bank_leg"]["narrative"] == (
        "Paid AdSense payments of $930.00 match bank receipts of $930.00 within tolerance."
    )
    assert payload["narrative"] == "2026-03: payment leg MATCHED, bank leg MATCHED."

    # Determinism: an identical rebuild serializes identically.
    rebuilt = _explain(
        facts=[_fact(amount="930")],
        payments=[_payment(amount="930")],
        bank_entries=[_bank_entry(received="930")],
    )
    assert rebuilt.to_api() == payload


def test_incomplete_legs_win_the_month_and_carry_low_confidence():
    """A missing operand makes the leg INCOMPLETE — the worst month status."""
    explanation = _explain(
        facts=[],
        payments=[_payment(amount="900"), _payment(amount="30", status="PENDING")],
        bank_entries=[_bank_entry(received="900")],
    )

    payload = explanation.to_api()
    assert payload["payment_leg"]["status"] == "INCOMPLETE"
    assert payload["payment_leg"]["payment_gap_usd"] is None
    assert payload["payment_leg"]["unexplained_residual_usd"] is None
    assert payload["payment_leg"]["components"][0]["confidence"] == {
        "label": "LOW",
        "score": "0",
    }
    # An unavailable residual is LOW-confidence, distinguishing it from a
    # reconciled zero on the wire.
    assert payload["payment_leg"]["unexplained_residual_confidence"] == {
        "label": "LOW",
        "score": "0",
    }
    assert payload["payment_leg"]["narrative"] == (
        "The payment leg cannot be reconciled: no YouTube revenue facts exist for the month."
    )
    # The bank leg is fine on its own; the month still reads INCOMPLETE.
    assert payload["bank_leg"]["status"] == "MATCHED"
    assert payload["status"] == "INCOMPLETE"
    # Zero-fact operand provenance is honest about its missing source, and so
    # are the uncomputable gap and residual — never a fabricated VARIANCE.
    provenance = payload["money_provenance"]
    assert provenance["payment_leg.youtube_revenue_total_usd"]["confidence"] == "MISSING_SOURCE"
    assert provenance["payment_leg.payment_gap_usd"]["confidence"] == "MISSING_SOURCE"
    assert provenance["payment_leg.payment_gap_usd"]["export_value"] is None
    assert provenance["payment_leg.unexplained_residual_usd"]["confidence"] == "MISSING_SOURCE"


def test_unexplained_bank_gap_without_evidence():
    """A gap beyond tolerance with zero evidence components is UNEXPLAINED."""
    explanation = _explain(
        facts=[_fact(amount="930")],
        payments=[_payment(amount="930")],
        bank_entries=[_bank_entry(received="920")],
    )

    payload = explanation.to_api()
    bank_leg = payload["bank_leg"]
    assert bank_leg["status"] == "UNEXPLAINED"
    assert bank_leg["bank_gap_usd"] == "10"
    assert bank_leg["unexplained_residual_usd"] == "10"
    assert payload["status"] == "UNEXPLAINED"
    assert bank_leg["narrative"] == (
        "Paid AdSense payments of $930.00 exceed normalized bank receipts of "
        "$920.00 by $10.00. $10.00 remains unexplained."
    )


def test_negative_payment_gap_reads_trails_and_stays_signed():
    """Overpayment flips the direction word and the residual keeps its sign."""
    explanation = _explain(
        facts=[_fact(amount="930")],
        payments=[_payment(amount="1000")],
        bank_entries=[_bank_entry(received="1000")],
    )

    payload = explanation.to_api()
    payment_leg = payload["payment_leg"]
    assert payment_leg["payment_gap_usd"] == "-70"
    assert payment_leg["status"] == "UNEXPLAINED"
    assert payment_leg["unexplained_residual_usd"] == "-70"
    assert payment_leg["narrative"] == (
        "YouTube revenue of $930.00 trails paid AdSense payments of $1000.00 "
        "by $70.00. -$70.00 remains unexplained."
    )


def test_residual_tolerance_edge_is_fully_explained():
    """|residual| exactly at tolerance counts as explained."""
    explanation = _explain(
        facts=[_fact(amount="930")],
        payments=[_payment(amount="930")],
        # gap = 930 - 920 = 10; fee explains 9.99 -> residual 0.01 == tolerance
        bank_entries=[_bank_entry(received="920", fee="9.99")],
    )

    bank_leg = explanation.to_api()["bank_leg"]
    assert bank_leg["unexplained_residual_usd"] == "0.01"
    assert bank_leg["status"] == "FULLY_EXPLAINED"
    assert bank_leg["components"][0]["confidence"] == {"label": "HIGH", "score": "0.95"}


def test_gap_tolerance_edge_is_matched():
    """|gap| exactly at tolerance reads MATCHED, not explained-away."""
    explanation = _explain(
        facts=[_fact(amount="930")],
        payments=[_payment(amount="930")],
        bank_entries=[_bank_entry(received="929.99")],
    )

    bank_leg = explanation.to_api()["bank_leg"]
    assert bank_leg["bank_gap_usd"] == "0.01"
    assert bank_leg["status"] == "MATCHED"
    assert bank_leg["narrative"] == (
        "Paid AdSense payments of $930.00 match bank receipts of $929.99 within tolerance."
    )


def test_negative_fx_difference_widens_the_residual_and_stays_signed():
    """A negative FX component keeps its sign in amounts, residual, and prose."""
    explanation = _explain(
        facts=[_fact(amount="930")],
        payments=[_payment(amount="930")],
        # gap = 930 - 925 = 5; evidence = 12 + (-5) = 7 -> residual = -2
        bank_entries=[_bank_entry(received="925", fee="12", fx="-5")],
    )

    bank_leg = explanation.to_api()["bank_leg"]
    assert bank_leg["status"] == "PARTIALLY_EXPLAINED"
    amounts = {c["key"]: c["amount_usd"] for c in bank_leg["components"]}
    assert amounts == {"transfer_fee": "12", "fx_difference": "-5"}
    assert bank_leg["unexplained_residual_usd"] == "-2"
    assert bank_leg["narrative"] == (
        "Paid AdSense payments of $930.00 exceed normalized bank receipts of "
        "$925.00 by $5.00. Evidence: $12.00 in bank transfer fees (1 bank "
        "entry); -$5.00 in bank-side fx difference (1 bank entry). -$2.00 "
        "remains unexplained."
    )


def test_zero_evidence_component_reads_low_even_on_an_explained_leg():
    """A component with no source rows is LOW — badges never contradict provenance.

    An unexplained payment gap with NO in-flight rows still emits the
    component row (stable wire shape), but its confidence must be LOW and its
    provenance MISSING_SOURCE, never the leg-derived MEDIUM/HIGH.
    """
    explanation = _explain(
        facts=[_fact(amount="930")],
        payments=[_payment(amount="900")],
        bank_entries=[_bank_entry(received="900")],
    )

    payload = explanation.to_api()
    component = payload["payment_leg"]["components"][0]
    assert payload["payment_leg"]["status"] == "UNEXPLAINED"
    assert component["evidence_count"] == 0
    assert component["amount_usd"] == "0"
    assert component["confidence"] == {"label": "LOW", "score": "0"}
    assert (
        payload["money_provenance"]["payment_leg.components.non_paid_adsense_payments.amount_usd"][
            "confidence"
        ]
        == "MISSING_SOURCE"
    )


def test_offsetting_components_still_read_partially_explained():
    """Nonzero components that sum to zero are evidence, not UNEXPLAINED.

    UNEXPLAINED is reserved for a leg with NO nonzero component; a fee and an
    equal opposite FX difference offset in the residual arithmetic but both
    remain visible evidence rows.
    """
    explanation = _explain(
        facts=[_fact(amount="930")],
        payments=[_payment(amount="930")],
        # gap = 930 - 920 = 10; evidence = 5 + (-5) = 0 -> residual = 10
        bank_entries=[_bank_entry(received="920", fee="5", fx="-5")],
    )

    bank_leg = explanation.to_api()["bank_leg"]
    assert bank_leg["status"] == "PARTIALLY_EXPLAINED"
    amounts = {c["key"]: c["amount_usd"] for c in bank_leg["components"]}
    assert amounts == {"transfer_fee": "5", "fx_difference": "-5"}
    assert bank_leg["unexplained_residual_usd"] == "10"


def test_partial_payment_leg_and_incomplete_bank_leg():
    """The remaining status cells: payment PARTIALLY, bank INCOMPLETE."""
    explanation = _explain(
        facts=[_fact(amount="930")],
        # gap = 30, in-flight explains only 10 -> residual 20 beyond tolerance.
        payments=[_payment(amount="900"), _payment(amount="10", status="PENDING")],
        bank_entries=[],
    )

    payload = explanation.to_api()
    payment_leg = payload["payment_leg"]
    assert payment_leg["status"] == "PARTIALLY_EXPLAINED"
    assert payment_leg["components"][0]["amount_usd"] == "10"
    assert payment_leg["components"][0]["confidence"] == {"label": "MEDIUM", "score": "0.80"}
    assert payment_leg["unexplained_residual_usd"] == "20"
    # No bank entries -> the bank operand side is missing entirely.
    bank_leg = payload["bank_leg"]
    assert bank_leg["status"] == "INCOMPLETE"
    assert bank_leg["bank_gap_usd"] is None
    assert bank_leg["bank_reconciliation_status"] == "MISSING_BANK_RECEIPT"
    assert bank_leg["unexplained_residual_usd"] is None
    # INCOMPLETE outranks PARTIALLY_EXPLAINED in the worst-of month status.
    assert payload["status"] == "INCOMPLETE"


def test_tolerance_default_matches_both_source_builders():
    """Drift alarm: the leg tolerance must equal the source builders' defaults.

    The module keeps its own constant so one tolerance governs both legs; if
    either source default ever moves, this failure forces the divergence to
    become an explicit decision instead of a silent one.
    """
    from ums_smart_revenue.finance.bank_reconciliation import (
        DEFAULT_BANK_RECONCILIATION_TOLERANCE_USD,
    )
    from ums_smart_revenue.finance.gap_explanation import (
        DEFAULT_GAP_EXPLANATION_TOLERANCE_USD,
    )
    from ums_smart_revenue.finance.payment_matching import (
        DEFAULT_PAYMENT_MATCH_TOLERANCE_USD,
    )

    assert DEFAULT_GAP_EXPLANATION_TOLERANCE_USD == DEFAULT_PAYMENT_MATCH_TOLERANCE_USD
    assert DEFAULT_GAP_EXPLANATION_TOLERANCE_USD == DEFAULT_BANK_RECONCILIATION_TOLERANCE_USD


def test_cancelled_and_non_usd_payments_are_warned_never_summed():
    """CANCELLED and non-USD rows never enter a component; both warn."""
    explanation = _explain(
        facts=[
            _fact(amount="930"),
            _fact(channel_id="channel-b", amount="5", source_kind="ADSENSE"),
        ],
        payments=[
            _payment(amount="900"),
            _payment(amount="25", status="PENDING"),
            _payment(amount="40", status="CANCELLED"),
            _payment(amount="99", currency="EUR", name="EUR row"),
            # A non-USD CANCELLED row belongs to the currency warning only —
            # the CANCELLED count is scoped to the requested currency so one
            # row never inflates two warnings.
            _payment(amount="7", status="CANCELLED", currency="EUR", name="EUR cancelled"),
        ],
        bank_entries=[_bank_entry(received="900")],
    )

    payload = explanation.to_api()
    assert payload["payment_leg"]["components"][0]["amount_usd"] == "25"
    warnings = {w["code"]: w["message"] for w in payload["warnings"]}
    assert set(warnings) == {
        "UNSUPPORTED_PAYMENT_CURRENCY",
        "CANCELLED_ADSENSE_PAYMENTS",
        "CHANNELS_WITHOUT_YOUTUBE_SOURCE",
    }
    assert warnings["CANCELLED_ADSENSE_PAYMENTS"].startswith("1 CANCELLED")
    assert warnings["UNSUPPORTED_PAYMENT_CURRENCY"].startswith("2 ")


def test_provenance_covers_every_numeric_field():
    """Every serialized money field has a provenance entry, and vice versa."""
    explanation = _explain(
        facts=[_fact(amount="930")],
        payments=[_payment(amount="900"), _payment(amount="30", status="PENDING")],
        bank_entries=[_bank_entry(received="880", fee="12", fx="5")],
    )
    payload = explanation.to_api()
    provenance = payload["money_provenance"]

    expected_keys = {"tolerance_usd"}
    for leg_key in ("payment_leg", "bank_leg"):
        leg = payload[leg_key]
        numeric_fields = [
            field for field in leg if field.endswith("_usd") and field != "unexplained_residual_usd"
        ]
        for field in numeric_fields:
            expected_keys.add(f"{leg_key}.{field}")
        expected_keys.add(f"{leg_key}.unexplained_residual_usd")
        for component in leg["components"]:
            expected_keys.add(f"{leg_key}.components.{component['key']}.amount_usd")

    assert set(provenance) == expected_keys
    for key, entry in provenance.items():
        assert set(entry) == {"source", "formula", "confidence", "export_value"}, key
    # Export values mirror the serialized payload numbers.
    assert (
        provenance["payment_leg.payment_gap_usd"]["export_value"]
        == payload["payment_leg"]["payment_gap_usd"]
    )
    assert provenance["bank_leg.components.fx_difference.amount_usd"]["export_value"] == "5"
