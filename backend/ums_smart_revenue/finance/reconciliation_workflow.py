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

    def us_view_share(self, month: str, youtube_channel_id: str) -> Decimal | None:
        """Return the US-view fraction, or None if unavailable."""
        ...


class NullUsViewShareProvider:
    """Default provider: US-view data not yet ingested (refine-later)."""

    @staticmethod
    def us_view_share(month: str, youtube_channel_id: str) -> Decimal | None:
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


@dataclass(frozen=True)
class _UsTaxResult:
    """Aggregate and per-channel US withholding estimate."""

    by_channel: dict[str, Decimal]
    total: Decimal


@dataclass(frozen=True)
class _YtAdsenseFeeResult:
    """YouTube-to-AdSense residual fee state."""

    total: Decimal
    pct: Decimal | None
    adsense_clamped: bool


@dataclass(frozen=True)
class _BankResidualResult:
    """AdSense-to-bank residual split between fee and FX."""

    fee_part: Decimal
    fx_part: Decimal


def _attribute(total: Decimal, gross: dict[str, Decimal], g: Decimal) -> dict[str, Decimal]:
    """Split ``total`` across channels ∝ gross; remainder to largest gross."""
    if total == 0 or g <= 0:
        return {c: Decimal("0.000000") for c in gross}
    out = {c: _q(total * (gross[c] / g)) for c in gross}
    drift = total - sum(out.values(), Decimal("0"))
    if drift != 0:
        largest = max(gross, key=lambda c: gross[c])
        out[largest] = _q(out[largest] + drift)
    return out


def _compute_us_tax(
    *,
    gross: dict[str, Decimal],
    us_view_shares: Mapping[str, Decimal | None],
    withholding_rate: Decimal,
    warnings: list[dict[str, str]],
) -> _UsTaxResult:
    """Compute per-channel US tax and append the missing-data warning."""
    by_channel = {
        c: _q((us_view_shares.get(c) or Decimal("0")) * gross[c] * withholding_rate) for c in gross
    }
    if any(us_view_shares.get(c) is None for c in gross):
        warnings.append(
            {
                "code": "MISSING_US_VIEW_DATA",
                "message": "US-view share missing; tax may be understated",
            }
        )
    return _UsTaxResult(by_channel=by_channel, total=sum(by_channel.values(), Decimal("0")))


def _compute_yt_adsense_fee(
    *,
    gross_total: Decimal,
    tax_total: Decimal,
    adsense_received_usd: Decimal | None,
    warnings: list[dict[str, str]],
) -> _YtAdsenseFeeResult:
    """Compute the YouTube-to-AdSense residual fee and anomaly warning."""
    if adsense_received_usd is None:
        warnings.append(
            {
                "code": "MISSING_ADSENSE_TOTAL",
                "message": "No AdSense total; fee not derived",
            }
        )
        return _YtAdsenseFeeResult(
            total=Decimal("0"),
            pct=None,
            adsense_clamped=False,
        )

    base = gross_total - tax_total
    if adsense_received_usd > base:
        warnings.append(
            {
                "code": "RECONCILIATION_ANOMALY",
                "message": (
                    "AdSense received exceeds estimate; YouTube->AdSense "
                    "fee clamped to 0 and bank-fee math uses the estimate "
                    "basis so the residual does not penalize CMS channels"
                ),
            }
        )
        return _YtAdsenseFeeResult(
            total=Decimal("0"),
            pct=Decimal("0.000000") if base > 0 else None,
            adsense_clamped=True,
        )

    yt_fee_total = _q(base - adsense_received_usd)
    return _YtAdsenseFeeResult(
        total=yt_fee_total,
        pct=_q(yt_fee_total / base) if base > 0 else None,
        adsense_clamped=False,
    )


def _effective_adsense_basis(
    *,
    gross_total: Decimal,
    tax_total: Decimal,
    adsense_received_usd: Decimal | None,
    adsense_clamped: bool,
) -> Decimal | None:
    """Return the Hop 3 AdSense basis after Hop 2 anomaly clamping."""
    if adsense_clamped:
        return _q(gross_total - tax_total)
    return adsense_received_usd


def _clamp_negative_bank_fee(
    *,
    delta: Decimal,
    fx_part: Decimal,
    warnings: list[dict[str, str]],
) -> _BankResidualResult:
    """Clamp a negative bank-fee residual while preserving supported FX evidence."""
    warning_message = "Bank exceeds AdSense after FX; bank fee clamped to 0"
    if fx_part > 0:
        # FIX: A positive FX variance larger than the cash delta cannot be
        # published after the fee residual clamps; cap it to the
        # evidence-backed delta so net does not drift below bank cash.
        fx_part = _q(max(delta, Decimal("0")))
        warning_message = (
            "FX exceeds AdSense-bank delta; unmatched FX suppressed and bank fee clamped to 0"
        )
    elif fx_part > delta:
        # FIX: A favorable (negative) FX variance smaller than the
        # AdSense-to-bank overage cannot be published after the fee
        # residual clamps; cap it to the cash delta so net does not
        # drift above observed bank cash.
        fx_part = _q(delta)
        warning_message = (
            "FX insufficient to explain AdSense-bank overage; "
            "unmatched FX suppressed and bank fee clamped to 0"
        )
    warnings.append(
        {
            "code": "RECONCILIATION_ANOMALY",
            "message": warning_message,
        }
    )
    return _BankResidualResult(fee_part=Decimal("0"), fx_part=fx_part)


