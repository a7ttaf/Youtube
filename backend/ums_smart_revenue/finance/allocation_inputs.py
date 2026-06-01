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
    AccountAllocationResult,
    build_account_allocation,
)
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository


# ============================================================================
# Purpose: Resolve ACCOUNT components, the source-aligned (channel, source_kind)
#   raw-gross basis, and the verified account->channels map for a month, then
#   run build_account_allocation. Single source of allocation orchestration.
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
) -> AccountAllocationResult:
    """Gather inputs and run the account allocation for one finance month."""
    components = deduction_repository.list_account_components(
        month=month, adsense_account_id=adsense_account_id
    )
    facts = revenue_repository.list_month_facts(month=month)
    gross_basis: dict[tuple[str, str], Decimal] = {}
    for fact in facts:
        key = (fact.youtube_channel_id, fact.source_kind)
        gross_basis[key] = gross_basis.get(key, Decimal("0")) + fact.gross_revenue_usd
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
        gross_basis=gross_basis,
    )
