"""Read-side resolver: prefer the committed allocation snapshot for LOCKED months.

For each reader (allocation GET, net-revenue, explain, exports) this is the single
decision point: LOCKED month -> latest committed snapshot (reconstructed losslessly);
LOCKED with no run -> live_fallback; OPEN / no close row -> live_compute. The one
tenant is taken from committed_repository.tenant_id so the close-status read and the
committed-run lookup cannot diverge cross-tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from ums_smart_revenue.finance.allocation import (
    AccountAllocationResult,
    AllocationLine,
    AllocationNote,
    AllocationSummary,
    UnallocatedIssue,
    summarize_account_allocation,
)
from ums_smart_revenue.finance.allocation_inputs import compute_month_account_allocation
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.committed_allocation import (
    CommitAllocationOutcome,
    SqlAlchemyCommittedAllocationRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.month_close import get_month_close_status
from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository


@dataclass(frozen=True)
class AllocationProvenance:
    """Where a reader's allocation numbers came from."""

    source: str  # "committed_snapshot" | "live_compute" | "live_fallback"
    commit_version: int | None = None
    committed_at: datetime | None = None
    run_id: UUID | None = None


def rebuild_result_from_run(outcome: CommitAllocationOutcome) -> AccountAllocationResult:
    """Reconstruct a full AccountAllocationResult from a committed run + children (lossless)."""
    run = outcome.run
    lines = tuple(
        AllocationLine(
            adsense_account_id=row.adsense_account_id,
            youtube_channel_id=row.youtube_channel_id,
            component_kind=row.component_kind,
            source_system=row.source_system,
            component_key=row.component_key,
            basis_source_kind=row.basis_source_kind,
            basis_amount_usd=row.basis_amount_usd,
            basis_share=row.basis_share,
            allocated_amount_usd=row.allocated_amount_usd,
            net_applicable=row.net_applicable,
        )
        for row in outcome.lines
    )
    unallocated = tuple(
        UnallocatedIssue(
            scope_id=row.scope_id,
            component_kind=row.component_kind,
            component_key=row.component_key,
            amount_usd=row.amount_usd,
            issue_code=row.issue_code,
            detail=row.detail,
        )
        for row in outcome.unallocated
    )
    notes = tuple(
        AllocationNote(
            note_code=row.note_code,
            youtube_channel_id=row.youtube_channel_id,
            detail=row.detail,
        )
        for row in outcome.notes
    )
    summary = AllocationSummary(
        component_count=run.component_count,
        allocated_component_count=run.allocated_component_count,
        unallocated_component_count=run.unallocated_component_count,
        allocated_total_usd=run.allocated_total_usd,
        unallocated_total_usd=run.unallocated_total_usd,
        net_applicable_total_usd=run.net_applicable_total_usd,
        reconciliation_total_usd=run.reconciliation_total_usd,
    )
    return AccountAllocationResult(
        month=run.month,
        allocation_method=run.allocation_method,
        lines=lines,
        unallocated=unallocated,
        notes=notes,
        summary=summary,
    )


def filter_committed_result_to_account(
    result: AccountAllocationResult, adsense_account_id: str
) -> AccountAllocationResult:
    """Scope a reconstructed result to one account, recomputing summary + dropping notes.

    Matches live single-account compute: lines for the account, unallocated issues for
    the account (scope_id), no cross-account CHANNEL_IN_MULTIPLE_ACCOUNTS notes, and a
    recomputed summary. Count fields are derived from distinct component_key over the
    filtered rows; a zero-amount no-op component leaves no row in the snapshot, so it is
    not counted (monetary totals stay exact).
    """
    lines = tuple(ln for ln in result.lines if ln.adsense_account_id == adsense_account_id)
    unallocated = tuple(iss for iss in result.unallocated if iss.scope_id == adsense_account_id)
    component_count = len(
        {ln.component_key for ln in lines} | {iss.component_key for iss in unallocated}
    )
    allocated_component_count = len({ln.component_key for ln in lines})
    summary = summarize_account_allocation(
        component_count=component_count,
        allocated_component_count=allocated_component_count,
        lines=lines,
        unallocated=unallocated,
    )
    return AccountAllocationResult(
        month=result.month,
        allocation_method=result.allocation_method,
        lines=lines,
        unallocated=unallocated,
        notes=(),
        summary=summary,
    )


def resolve_month_account_allocation(
    *,
    month: str,
    session: Session,
    deduction_repository: SqlAlchemyDeductionComponentRepository,
    revenue_repository: SqlAlchemyRevenueFactRepository,
    link_repository: SqlAlchemyChannelAccountLinkRepository,
    committed_repository: SqlAlchemyCommittedAllocationRepository,
    adsense_account_id: str | None = None,
) -> tuple[AccountAllocationResult, AllocationProvenance]:
    """Lock-aware snapshot-vs-live selection (single decision point for all readers)."""
    tenant_id = committed_repository.tenant_id
    status = get_month_close_status(session, month, tenant_id=tenant_id)
    if status == "LOCKED":
        outcome = committed_repository.get_latest_committed(month)
        if outcome is not None:
            result = rebuild_result_from_run(outcome)
            if adsense_account_id is not None:
                result = filter_committed_result_to_account(result, adsense_account_id)
            provenance = AllocationProvenance(
                source="committed_snapshot",
                commit_version=outcome.run.commit_version,
                committed_at=outcome.run.committed_at,
                run_id=outcome.run.id,
            )
            return result, provenance
        source = "live_fallback"
    else:
        source = "live_compute"
    result = compute_month_account_allocation(
        month=month,
        deduction_repository=deduction_repository,
        revenue_repository=revenue_repository,
        link_repository=link_repository,
        adsense_account_id=adsense_account_id,
    )
    return result, AllocationProvenance(source=source)


def allocation_provenance_to_api(provenance: AllocationProvenance) -> dict[str, object]:
    """Serialize provenance for API/explain JSON (committed_run is null unless snapshot)."""
    committed_run: dict[str, object] | None = None
    if provenance.source == "committed_snapshot":
        committed_run = {
            "commit_version": provenance.commit_version,
            "committed_at": (
                provenance.committed_at.isoformat() if provenance.committed_at else None
            ),
            "run_id": str(provenance.run_id) if provenance.run_id is not None else None,
        }
    return {"allocation_source": provenance.source, "committed_run": committed_run}


def account_allocation_disclosure_token(provenance: AllocationProvenance) -> str:
    """One-line human-readable export disclosure of the allocation source."""
    if provenance.source == "committed_snapshot":
        stamp = provenance.committed_at.date().isoformat() if provenance.committed_at else "?"
        return f"Account allocation: committed snapshot v{provenance.commit_version} ({stamp})"
    if provenance.source == "live_fallback":
        return "Account allocation: live fallback"
    return "Account allocation: live compute"
