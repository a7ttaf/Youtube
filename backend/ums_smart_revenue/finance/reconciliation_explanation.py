"""Build the revenue_reconciliation explanation (components + prose) for a
channel-month from a ChannelReconciliation. Deterministic; no LLM."""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from types import MappingProxyType

from ums_smart_revenue.finance.explanations import (
    REVENUE_RECONCILIATION_METRIC,
    NumberExplanationEntry,
)
from ums_smart_revenue.finance.reconciliation_workflow import ChannelReconciliation

__all__ = ["REVENUE_RECONCILIATION_METRIC", "build_reconciliation_explanation"]

# FIX: Module-level mapping is read-only at runtime. The wrapped proxy prevents
# any caller (including future test or integration code) from mutating the
# labels, mirroring the immutable-dict convention used in
# google_source_normalizer.py (see lines 74-80).
_GROSS_SOURCE_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "YOUTUBE_CMS": "Estimated gross (CMS)",
        "YOUTUBE_ANALYTICS": "Gross (YouTube analytics)",
        "ADSENSE": "Gross (AdSense)",
        "MANUAL_UPLOAD": "Gross (manual upload)",
        "ALLOCATION": "Gross (allocation)",
    }
)


def _money(value: Decimal) -> str:
    """Render a Decimal as a plain 2dp money string for prose."""
    return f"{value.quantize(Decimal('0.01'))}"


def _signed_fx_adjustment(value: Decimal) -> str:
    """Render FX as a signed prose adjustment without double-negative money."""
    amount = abs(value)
    if value < 0:
        return f"+${_money(amount)} FX"
    return f"-${_money(amount)} FX"


def _component(key: str, label: str, value: Decimal) -> dict[str, object]:
    """One numeric explanation component."""
    return {"key": key, "label": label, "value": str(value)}


def _gross_source_label(source_kind: str | None) -> str:
    """Return the user-facing gross provenance label for the selected fact."""
    if source_kind is None:
        return _GROSS_SOURCE_LABELS["YOUTUBE_CMS"]
    return _GROSS_SOURCE_LABELS.get(source_kind, "Source-backed gross")


def build_reconciliation_explanation(
    *,
    month: str,
    line: ChannelReconciliation,
    warnings: list[dict[str, str]],
    gross_source_kind: str | None = None,
) -> NumberExplanationEntry:
    """Assemble the persisted explanation for one channel-month reconciliation."""
    # ========================================================================
    # Purpose: Turn a per-channel ChannelReconciliation into a persisted
    #   NumberExplanationEntry: one component per derived hop plus a
    #   deterministic prose narrative. No randomness/LLM -> identical inputs
    #   yield identical text (auditable).
    # Database/ORM: None (caller persists via SqlAlchemyNumberExplanationRepository).
    # Standards: Typed; pure; Decimal money rendering at 2dp.
    # Blast Radius: Finance number explanations (revenue_reconciliation metric).
    # ========================================================================
    share_txt = (
        f"{(line.us_view_share * 100).quantize(Decimal('0.1'))}% US views"
        if line.us_view_share is not None
        else "US-view share unavailable"
    )
    narrative = (
        f"Channel {line.youtube_channel_id} for {month}: estimated "
        f"${_money(line.gross_usd)}; -${_money(line.us_tax_usd)} US tax "
        f"({share_txt}); -${_money(line.yt_adsense_fee_usd)} YouTube->AdSense "
        f"transfer; -${_money(line.adsense_bank_fee_usd)} AdSense->bank fee "
        f"and {_signed_fx_adjustment(line.fx_variance_usd)} => "
        f"${_money(line.net_received_usd)} received."
    )
    components: list[dict[str, object]] = [
        _component(
            "estimated_gross_usd",
            _gross_source_label(gross_source_kind),
            line.gross_usd,
        ),
        _component("us_tax_usd", "US tax", line.us_tax_usd),
        _component(
            "yt_adsense_fee_usd", "YouTube->AdSense transfer fee",
            line.yt_adsense_fee_usd,
        ),
        _component(
            "adsense_bank_fee_usd", "AdSense->bank transfer fee",
            line.adsense_bank_fee_usd,
        ),
        _component("fx_variance_usd", "FX variance", line.fx_variance_usd),
        _component("net_received_usd", "Net received", line.net_received_usd),
        {"key": "narrative", "label": "Reconciliation narrative", "text": narrative},
    ]
    confidence = (
        {"label": "LOW", "score": "0"}
        if any(w["code"].startswith("MISSING") for w in warnings)
        else {"label": "MEDIUM", "score": "0.80"}
    )
    return NumberExplanationEntry(
        month=month,
        entity_type="channel",
        entity_id=line.youtube_channel_id,
        metric=REVENUE_RECONCILIATION_METRIC,
        value=line.net_received_usd,
        currency="USD",
        formula=(
            "estimated_gross - us_tax - yt_adsense_fee - adsense_bank_fee - fx_variance"
        ),
        confidence=confidence,
        components=components,
        warnings=list(warnings),
    )
