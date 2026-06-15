"""Pure operator-asserted ('manual') account-level allocation (Phase 4 Spec 2b).

The operator supplies the exact per-channel split for each ACCOUNT-grain
deduction component in the request body; the proportional engine
(allocation.build_account_allocation) is bypassed entirely. Validation is
fail-closed: every ACCOUNT component in the month must be exactly and
exclusively covered by the supplied lines, and every supplied line must point at
a known component and a verified channel for that component's account. No
database access: the caller resolves the components and the verified map. See
Docs/superpowers/specs/2026-05-31-spec-account-allocation-design.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ums_smart_revenue.finance.allocation import (
    AccountAllocationResult,
    AllocationLine,
    AllocationValidationError,
    summarize_account_allocation,
)
from ums_smart_revenue.finance.deduction_components import DeductionComponent
from ums_smart_revenue.finance.deduction_policy import NET_APPLICABLE_COMPONENT_KINDS

MANUAL_ALLOCATION_METHOD = "manual"
_BASIS_SOURCE_KIND = "MANUAL"
MANUAL_AMOUNT_SCALE = Decimal("0.000001")  # 6dp, matches deduction_components.amount_usd


@dataclass(frozen=True)
class ManualAllocationInput:
    """One operator-asserted allocation line for the manual method."""

    component_key: str
    youtube_channel_id: str
    amount_usd: Decimal


# ============================================================================
# Purpose: Build the manual ('manual') account-allocation result from
#   operator-asserted per-channel lines, bypassing the proportional engine. Fails
#   closed (AllocationValidationError) on any line pointing at an unknown
#   component_key or an unverified channel, any duplicate (component_key, channel)
#   pair, any negative or over-precision amount, any per-component sum that does
#   not EXACTLY equal the component amount, any ACCOUNT component left uncovered,
#   and any non-ACCOUNT component present in the month (rejects the WHOLE request
#   so the operator never silently loses a component). The roll-up reuses the
#   shared summarize_account_allocation helper for no-drift conservation.
# Database/ORM: None (caller resolves the components and the verified map).
# Standards: Decimal exact equality for conservation; deterministic line order
#   (sorted by component_key, channel); typed AllocationValidationError boundary.
# Blast Radius: Finance read-model only. No persistence, no auth, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/allocation.py -> shared result types.
#   - File: backend/ums_smart_revenue/finance/committed_allocation.py -> commit path.
# ============================================================================
def build_manual_account_allocation(
    *,
    month: str,
    components: Iterable[DeductionComponent],
    verified_channels: Mapping[str, Sequence[str]],
    manual_lines: Sequence[ManualAllocationInput],
) -> AccountAllocationResult:
    """Compute the manual allocation result, or fail closed on any violation.

    :raises AllocationValidationError: On any of the following:
        - Any non-ACCOUNT component exists in the month.
        - A line references an unknown component_key.
        - A line's channel is not verified for the component's account.
        - A duplicate (component_key, channel) pair is supplied.
        - A line amount is out of range, negative, or has more than 6 dp.
        - Any ACCOUNT component is not covered by at least one line.
        - The per-component line sum does not exactly equal the component amount.
    """
    component_list = list(components)
    non_account = sorted(
        component.component_key for component in component_list if component.scope_kind != "ACCOUNT"
    )
    if non_account:
        # Reject the whole request: a non-ACCOUNT component cannot be covered by
        # an operator line, and silently dropping it would lose evidence.
        raise AllocationValidationError(
            "manual allocation requires ACCOUNT-only components; "
            "non-ACCOUNT component_key(s): " + ", ".join(non_account)
        )

    by_key: dict[str, DeductionComponent] = {
        component.component_key: component for component in component_list
    }
    lines_by_component = _index_and_validate_lines(
        manual_lines=manual_lines,
        by_key=by_key,
        verified_channels=verified_channels,
    )

    uncovered = sorted(key for key in by_key if key not in lines_by_component)
    if uncovered:
        raise AllocationValidationError(
            "every ACCOUNT component must be covered by a manual line; "
            "uncovered component_key(s): " + ", ".join(uncovered)
        )

    for component_key, component in by_key.items():
        total = sum(
            (line.amount_usd for line in lines_by_component[component_key]),
            Decimal("0"),
        )
        if total != component.amount_usd:
            raise AllocationValidationError(
                f"manual lines for component_key {component_key} must sum to "
                f"{component.amount_usd} exactly; got {total}"
            )

    allocation_lines = _build_lines(by_key, lines_by_component)
    return AccountAllocationResult(
        month=month,
        allocation_method=MANUAL_ALLOCATION_METHOD,
        lines=allocation_lines,
        unallocated=(),
        notes=(),
        summary=summarize_account_allocation(
            component_count=len(by_key),
            allocated_component_count=len(by_key),
            lines=allocation_lines,
            unallocated=(),
        ),
    )


def _index_and_validate_lines(
    *,
    manual_lines: Sequence[ManualAllocationInput],
    by_key: Mapping[str, DeductionComponent],
    verified_channels: Mapping[str, Sequence[str]],
) -> dict[str, list[ManualAllocationInput]]:
    """Validate each line and index them by component_key.

    :raises AllocationValidationError: On unknown component_key, unverified
        channel, duplicate (component_key, channel) pair, out-of-range amount,
        or over-precision amount (> 6 decimal places).
    """
    lines_by_component: dict[str, list[ManualAllocationInput]] = {}
    seen_pairs: set[tuple[str, str]] = set()
    for line in manual_lines:
        component = by_key.get(line.component_key)
        if component is None:
            raise AllocationValidationError(
                f"manual line references unknown component_key: {line.component_key}"
            )
        verified = verified_channels.get(component.scope_id) or ()
        if line.youtube_channel_id not in verified:
            raise AllocationValidationError(
                f"channel {line.youtube_channel_id} is not verified for account "
                f"{component.scope_id} (component_key {line.component_key})"
            )
        pair = (line.component_key, line.youtube_channel_id)
        if pair in seen_pairs:
            raise AllocationValidationError(
                "duplicate manual line for "
                f"(component_key {line.component_key}, "
                f"channel {line.youtube_channel_id})"
            )
        seen_pairs.add(pair)
        # FIX: quantize() raised an uncaught decimal.InvalidOperation for a
        # schema-valid but out-of-range magnitude (e.g. "1e1000"), escaping the
        # typed-error chain as a 500; catch it and fail closed as 422.
        try:
            quantized = line.amount_usd.quantize(MANUAL_AMOUNT_SCALE)
        except InvalidOperation as exc:
            raise AllocationValidationError(
                f"manual line amount for channel {line.youtube_channel_id} "
                f"(component_key {line.component_key}) is out of range"
            ) from exc
        if line.amount_usd < 0 or line.amount_usd != quantized:
            raise AllocationValidationError(
                f"manual line amount for channel {line.youtube_channel_id} "
                f"(component_key {line.component_key}) must be >= 0 and "
                "quantized to <= 6 decimal places"
            )
        lines_by_component.setdefault(line.component_key, []).append(line)
    return lines_by_component


def _build_lines(
    by_key: Mapping[str, DeductionComponent],
    lines_by_component: Mapping[str, list[ManualAllocationInput]],
) -> tuple[AllocationLine, ...]:
    """Materialize validated manual lines, sorted by (component_key, channel)."""
    flat = [
        (line, by_key[component_key])
        for component_key, component_lines in lines_by_component.items()
        for line in component_lines
    ]
    flat.sort(key=lambda item: (item[0].component_key, item[0].youtube_channel_id))
    return tuple(
        AllocationLine(
            adsense_account_id=component.scope_id,
            youtube_channel_id=line.youtube_channel_id,
            component_kind=component.component_kind,
            source_system=component.source_system,
            component_key=component.component_key,
            basis_source_kind=_BASIS_SOURCE_KIND,
            basis_amount_usd=line.amount_usd,
            basis_share=(
                (line.amount_usd / component.amount_usd).quantize(MANUAL_AMOUNT_SCALE)
                if component.amount_usd != 0
                else Decimal("0")
            ),
            allocated_amount_usd=line.amount_usd,
            net_applicable=component.component_kind in NET_APPLICABLE_COMPONENT_KINDS,
        )
        for line, component in flat
    )
