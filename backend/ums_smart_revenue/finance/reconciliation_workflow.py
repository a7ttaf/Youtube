"""Pure compute core for the smart revenue reconciliation workflow.

Derives the three reductions from actual figures and attributes the aggregate
ones per channel proportional to CMS gross. No DB access — fully unit-testable.

Hops:
  1. US tax (per channel)   = us_view_share * gross * withholding_rate
  2. YouTube->AdSense fee   = residual ((G - tax) - adsense_received), attributed
  3. AdSense->bank fee+FX   = residual (adsense - bank), FX from bank deltas,
                              remainder = fee; both attributed ∝ gross
Rounding remainder for each attributed aggregate lands on the largest-gross
channel so per-channel sums equal the aggregate exactly.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Protocol

_Q = Decimal("0.000001")
DEFAULT_US_WITHHOLDING_RATE = Decimal("0.30")


def _q(value: Decimal) -> Decimal:
    """Quantize to 6dp (matches Numeric(_,6) columns), round half up."""
    return value.quantize(_Q, rounding=ROUND_HALF_UP)


class UsViewShareProvider(Protocol):
    """Supplies the US-view revenue fraction (0..1) for a channel-month."""

    def us_view_share(
        self, month: str, youtube_channel_id: str
    ) -> Decimal | None:
        """Return the US-view fraction, or None if unavailable."""
        ...


class NullUsViewShareProvider:
    """Default provider: US-view data not yet ingested (refine-later)."""

    def us_view_share(
        self, month: str, youtube_channel_id: str
    ) -> Decimal | None:
        """Always None until a real geography feed exists."""
        return None


@dataclass(frozen=True)
class ChannelReconciliation:
    """Per-channel reconciliation outcome for a month."""

    youtube_channel_id: str
    gross_usd: Decimal
    us_tax_usd: Decimal
    yt_adsense_fee_usd: Decimal
    adsense_bank_fee_usd: Decimal
    fx_variance_usd: Decimal
    net_received_usd: Decimal
    us_view_share: Decimal | None


@dataclass(frozen=True)
class MonthReconciliationResult:
    """Whole-month reconciliation result plus derived rates and warnings."""

    month: str
    channels: list[ChannelReconciliation]
    gross_total_usd: Decimal
    us_tax_total_usd: Decimal
    yt_adsense_fee_total_usd: Decimal
    adsense_bank_fee_total_usd: Decimal
    fx_total_usd: Decimal
    net_total_usd: Decimal
    yt_adsense_fee_pct: Decimal | None
    warnings: list[dict[str, str]] = field(default_factory=list)


def _attribute(
    total: Decimal, gross: dict[str, Decimal], g: Decimal
) -> dict[str, Decimal]:
    """Split ``total`` across channels ∝ gross; remainder to largest gross."""
    if total <= 0 or g <= 0:
        return {c: Decimal("0.000000") for c in gross}
    out = {c: _q(total * (gross[c] / g)) for c in gross}
    drift = total - sum(out.values(), Decimal("0"))
    if drift != 0:
        largest = max(gross, key=lambda c: gross[c])
        out[largest] = _q(out[largest] + drift)
    return out


def compute_month_reconciliation(
    *,
    month: str,
    channel_gross: Mapping[str, Decimal],
    us_view_shares: Mapping[str, Decimal | None],
    adsense_received_usd: Decimal | None,
    bank_received_usd: Decimal | None,
    fx_total_usd: Decimal,
    withholding_rate: Decimal = DEFAULT_US_WITHHOLDING_RATE,
) -> MonthReconciliationResult:
    """Compute per-channel tax, transfer fees, FX, and net received for a month."""
    gross = {c: Decimal(v) for c, v in channel_gross.items()}
    g = sum(gross.values(), Decimal("0"))
    warnings: list[dict[str, str]] = []

    # Hop 1 — US tax per channel.
    us_tax = {
        c: _q(
            (us_view_shares.get(c) or Decimal("0")) * gross[c] * withholding_rate
        )
        for c in gross
    }
    tax_total = sum(us_tax.values(), Decimal("0"))
    if any(us_view_shares.get(c) is None for c in gross):
        warnings.append(
            {
                "code": "MISSING_US_VIEW_DATA",
                "message": "US-view share missing; tax may be understated",
            }
        )

    # Hop 2 — YouTube->AdSense residual fee.
    if adsense_received_usd is None:
        warnings.append(
            {
                "code": "MISSING_ADSENSE_TOTAL",
                "message": "No AdSense total; fee not derived",
            }
        )
        yt_fee_total = Decimal("0")
        yt_fee_pct: Decimal | None = None
    else:
        base = g - tax_total
        if adsense_received_usd > base:
            warnings.append(
                {
                    "code": "RECONCILIATION_ANOMALY",
                    "message": "AdSense received exceeds estimate; fee clamped to 0",
                }
            )
            yt_fee_total = Decimal("0")
        else:
            yt_fee_total = _q(base - adsense_received_usd)
        yt_fee_pct = _q(yt_fee_total / base) if base > 0 else None
    yt_fee = _attribute(yt_fee_total, gross, g)

    # Hop 3 — AdSense->bank fee + FX.
    fx_total = max(Decimal("0"), fx_total_usd)
    if adsense_received_usd is None or bank_received_usd is None:
        if bank_received_usd is None:
            warnings.append(
                {
                    "code": "MISSING_BANK_TOTAL",
                    "message": "No bank receipt; fee/FX not derived",
                }
            )
        fee_part = Decimal("0")
        fx_part = Decimal("0")
    else:
        delta = adsense_received_usd - bank_received_usd
        if delta < 0:
            warnings.append(
                {
                    "code": "RECONCILIATION_ANOMALY",
                    "message": "Bank exceeds AdSense; bank fee clamped to 0",
                }
            )
            delta = Decimal("0")
        fx_part = min(fx_total, delta)
        fee_part = _q(delta - fx_part)
    adsense_bank_fee = _attribute(fee_part, gross, g)
    fx_variance = _attribute(fx_part, gross, g)

    channels: list[ChannelReconciliation] = []
    for c in gross:
        net = _q(
            gross[c]
            - us_tax[c]
            - yt_fee[c]
            - adsense_bank_fee[c]
            - fx_variance[c]
        )
        channels.append(
            ChannelReconciliation(
                youtube_channel_id=c,
                gross_usd=_q(gross[c]),
                us_tax_usd=us_tax[c],
                yt_adsense_fee_usd=yt_fee[c],
                adsense_bank_fee_usd=adsense_bank_fee[c],
                fx_variance_usd=fx_variance[c],
                net_received_usd=net,
                us_view_share=us_view_shares.get(c),
            )
        )
    channels.sort(key=lambda x: x.youtube_channel_id)
    return MonthReconciliationResult(
        month=month,
        channels=channels,
        gross_total_usd=_q(g),
        us_tax_total_usd=_q(tax_total),
        yt_adsense_fee_total_usd=_q(yt_fee_total),
        adsense_bank_fee_total_usd=_q(fee_part),
        fx_total_usd=_q(fx_part),
        net_total_usd=_q(sum((x.net_received_usd for x in channels), Decimal("0"))),
        yt_adsense_fee_pct=yt_fee_pct,
        warnings=warnings,
    )
