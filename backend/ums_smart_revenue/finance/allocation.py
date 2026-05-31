"""Pure account-level deduction allocation (Phase 4 Spec 2b PR-1).

Distributes ACCOUNT-grain deduction components across the verified
channel↔account map by source-aligned raw-gross-proportional share. No
database access: the caller resolves every input. See
Docs/superpowers/specs/2026-05-31-spec-account-allocation-design.md.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from ums_smart_revenue.finance.deduction_components import DeductionComponent
from ums_smart_revenue.finance.net_revenue import (
    NET_APPLICABLE_COMPONENT_KINDS,
    SOURCE_SYSTEM_TO_SOURCE_KIND,
)

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


def _issue(component: DeductionComponent, code: str, detail: str) -> UnallocatedIssue:
    """Build an UnallocatedIssue from an ACCOUNT component."""
    return UnallocatedIssue(
        scope_id=component.scope_id,
        component_kind=component.component_kind,
        component_key=component.component_key,
        amount_usd=component.amount_usd,
        issue_code=code,
        detail=detail,
    )


def _proportional_allocation(
    amount: Decimal, weights: list[tuple[str, Decimal]]
) -> dict[str, Decimal]:
    """Split `amount` across (channel, basis) weights, conserving to 1e-6.

    Largest-remainder (Hamilton) apportionment: floor each share to 6dp, then
    hand the leftover micro-units to the largest fractional remainders
    (channel_id ascending as the deterministic tiebreak). Remainders are always
    non-negative fractional parts (exact - floor), so the same largest-remainder
    rule applies to negative amounts: handing those shares an extra micro-unit
    makes them less negative; ascending channel_id still breaks exact ties.

    Raises AllocationValidationError when basis_total <= 0; the caller in
    build_account_allocation guards this case before reaching here.
    Conserves exactly: sum(result.values()) == amount.
    """
    basis_total = sum((weight for _, weight in weights), Decimal("0"))
    if basis_total <= 0:
        raise AllocationValidationError(
            "basis_total must be positive for proportional allocation"
        )
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


@dataclass(frozen=True)
class AllocationLine:
    """One ACCOUNT component's allocated share for one channel."""

    adsense_account_id: str
    youtube_channel_id: str
    component_kind: str
    source_system: str
    component_key: str
    basis_source_kind: str
    basis_gross_usd: Decimal
    basis_share: Decimal
    allocated_amount_usd: Decimal
    net_applicable: bool


@dataclass(frozen=True)
class UnallocatedIssue:
    """An ACCOUNT component (or guarded non-ACCOUNT row) that was not allocated."""

    scope_id: str
    component_kind: str
    component_key: str
    amount_usd: Decimal
    issue_code: str
    detail: str


@dataclass(frozen=True)
class AllocationNote:
    """Informational, non-blocking observation."""

    note_code: str
    youtube_channel_id: str
    detail: str


@dataclass(frozen=True)
class AllocationSummary:
    """Conserved roll-up over all input components."""

    component_count: int
    allocated_component_count: int
    unallocated_component_count: int
    allocated_total_usd: Decimal
    unallocated_total_usd: Decimal
    net_applicable_total_usd: Decimal
    reconciliation_total_usd: Decimal


@dataclass(frozen=True)
class AccountAllocationResult:
    """Full allocation result for one month."""

    month: str
    allocation_method: str
    lines: tuple[AllocationLine, ...]
    unallocated: tuple[UnallocatedIssue, ...]
    notes: tuple[AllocationNote, ...]
    summary: AllocationSummary


@dataclass(frozen=True)
class _ComponentOutcome:
    """One component's allocation result: produced lines, an optional blocking
    issue, and whether it counts as allocated (a success or a zero-amount no-op)."""

    lines: tuple[AllocationLine, ...]
    issue: UnallocatedIssue | None
    allocated: bool


def _multi_account_notes(
    verified_channels: Mapping[str, Sequence[str]],
) -> list[AllocationNote]:
    """Emit one informational note per channel reachable from >1 account.

    Non-blocking: allocation still runs under each account independently; the
    note flags that the channel's gross is weighted separately per account.
    """
    accounts_by_channel: dict[str, set[str]] = {}
    for account, channels in verified_channels.items():
        for channel_id in channels:
            accounts_by_channel.setdefault(channel_id, set()).add(account)
    return [
        AllocationNote(
            note_code="CHANNEL_IN_MULTIPLE_ACCOUNTS",
            youtube_channel_id=channel_id,
            detail=f"channel reachable from {len(accounts)} accounts",
        )
        for channel_id, accounts in sorted(accounts_by_channel.items())
        if len(accounts) > 1
    ]


