"""Unit tests for deterministic reconciliation explanation prose."""

from decimal import Decimal

import pytest

from ums_smart_revenue.finance.explanations import (
    SUPPORTED_METRICS,
    NumberExplanationValidationError,
    build_channel_month_revenue_explanation,
)
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
    """The reconciliation metric is intentionally NOT in the generic SUPPORTED_METRICS set.

    Regression for chatgpt-codex-connector PRRT_kwDOSZIgN86IT-bX: removing the
    reconciliation metric from the generic supported set closes the gap where
    a caller with only channel revenue/confidence access could overwrite the
    smart reconciliation row through the generic
    ``POST /revenue/channels/{id}/months/{month}/explain?metric=...`` endpoint.
    The metric is built and persisted only by ``build_reconciliation_explanation``
    via the dedicated ``ReconciliationWorkflowService`` workflow.
    """
    assert REVENUE_RECONCILIATION_METRIC not in SUPPORTED_METRICS


def test_generic_builder_rejects_reconciliation_metric():
    """The generic builder raises a typed validation error for the reconciliation metric.

    Belt-and-braces: even if a future caller bypasses the route-level guard,
    the finance builder refuses to construct an adjusted-gross-shaped
    explanation under the smart reconciliation metric.
    """
    with pytest.raises(NumberExplanationValidationError):
        build_channel_month_revenue_explanation(
            facts=[],
            manual_overrides=[],
            month="2026-03",
            youtube_channel_id="c1",
            metric=REVENUE_RECONCILIATION_METRIC,
        )


def test_explanation_shape():
    entry = build_reconciliation_explanation(month="2026-03", line=_line(), warnings=[])
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
    entry = build_reconciliation_explanation(month="2026-03", line=_line(), warnings=[])
    narrative = next(c for c in entry.components if c["key"] == "narrative")["text"]
    assert "100" in narrative and "75" in narrative
    # Deterministic: same inputs => identical text.
    again = build_reconciliation_explanation(month="2026-03", line=_line(), warnings=[])
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
        component["text"] for component in entry.components if component["key"] == "narrative"
    )
    assert "$-" not in narrative
    assert "+$5.00 FX" in narrative


def test_low_confidence_for_reconciliation_anomaly_warning():
    """Non-MISSING serious warnings lower persisted confidence to LOW."""
    entry = build_reconciliation_explanation(
        month="2026-03",
        line=_line(),
        warnings=[
            {
                "code": "RECONCILIATION_ANOMALY",
                "message": "Bank exceeds AdSense after FX; bank fee clamped to 0",
            }
        ],
    )
    assert entry.confidence == {"label": "LOW", "score": "0"}


def test_low_confidence_for_unscoped_bank_basis_warning():
    """BANK_BASIS_UNSCOPED (suppressed bank/FX evidence) lowers confidence to LOW."""
    entry = build_reconciliation_explanation(
        month="2026-03",
        line=_line(),
        warnings=[
            {
                "code": "BANK_BASIS_UNSCOPED",
                "message": "Bank basis does not match AdSense basis",
            }
        ],
    )
    assert entry.confidence == {"label": "LOW", "score": "0"}


def test_low_confidence_for_zero_gross_basis_warning():
    """ZERO_GROSS_RECONCILIATION_BASIS lowers confidence to LOW."""
    entry = build_reconciliation_explanation(
        month="2026-03",
        line=_line(),
        warnings=[
            {
                "code": "ZERO_GROSS_RECONCILIATION_BASIS",
                "message": "Hop 3 totals suppressed",
            }
        ],
    )
    assert entry.confidence == {"label": "LOW", "score": "0"}


def test_medium_confidence_for_unrelated_warning():
    """Unrelated info-only warning codes keep MEDIUM confidence."""
    entry = build_reconciliation_explanation(
        month="2026-03",
        line=_line(),
        warnings=[
            {
                "code": "INFO_BANK_FEE_LARGER_THAN_DELTA",
                "message": "Bank fee larger than expected; reviewed by analyst",
            }
        ],
    )
    assert entry.confidence == {"label": "MEDIUM", "score": "0.80"}


def test_low_confidence_for_missing_us_view_share():
    """MISSING-prefixed warnings keep the prior LOW behaviour for missing inputs."""
    entry = build_reconciliation_explanation(
        month="2026-03",
        line=_line(),
        warnings=[
            {
                "code": "MISSING_US_VIEW_DATA",
                "message": "US-view share missing; tax may be understated",
            }
        ],
    )
    assert entry.confidence == {"label": "LOW", "score": "0"}