def _suppress_zero_gross_residual(
    *,
    gross_total: Decimal,
    result: _BankResidualResult,
    warnings: list[dict[str, str]],
) -> _BankResidualResult:
    """Suppress Hop 3 totals when there is no gross basis for attribution."""
    if gross_total > 0 or (result.fee_part == 0 and result.fx_part == 0):
        return result
    warnings.append(
        {
            "code": "ZERO_GROSS_RECONCILIATION_BASIS",
            "message": (
                "Bank fee/FX evidence cannot be attributed with a zero "
                "source-backed gross basis; hop 3 totals suppressed"
            ),
        }
    )
    return _BankResidualResult(fee_part=Decimal("0"), fx_part=Decimal("0"))


def _compute_bank_residual(
    *,
    gross_total: Decimal,
    effective_adsense_usd: Decimal | None,
    bank_received_usd: Decimal | None,
    fx_total_usd: Decimal,
    warnings: list[dict[str, str]],
) -> _BankResidualResult:
    """Compute the AdSense-to-bank fee/FX residual and related warnings."""
    if effective_adsense_usd is None or bank_received_usd is None:
        if bank_received_usd is None:
            warnings.append(
                {
                    "code": "MISSING_BANK_TOTAL",
                    "message": "No bank receipt; fee/FX not derived",
                }
            )
        return _BankResidualResult(fee_part=Decimal("0"), fx_part=Decimal("0"))

    delta = effective_adsense_usd - bank_received_usd
    fx_part = _q(fx_total_usd)
    fee_part = _q(delta - fx_part)
    result = _BankResidualResult(fee_part=fee_part, fx_part=fx_part)
    if fee_part < 0:
        result = _clamp_negative_bank_fee(
            delta=delta,
            fx_part=fx_part,
            warnings=warnings,
        )
    return _suppress_zero_gross_residual(
        gross_total=gross_total,
        result=result,
        warnings=warnings,
    )


def _build_channel_reconciliations(
    *,
    gross: dict[str, Decimal],
    us_tax: dict[str, Decimal],
    yt_fee: dict[str, Decimal],
    adsense_bank_fee: dict[str, Decimal],
    fx_variance: dict[str, Decimal],
    us_view_shares: Mapping[str, Decimal | None],
) -> list[ChannelReconciliation]:
    """Build sorted per-channel reconciliation rows."""
    channels = [
        ChannelReconciliation(
            youtube_channel_id=c,
            gross_usd=_q(gross[c]),
            us_tax_usd=us_tax[c],
            yt_adsense_fee_usd=yt_fee[c],
            adsense_bank_fee_usd=adsense_bank_fee[c],
            fx_variance_usd=fx_variance[c],
            net_received_usd=_q(
                gross[c] - us_tax[c] - yt_fee[c] - adsense_bank_fee[c] - fx_variance[c]
            ),
            us_view_share=us_view_shares.get(c),
        )
        for c in gross
    ]
    channels.sort(key=lambda x: x.youtube_channel_id)
    return channels


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

    us_tax = _compute_us_tax(
        gross=gross,
        us_view_shares=us_view_shares,
        withholding_rate=withholding_rate,
        warnings=warnings,
    )

    yt_adsense_fee = _compute_yt_adsense_fee(
        gross_total=g,
        tax_total=us_tax.total,
        adsense_received_usd=adsense_received_usd,
        warnings=warnings,
    )
    yt_fee = _attribute(yt_adsense_fee.total, gross, g)

    # FIX: When Hop 2 already clamped an over-estimate AdSense receipt to a
    # zero YouTube->AdSense fee, the bank-fee residual must be derived from
    # the *effective* (clamped) AdSense basis, not the raw over-receipt.
    # Otherwise Hop 3 turns the overage into a phantom bank fee and the
    # channel net drifts below observed bank cash.
    bank_residual = _compute_bank_residual(
        gross_total=g,
        effective_adsense_usd=_effective_adsense_basis(
            gross_total=g,
            tax_total=us_tax.total,
            adsense_received_usd=adsense_received_usd,
            adsense_clamped=yt_adsense_fee.adsense_clamped,
        ),
        bank_received_usd=bank_received_usd,
        fx_total_usd=fx_total_usd,
        warnings=warnings,
    )
    adsense_bank_fee = _attribute(bank_residual.fee_part, gross, g)
    fx_variance = _attribute(bank_residual.fx_part, gross, g)
    channels = _build_channel_reconciliations(
        gross=gross,
        us_tax=us_tax.by_channel,
        yt_fee=yt_fee,
        adsense_bank_fee=adsense_bank_fee,
        fx_variance=fx_variance,
        us_view_shares=us_view_shares,
    )

    return MonthReconciliationResult(
        month=month,
        channels=channels,
        gross_total_usd=_q(g),
        us_tax_total_usd=_q(us_tax.total),
        yt_adsense_fee_total_usd=_q(yt_adsense_fee.total),
        adsense_bank_fee_total_usd=_q(bank_residual.fee_part),
        fx_total_usd=_q(bank_residual.fx_part),
        net_total_usd=_q(sum((x.net_received_usd for x in channels), Decimal("0"))),
        yt_adsense_fee_pct=yt_adsense_fee.pct,
        warnings=warnings,
    )
