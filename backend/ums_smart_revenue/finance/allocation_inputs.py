"""Shared account-allocation input orchestrator (Phase 4 Spec 2b PR-2).

Gathers the inputs build_account_allocation needs (ACCOUNT components,
source-aligned gross basis, verified channel map) from repositories, so the
allocation endpoint, the net-revenue route, and the finance-export path share
exactly one allocation path. Pure orchestration over repositories: no auth, no
audit, no writes.
"""

from decimal import Decimal
from uuid import UUID

from ums_smart_revenue.finance.allocation import (
    ALLOCATION_METHOD,
    POST_TAX_ALLOCATION_METHOD,
    AccountAllocationResult,
    build_account_allocation,
)
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.revenue_facts import (
    RevenueFactEntry,
    SqlAlchemyRevenueFactRepository,
)


def _build_gross_basis(
    facts: list[RevenueFactEntry],
) -> dict[tuple[str, str], Decimal]:
    """Sum gross_revenue_usd per (channel, source_kind)."""
    basis: dict[tuple[str, str], Decimal] = {}
    for fact in facts:
        key = (fact.youtube_channel_id, fact.source_kind)
        basis[key] = basis.get(key, Decimal("0")) + fact.gross_revenue_usd
    return basis


def _build_net_basis(
    facts: list[RevenueFactEntry],
) -> dict[tuple[str, str], Decimal]:
    """Sum source net_revenue_usd per (channel, source_kind), fail-closed.

    A (channel, source_kind) key is OMITTED entirely if ANY fact in that group
    has null net_revenue_usd -- never a silent partial-net sum. The downstream
    engine then treats those channels as missing basis (BASIS_MISSING/INCOMPLETE).
    Uses source net only; never derived/allocated net (which would be circular).
    """
    null_net_keys: set[tuple[str, str]] = set()
    net_basis: dict[tuple[str, str], Decimal] = {}
    for fact in facts:
        key = (fact.youtube_channel_id, fact.source_kind)
        if fact.net_revenue_usd is None:
            null_net_keys.add(key)
        else:
            net_basis[key] = net_basis.get(key, Decimal("0")) + fact.net_revenue_usd
    for key in null_net_keys:
        net_basis.pop(key, None)
    return net_basis


# ============================================================================
# Purpose: Resolve ACCOUNT components, the source-aligned (channel, source_kind)
#   basis (gross or post-tax net), and the verified account->channels map for a
#   month, then run build_account_allocation. Single source of allocation
#   orchestration.
# Database/ORM: Reads via the three injected repositories only.
# Standards: pure orchestration; no auth, no audit, no writes; deterministic.
# Blast Radius: Finance read-model (account allocation). No mutation, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/api/allocation.py -> account-allocations endpoint.
#   - File: backend/ums_smart_revenue/api/revenue.py -> net-revenue route.
#   - File: backend/ums_smart_revenue/api/exports.py -> finance export source summaries.
# ============================================================================
def compute_month_account_allocation(
    *,
    month: str,
    deduction_repository: SqlAlchemyDeductionComponentRepository,
    revenue_repository: SqlAlchemyRevenueFactRepository,
    link_repository: SqlAlchemyChannelAccountLinkRepository,
    adsense_account_id: str | None = None,
    allocation_method: str = ALLOCATION_METHOD,
) -> AccountAllocationResult:
    """Gather inputs and run the account allocation for one finance month."""
    components = deduction_repository.list_account_components(
        month=month, adsense_account_id=adsense_account_id
    )
    facts = revenue_repository.list_month_facts(month=month)
    if allocation_method == POST_TAX_ALLOCATION_METHOD:
        basis = _build_net_basis(facts)
    else:
        basis = _build_gross_basis(facts)
    tenant_id: UUID = link_repository.tenant_id
    accounts = sorted({component.scope_id for component in components})
    verified_channels = {
        account: link_repository.list_verified_adsense_account_channels(
            tenant_id=tenant_id, month=month, adsense_account_id=account
        )
        for account in accounts
    }
    return build_account_allocation(
        month=month,
        components=components,
        verified_channels=verified_channels,
        basis=basis,
        allocation_method=allocation_method,
    )