# ============================================================================
# Purpose: Allocate ONE ACCOUNT-grain deduction component across its verified
#   channels by source-aligned raw-gross-proportional share. Fails closed with a
#   typed UnallocatedIssue on every blocking condition (non-ACCOUNT scope,
#   unmapped account, unresolved/missing/incomplete basis, non-positive gross);
#   never substitutes a different basis. Extracted from build_account_allocation
#   so each function keeps a maintainable branching budget (DeepSource PY-R1000).
# Database/ORM: None (caller resolves the verified map and gross basis).
# Standards: Decimal arithmetic; exact per-component conservation via
#   _proportional_allocation; net_applicable from NET_APPLICABLE_COMPONENT_KINDS.
# Blast Radius: Finance read-model only. No persistence, no auth, no Neo4j.
# ============================================================================
def _allocate_component(
    component: DeductionComponent,
    verified_channels: Mapping[str, Sequence[str]],
    gross_basis: Mapping[tuple[str, str], Decimal],
) -> _ComponentOutcome:
    """Return the allocation outcome for a single component (pure compute)."""
    if component.scope_kind != "ACCOUNT":
        return _ComponentOutcome(
            (),
            UnallocatedIssue(
                scope_id=component.scope_id,
                component_kind=component.component_kind,
                component_key=component.component_key,
                amount_usd=component.amount_usd,
                issue_code="UNSUPPORTED_SCOPE",
                detail=f"scope_kind {component.scope_kind} is not allocatable",
            ),
            False,
        )

    account = component.scope_id
    amount = component.amount_usd
    if amount == 0:
        return _ComponentOutcome((), None, True)

    channels = list(verified_channels.get(account) or [])
    if not channels:
        return _ComponentOutcome(
            (),
            _issue(component, "ACCOUNT_UNMAPPED_OR_UNVERIFIED",
                   "no verified channels for account-month"),
            False,
        )

    source_kind = _basis_source_kind(component.source_system)
    if source_kind is None:
        return _ComponentOutcome(
            (),
            _issue(component, "BASIS_MISSING",
                   f"unresolved source kind for {component.source_system}"),
            False,
        )

    present = [
        (channel_id, gross_basis[(channel_id, source_kind)])
        for channel_id in channels
        if (channel_id, source_kind) in gross_basis
    ]
    if not present:
        return _ComponentOutcome(
            (),
            _issue(component, "BASIS_MISSING",
                   "no source-aligned gross for any verified channel"),
            False,
        )
    if len(present) != len(channels):
        return _ComponentOutcome(
            (),
            _issue(component, "BASIS_INCOMPLETE",
                   "some verified channels missing source-aligned gross"),
            False,
        )

    basis_total = sum((gross for _, gross in present), Decimal("0"))
    if basis_total <= 0:
        return _ComponentOutcome(
            (),
            _issue(component, "ZERO_GROSS_BASIS",
                   "verified channels have zero or negative source-aligned gross"),
            False,
        )

    net_applicable = component.component_kind in NET_APPLICABLE_COMPONENT_KINDS
    allocated = _proportional_allocation(amount, present)
    lines = tuple(
        AllocationLine(
            adsense_account_id=account,
            youtube_channel_id=channel_id,
            component_kind=component.component_kind,
            source_system=component.source_system,
            component_key=component.component_key,
            basis_source_kind=source_kind,
            basis_gross_usd=gross,
            basis_share=(gross / basis_total).quantize(_SCALE),
            allocated_amount_usd=allocated[channel_id],
            net_applicable=net_applicable,
        )
        for channel_id, gross in present
    )
    return _ComponentOutcome(lines, None, True)


# ============================================================================
# Purpose: Allocate ACCOUNT-grain deduction evidence across each account's
#   verified channels by source-aligned raw-gross-proportional share. Fails
#   closed (UNALLOCATED) on unmapped accounts or missing/incomplete basis;
#   never substitutes a different basis. Pure compute — no DB access.
# Database/ORM: None (caller resolves components, map, and gross basis).
# Standards: Decimal arithmetic; exact per-component + aggregate conservation;
#   net_applicable from net_revenue's NET_APPLICABLE_COMPONENT_KINDS.
# Blast Radius: Finance read-model only. No persistence, no net_revenue change,
#   no auth, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/net_revenue.py -> shared constants.
#   - File: backend/ums_smart_revenue/api/allocation.py -> read endpoint.
# ============================================================================
def build_account_allocation(
    *,
    month: str,
    components: Iterable[DeductionComponent],
    verified_channels: Mapping[str, Sequence[str]],
    gross_basis: Mapping[tuple[str, str], Decimal],
) -> AccountAllocationResult:
    """Compute per-channel allocation + unallocated issues for one month.

    Precondition: each verified_channels[account] is expected to contain
    DISTINCT channel ids (the Spec 2a read contract
    list_verified_adsense_account_channels guarantees this via .distinct());
    duplicates would double-count.
    """
    lines: list[AllocationLine] = []
    unallocated: list[UnallocatedIssue] = []
    notes = _multi_account_notes(verified_channels)

    component_count = 0
    allocated_component_count = 0
    for component in components:
        component_count += 1
        outcome = _allocate_component(component, verified_channels, gross_basis)
        lines.extend(outcome.lines)
        if outcome.issue is not None:
            unallocated.append(outcome.issue)
        if outcome.allocated:
            allocated_component_count += 1

    allocated_total = sum((ln.allocated_amount_usd for ln in lines), Decimal("0"))
    unallocated_total = sum((iss.amount_usd for iss in unallocated), Decimal("0"))
    net_total = sum(
        (ln.allocated_amount_usd for ln in lines if ln.net_applicable), Decimal("0")
    )
    reconciliation_total = sum(
        (ln.allocated_amount_usd for ln in lines if not ln.net_applicable), Decimal("0")
    )
    summary = AllocationSummary(
        component_count=component_count,
        allocated_component_count=allocated_component_count,
        unallocated_component_count=len(unallocated),
        allocated_total_usd=allocated_total,
        unallocated_total_usd=unallocated_total,
        net_applicable_total_usd=net_total,
        reconciliation_total_usd=reconciliation_total,
    )
    return AccountAllocationResult(
        month=month,
        allocation_method=ALLOCATION_METHOD,
        lines=tuple(lines),
        unallocated=tuple(unallocated),
        notes=tuple(notes),
        summary=summary,
    )
