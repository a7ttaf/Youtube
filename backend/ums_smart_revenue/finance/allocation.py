"""Pure account-level deduction allocation (Phase 4 Spec 2b PR-1).

Distributes ACCOUNT-grain deduction components across the verified
channel↔account map by source-aligned raw-gross-proportional share. No
database access: the caller resolves every input. See
Docs/superpowers/specs/2026-05-31-spec-account-allocation-design.md.
"""

from __future__ import annotations

from decimal import ROUND_FLOOR, Decimal

from ums_smart_revenue.finance.net_revenue import SOURCE_SYSTEM_TO_SOURCE_KIND

ALLOCATION_METHOD = "gross_revenue_proportional"
_SCALE = Decimal("0.000001")  # 6dp, matches deduction_components.amount_usd
_PAYMENT_GAP_SOURCE_SYSTEM = "adsense_payment_gap"


class AllocationError(Exception):
    """Base class for allocation errors."""


class AllocationValidationError(AllocationError):
    """Raised for malformed allocation input."""


def _basis_source_kind(source_system: str) -> str | None:
    """Resolve the raw-gross source kind that weights this component's split.

    Mirrors net_revenue's source alignment; the AdSense payment-gap source has
    no entry in the map and is special-cased to ADSENSE. Returns None when the
    source_system is unresolvable (the caller records BASIS_MISSING).
    """
    if source_system in SOURCE_SYSTEM_TO_SOURCE_KIND:
        return SOURCE_SYSTEM_TO_SOURCE_KIND[source_system]
    if source_system == _PAYMENT_GAP_SOURCE_SYSTEM:
        return "ADSENSE"
    return None


def _proportional_allocation(
    amount: Decimal, weights: list[tuple[str, Decimal]]
) -> dict[str, Decimal]:
    """Split `amount` across (channel, basis) weights, conserving to 1e-6.

    Largest-remainder (Hamilton) apportionment: floor each share to 6dp, then
    hand the leftover micro-units to the largest fractional remainders
    (channel_id ascending as the deterministic tiebreak). Requires
    basis_total > 0; conserves exactly: sum(result.values()) == amount.
    """
    basis_total = sum((weight for _, weight in weights), Decimal("0"))
    floors: dict[str, Decimal] = {}
    remainders: list[tuple[Decimal, str]] = []
    allocated = Decimal("0")
    for channel_id, weight in weights:
        exact = amount * weight / basis_total
        floor_value = exact.quantize(_SCALE, rounding=ROUND_FLOOR)
        floors[channel_id] = floor_value
        remainders.append((exact - floor_value, channel_id))
        allocated += floor_value
    leftover_units = int(((amount - allocated) / _SCALE).to_integral_value())
    order = sorted(remainders, key=lambda item: (-item[0], item[1]))
    for index in range(leftover_units):
        floors[order[index][1]] += _SCALE
    return floors
