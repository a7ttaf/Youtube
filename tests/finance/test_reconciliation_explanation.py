"""Unit tests for deterministic reconciliation explanation prose."""

from decimal import Decimal

from ums_smart_revenue.finance.explanations import SUPPORTED_METRICS
from ums_smart_revenue.finance.reconciliation_explanation import (
    REVENUE_RECONCILIATION_METRIC,
    build_reconciliation_explanation,
)
from ums_smart_revenue.finance.reconciliation_workflow import ChannelReconciliation

D = Decimal


def _line():
    return ChannelReconciliation(
        youtube_channel_id="c1",
        gross_usd=D("100.000000"),
        us_tax_usd=D("15.000000"),
        yt_adsense_fee_usd=D("5.000000"),
        adsense_bank_fee_usd=D("3.000000"),
        fx_variance_usd=D("2.000000"),
        net_received_usd=D("75.000000"),
        us_view_share=D("0.5"),
    )


def test_metric_registered():
    assert REVENUE_RECONCILIATION_METRIC in SUPPORTED_METRICS


def test_explanation_shape():
    entry = build_reconciliation_explanation(
        month="2026-03", line=_line(), warnings=[]
    )
    assert entry.entity_type == "channel"
    assert entry.entity_id == "c1"
    assert entry.metric == REVENUE_RECONCILIATION_METRIC
    assert entry.value == D("75.000000")
    assert entry.currency == "USD"
    keys = {comp["key"] for comp in entry.components}
    assert {
        "estimated_gross_usd",
        "us_tax_usd",
        "yt_adsense_fee_usd",
        "adsense_bank_fee_usd",
        "fx_variance_usd",
        "net_received_usd",
        "narrative",
    } <= keys


def test_narrative_is_deterministic_prose():
    entry = build_reconciliation_explanation(
        month="2026-03", line=_line(), warnings=[]
    )
    narrative = next(c for c in entry.components if c["key"] == "narrative")["text"]
    assert "100" in narrative and "75" in narrative
    # Deterministic: same inputs => identical text.
    again = build_reconciliation_explanation(
        month="2026-03", line=_line(), warnings=[]
    )
    again_text = next(c for c in again.components if c["key"] == "narrative")["text"]
    assert narrative == again_text


def test_negative_fx_variance_renders_as_positive_benefit_not_double_negative():
    """Signed FX variance prose must not render a double-negative dollar amount."""
    entry = build_reconciliation_explanation(
        month="2026-03",
        line=ChannelReconciliation(
            youtube_channel_id="c1",
            gross_usd=Decimal("100"),
            us_tax_usd=Decimal("0"),
            yt_adsense_fee_usd=Decimal("10"),
            adsense_bank_fee_usd=Decimal("3"),
            fx_variance_usd=Decimal("-5"),
            net_received_usd=Decimal("92"),
            us_view_share=None,
        ),
        warnings=[],
    )

    narrative = next(
        component["text"]
        for component in entry.components
        if component["key"] == "narrative"
    )
    assert "$-" not in narrative
    assert "+$5.00 FX" in narrative
