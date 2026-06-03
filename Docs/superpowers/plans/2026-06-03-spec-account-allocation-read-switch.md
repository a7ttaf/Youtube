# Account-Allocation Read-Switch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the four account-allocation readers prefer the committed snapshot (PR-5) for LOCKED months via one central resolver, with lossless reconstruction, account-scoped recompute, and full source provenance on every surface.

**Architecture:** A new read-side resolver `resolve_month_account_allocation` owns the lock-aware policy (LOCKED → latest committed snapshot, else `live_fallback`; OPEN/none → `live_compute`), rebuilds an `AccountAllocationResult` from the committed run, and returns provenance. All four readers swap their direct `compute_month_account_allocation` call for the resolver. No DB/schema/auth/write-path change.

**Tech Stack:** Python 3 / FastAPI / SQLAlchemy 2.x / PostgreSQL (SQLite for unit tests) / pytest / ruff.

**Branch:** `spec/account-allocation-read-switch` (off `main` `7c06670`). **Spec:** `Docs/superpowers/specs/2026-06-03-spec-account-allocation-read-switch-design.md`.

**Hard constraints:** Strict TDD per task (failing test → run-to-fail → minimal impl → run-to-pass → commit). Every commit message is **trailer-free** (no `Co-Authored-By`, no Generated footer). Use `python -m pytest` (not bare `pytest`). PG-tier/full-suite runs need `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:ums@localhost:55432/test_ums` (container `ums-mig-pg-test`). Do NOT push, open a PR, or merge. Do NOT use `git checkout`/`restore`/`reset` on files. **No migration.**

---

## File Structure

- **Create** `backend/ums_smart_revenue/finance/account_allocation_read.py` — the resolver, `AllocationProvenance`, `rebuild_result_from_run`, `filter_committed_result_to_account`, `allocation_provenance_to_api`, `account_allocation_disclosure_token`.
- **Modify** `backend/ums_smart_revenue/finance/committed_allocation.py` — public `tenant_id` property + `get_latest_committed`.
- **Modify** `backend/ums_smart_revenue/finance/allocation.py` — extract `summarize_account_allocation` (live path byte-identical).
- **Modify** `backend/ums_smart_revenue/api/revenue.py` — host the relocated `current_committed_allocation_repository`; wire net-revenue GET; wire explain.
- **Modify** `backend/ums_smart_revenue/api/allocation.py` — import the provider from `revenue.py`; wire allocation GET.
- **Modify** `backend/ums_smart_revenue/api/exports.py` — resolver in the source-bundle builder; bundle provenance field; thread to builders.
- **Modify** `backend/ums_smart_revenue/finance/explanations.py` — thread provenance into the net explanation account component.
- **Modify** `backend/ums_smart_revenue/reports/{finance_workbook,executive_pdf,branded_slide_pack}.py` — store provenance on the report objects + render a disclosure token.
- **Tests:** create `tests/finance/test_account_allocation_read.py`; extend `tests/api/test_allocation_api.py`, `tests/api/test_net_revenue_api.py`, `tests/api/test_exports_account_allocation.py`, and the explain/export test modules.

---

## Task 1: Committed-repo read accessors

**Files:**
- Modify: `backend/ums_smart_revenue/finance/committed_allocation.py` (class `SqlAlchemyCommittedAllocationRepository`, after `__init__` at `:85` and after `get_run_by_idempotency_key` at `:244`)
- Test: `tests/finance/test_committed_allocation.py` (existing module — add cases)

- [ ] **Step 1: Write the failing tests**

Append to `tests/finance/test_committed_allocation.py` (reuses the existing `_session`, `_seed_account_deduction`, `_repos`, `_commit`, `TENANT`, `MONTH` helpers):

```python
def test_tenant_id_property_exposes_resolved_tenant(tmp_path):
    """The repo exposes its resolved tenant for the read-switch single-tenant source."""
    session = _session(tmp_path)
    committed, _ded, _rev, _link = _repos(session)
    assert committed.tenant_id == TENANT


def test_get_latest_committed_returns_run_with_children(tmp_path):
    """get_latest_committed returns the latest run + its child rows, or None."""
    session = _session(tmp_path)
    assert _repos(session)[0].get_latest_committed(MONTH) is None  # nothing committed yet
    _seed_account_deduction(session, mapped=True)
    committed, ded, rev, link = _repos(session)
    outcome = _commit(committed, ded, rev, link)
    latest = committed.get_latest_committed(MONTH)
    assert latest is not None
    assert latest.run.id == outcome.run.id
    assert latest.run.commit_version == 1
    assert len(latest.lines) == 1
    assert latest.lines[0].youtube_channel_id == "chA"


def test_get_latest_committed_returns_highest_version(tmp_path):
    """With two commits, get_latest_committed returns the highest commit_version."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True)
    committed, ded, rev, link = _repos(session)
    _commit(committed, ded, rev, link, key="k1", fp="f1")
    second = _commit(committed, ded, rev, link, key="k2", fp="f2")
    latest = committed.get_latest_committed(MONTH)
    assert latest.run.id == second.run.id
    assert latest.run.commit_version == 2
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/finance/test_committed_allocation.py -q`
Expected: FAIL — `AttributeError: 'SqlAlchemyCommittedAllocationRepository' object has no attribute 'tenant_id'` / `... 'get_latest_committed'`.

- [ ] **Step 3: Add the accessors**

In `backend/ums_smart_revenue/finance/committed_allocation.py`, immediately after `__init__` (`:85-87`) add the property, and immediately after `get_run_by_idempotency_key` (ends `:254`) add `get_latest_committed`:

```python
    @property
    def tenant_id(self) -> UUID:
        """The tenant UUID this repository is scoped to (read-only)."""
        return self._tenant_id
```

```python
    def get_latest_committed(self, month: str) -> CommitAllocationOutcome | None:
        """Return the highest-version committed run + its child rows for a month, or None."""
        run = self.get_latest_run(month)
        return None if run is None else self._replay(run)
```

(`_replay` already loads lines/unallocated/notes and returns a `CommitAllocationOutcome`; its `created=False` flag is irrelevant for reads. `UUID` is already imported.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/finance/test_committed_allocation.py -q`
Expected: PASS (existing cases + 3 new).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/committed_allocation.py tests/finance/test_committed_allocation.py
git commit -m "feat(finance): committed-allocation read accessors (tenant_id + get_latest_committed)"
```

---

## Task 2: `summarize_account_allocation` extraction + the read resolver

**Files:**
- Modify: `backend/ums_smart_revenue/finance/allocation.py` (`build_account_allocation` `:311-364`)
- Create: `backend/ums_smart_revenue/finance/account_allocation_read.py`
- Test: `tests/finance/test_account_allocation_read.py`

- [ ] **Step 1: Write the failing resolver tests**

Create `tests/finance/test_account_allocation_read.py`:

```python
"""Read-switch resolver: lock-aware snapshot-vs-live selection + reconstruction."""
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.finance.account_allocation_read import (
    AllocationProvenance,
    account_allocation_disclosure_token,
    allocation_provenance_to_api,
    resolve_month_account_allocation,
)
from ums_smart_revenue.finance.allocation_inputs import compute_month_account_allocation
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.committed_allocation import (
    SqlAlchemyCommittedAllocationRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

TENANT = UUID(UMS_TENANT_ID)
MONTH = "2026-04"
ACTOR = str(TENANT)


def _session(tmp_path) -> Session:
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    TenantBase.metadata.create_all(engine)
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    session = Session(engine)
    session.add(TenantORM(
        id=TENANT, slug="ums", display_name="UMS", primary_currency="USD", status="ACTIVE",
    ))
    session.commit()
    return session


def _add_account(session, *, account, channel, gross, deduction, mapped):
    """Seed one ACCOUNT deduction over one channel (ADSENSE gross), optionally mapped."""
    session.add(YouTubeChannelORM(
        id=uuid4(), tenant_id=TENANT, youtube_channel_id=channel, channel_name=channel, active=True,
    ))
    session.flush()  # cross-registry composite FK: channel before the dependent fact
    session.add(MonthlyChannelRevenueFactORM(
        id=uuid4(), tenant_id=TENANT, month=MONTH, youtube_channel_id=channel,
        source_kind="ADSENSE", gross_revenue_usd=Decimal(gross), net_revenue_usd=None,
    ))
    session.add(DeductionComponentORM(
        id=uuid4(), tenant_id=TENANT, month=MONTH, component_kind="DEDUCTION",
        scope_kind="ACCOUNT", scope_id=account, amount_usd=Decimal(deduction),
        currency_code="USD", source_system="adsense_management",
        source_table="google_revenue_source_rows", component_key=f"ad-{account}",
        raw_payload={},
    ))
    if mapped:
        owner = f"owner-{account}"
        session.add(AdsenseContentOwnerLinkORM(
            id=uuid4(), tenant_id=TENANT, adsense_account_id=account, content_owner_id=owner,
            verification_status="VERIFIED", provenance_kind="OPERATOR_ASSERTED",
            provenance_payload={}, effective_month_start="2026-01",
        ))
        session.add(ContentOwnerChannelLinkORM(
            id=uuid4(), tenant_id=TENANT, content_owner_id=owner, youtube_channel_id=channel,
            provenance_kind="SOURCE_ROW", active=True, effective_month_start="2026-01",
        ))


def _close(session, status):
    session.add(FinanceMonthCloseORM(
        tenant_id=TENANT, month=MONTH, status=status, allocation_rule_payload={},
    ))
    session.commit()


def _repos(session):
    return (
        SqlAlchemyCommittedAllocationRepository(session),
        SqlAlchemyDeductionComponentRepository(session),
        SqlAlchemyRevenueFactRepository(session),
        SqlAlchemyChannelAccountLinkRepository(session),
    )


def _commit(session, *, status_after="OPEN"):
    """Seed one mapped account, commit a snapshot, then set the close status."""
    _add_account(session, account="pub-1", channel="chA", gross="1000.00", deduction="100.00", mapped=True)
    session.commit()
    committed, ded, rev, link = _repos(session)
    committed.commit_allocation(
        month=MONTH, allocation_method="gross_revenue_proportional",
        idempotency_key="k1", request_fingerprint="fp1", reason="close",
        committed_by=ACTOR, deduction_repository=ded, revenue_repository=rev,
        link_repository=link,
    )
    _close(session, status_after)


def _resolve(session, *, adsense_account_id=None):
    committed, ded, rev, link = _repos(session)
    return resolve_month_account_allocation(
        month=MONTH, session=session, deduction_repository=ded, revenue_repository=rev,
        link_repository=link, committed_repository=committed,
        adsense_account_id=adsense_account_id,
    )


def test_locked_with_snapshot_uses_committed(tmp_path):
    """LOCKED month with a committed run -> committed_snapshot provenance + snapshot lines."""
    session = _session(tmp_path)
    _commit(session, status_after="LOCKED")
    result, prov = _resolve(session)
    assert prov.source == "committed_snapshot"
    assert prov.commit_version == 1
    assert prov.run_id is not None
    assert len(result.lines) == 1
    assert result.lines[0].youtube_channel_id == "chA"
    assert result.summary.allocated_total_usd == Decimal("100.000000")


def test_open_month_uses_live_compute(tmp_path):
    """OPEN month -> live_compute even when a snapshot exists."""
    session = _session(tmp_path)
    _commit(session, status_after="OPEN")
    _result, prov = _resolve(session)
    assert prov.source == "live_compute"
    assert prov.commit_version is None


def test_no_close_row_uses_live_compute(tmp_path):
    """No close row -> treated as open -> live_compute."""
    session = _session(tmp_path)
    _add_account(session, account="pub-1", channel="chA", gross="1000.00", deduction="100.00", mapped=True)
    session.commit()
    _result, prov = _resolve(session)
    assert prov.source == "live_compute"


def test_locked_without_snapshot_falls_back_to_live(tmp_path):
    """LOCKED month with no committed run -> live_fallback (never errors)."""
    session = _session(tmp_path)
    _add_account(session, account="pub-1", channel="chA", gross="1000.00", deduction="100.00", mapped=True)
    session.commit()
    _close(session, "LOCKED")
    result, prov = _resolve(session)
    assert prov.source == "live_fallback"
    assert len(result.lines) == 1


def test_reconstruction_equals_live_for_locked(tmp_path):
    """Rebuilt snapshot result equals the live result for the same frozen inputs."""
    session = _session(tmp_path)
    _commit(session, status_after="LOCKED")
    snap, _prov = _resolve(session)
    committed, ded, rev, link = _repos(session)
    live = compute_month_account_allocation(
        month=MONTH, deduction_repository=ded, revenue_repository=rev, link_repository=link,
    )
    assert snap.lines == live.lines
    assert snap.unallocated == live.unallocated
    assert snap.summary == live.summary


def test_account_filter_matches_live_per_account(tmp_path):
    """LOCKED snapshot filtered to one account == live compute filtered to that account."""
    session = _session(tmp_path)
    _add_account(session, account="pub-1", channel="chA", gross="1000.00", deduction="100.00", mapped=True)
    _add_account(session, account="pub-2", channel="chB", gross="500.00", deduction="40.00", mapped=True)
    session.commit()
    committed, ded, rev, link = _repos(session)
    committed.commit_allocation(
        month=MONTH, allocation_method="gross_revenue_proportional",
        idempotency_key="k1", request_fingerprint="fp1", reason="close",
        committed_by=ACTOR, deduction_repository=ded, revenue_repository=rev, link_repository=link,
    )
    _close(session, "LOCKED")
    for account in ("pub-1", "pub-2"):
        snap, prov = _resolve(session, adsense_account_id=account)
        live = compute_month_account_allocation(
            month=MONTH, deduction_repository=ded, revenue_repository=rev,
            link_repository=link, adsense_account_id=account,
        )
        assert prov.source == "committed_snapshot"
        assert snap.lines == live.lines
        assert snap.unallocated == live.unallocated
        assert snap.notes == live.notes == ()
        assert snap.summary == live.summary


def test_provenance_api_and_token(tmp_path):
    """allocation_provenance_to_api + disclosure token render committed vs live."""
    committed = AllocationProvenance(
        source="committed_snapshot", commit_version=3,
        committed_at=None, run_id=UUID(int=1),
    )
    api = allocation_provenance_to_api(committed)
    assert api["allocation_source"] == "committed_snapshot"
    assert api["committed_run"]["commit_version"] == 3
    assert allocation_provenance_to_api(AllocationProvenance(source="live_compute"))["committed_run"] is None
    assert account_allocation_disclosure_token(AllocationProvenance(source="live_compute")) == (
        "Account allocation: live compute"
    )
    assert "committed snapshot v3" in account_allocation_disclosure_token(committed)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/finance/test_account_allocation_read.py -q`
Expected: FAIL at import — `cannot import name 'resolve_month_account_allocation' from 'ums_smart_revenue.finance.account_allocation_read'` (module does not exist).

- [ ] **Step 3a: Extract `summarize_account_allocation` in `allocation.py`**

In `backend/ums_smart_revenue/finance/allocation.py`, add (near the other module helpers, before `build_account_allocation`; `Decimal` is already imported, add `Sequence` to the existing `from collections.abc import ...` / `from typing import ...` import if not present):

```python
def summarize_account_allocation(
    *,
    component_count: int,
    allocated_component_count: int,
    lines: Sequence[AllocationLine],
    unallocated: Sequence[UnallocatedIssue],
) -> AllocationSummary:
    """Conserved roll-up over a set of allocation lines + unallocated issues."""
    allocated_total = sum((ln.allocated_amount_usd for ln in lines), Decimal("0"))
    unallocated_total = sum((iss.amount_usd for iss in unallocated), Decimal("0"))
    net_total = sum(
        (ln.allocated_amount_usd for ln in lines if ln.net_applicable), Decimal("0")
    )
    reconciliation_total = sum(
        (ln.allocated_amount_usd for ln in lines if not ln.net_applicable), Decimal("0")
    )
    return AllocationSummary(
        component_count=component_count,
        allocated_component_count=allocated_component_count,
        unallocated_component_count=len(unallocated),
        allocated_total_usd=allocated_total,
        unallocated_total_usd=unallocated_total,
        net_applicable_total_usd=net_total,
        reconciliation_total_usd=reconciliation_total,
    )
```

Replace the inline summary block in `build_account_allocation` (`allocation.py:340-356`) with a call (byte-identical arithmetic):

```python
    summary = summarize_account_allocation(
        component_count=component_count,
        allocated_component_count=allocated_component_count,
        lines=lines,
        unallocated=unallocated,
    )
```

- [ ] **Step 3b: Create the resolver module**

Create `backend/ums_smart_revenue/finance/account_allocation_read.py`:

```python
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
            basis_gross_usd=row.basis_gross_usd,
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
    unallocated = tuple(
        iss for iss in result.unallocated if iss.scope_id == adsense_account_id
    )
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
        result = compute_month_account_allocation(
            month=month, deduction_repository=deduction_repository,
            revenue_repository=revenue_repository, link_repository=link_repository,
            adsense_account_id=adsense_account_id,
        )
        return result, AllocationProvenance(source="live_fallback")
    result = compute_month_account_allocation(
        month=month, deduction_repository=deduction_repository,
        revenue_repository=revenue_repository, link_repository=link_repository,
        adsense_account_id=adsense_account_id,
    )
    return result, AllocationProvenance(source="live_compute")


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
        return "Account allocation: live (fallback — no committed snapshot)"
    return "Account allocation: live compute"
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/finance/test_account_allocation_read.py tests/finance/test_account_allocation.py -q`
(Substitute the existing allocation test module name if different — confirm `build_account_allocation` tests stay green after the extraction.)
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/allocation.py backend/ums_smart_revenue/finance/account_allocation_read.py tests/finance/test_account_allocation_read.py
git commit -m "feat(finance): read-switch resolver + summarize_account_allocation extraction"
```

---

## Task 3: Relocate the committed-repo provider + wire allocation GET and net-revenue GET

**Files:**
- Modify: `backend/ums_smart_revenue/api/revenue.py` (provider block ends `:376`; net-revenue route `:1130-1198`)
- Modify: `backend/ums_smart_revenue/api/allocation.py` (imports `:24-27`, provider `:54-58`, GET route `:221-283`, POST commit DI `:330-332`)
- Test: `tests/api/test_allocation_api.py`, `tests/api/test_net_revenue_api.py`

- [ ] **Step 1: Write failing reader tests**

In `tests/api/test_net_revenue_api.py` (reuses its `build_database_url`/`seed_database`/`create_app`/`_company_finance_principal` helpers; add a `FinanceMonthCloseORM` import + a small lock helper), add:

```python
def _lock_month(database_url: str, month: str) -> None:
    """Mark a finance month LOCKED so readers prefer the committed snapshot."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from ums_smart_revenue.db.finance_models import FinanceMonthCloseORM
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(FinanceMonthCloseORM(month=month, status="LOCKED", allocation_rule_payload={}))
        session.commit()


def test_net_revenue_open_month_reports_live_provenance(tmp_path):
    """An OPEN month serves live compute and discloses allocation_source=live_compute."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _company_finance_principal
    client = TestClient(app)
    resp = client.get(f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}")
    assert resp.status_code == 200
    assert resp.json()["allocation_source"] == "live_compute"
    assert resp.json()["committed_run"] is None
```

In `tests/api/test_allocation_api.py`, add a test that a LOCKED month with a committed snapshot reports `allocation_source == "committed_snapshot"` with a `committed_run.commit_version` (seed a mapped month, POST the commit route, lock the month, then GET the allocation endpoint; reuse that module's existing app/seed/principal/commit helpers).

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/api/test_net_revenue_api.py::test_net_revenue_open_month_reports_live_provenance -v`
Expected: FAIL — `KeyError: 'allocation_source'` (response has no provenance yet).

- [ ] **Step 3a: Relocate the provider into `revenue.py`**

In `backend/ums_smart_revenue/api/revenue.py`, add the import (top, beside the other finance repo imports) and the provider immediately after `current_channel_account_link_repository` (`:376`):

```python
from ums_smart_revenue.finance.committed_allocation import (
    SqlAlchemyCommittedAllocationRepository,
)
```

```python
def current_committed_allocation_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyCommittedAllocationRepository:
    """Build the committed-allocation repository bound to the request session."""
    return SqlAlchemyCommittedAllocationRepository(session)
```

In `backend/ums_smart_revenue/api/allocation.py`, **delete** the local provider (`:54-58`) and import it from `revenue.py` instead — extend the existing `from ums_smart_revenue.api.revenue import (...)` block (`:24-27`):

```python
from ums_smart_revenue.api.revenue import (
    current_committed_allocation_repository,
    current_deduction_component_repository,
    current_revenue_fact_repository,
)
```

The POST commit route's `Depends(current_committed_allocation_repository)` (`:332`) now resolves the imported symbol — no functional change. (Keep the `from ums_smart_revenue.finance.committed_allocation import (...)` block in `allocation.py` for the typed errors/types it still uses.)

- [ ] **Step 3b: Wire allocation GET to the resolver**

In `backend/ums_smart_revenue/api/allocation.py`, add the resolver + provider imports:

```python
from ums_smart_revenue.api.dependencies import current_db_session  # if not already imported
from ums_smart_revenue.finance.account_allocation_read import (
    allocation_provenance_to_api,
    resolve_month_account_allocation,
)
```

Add two DI params to `get_account_allocations` (`:222-238`): `session: Annotated[Session, Depends(current_db_session)]` and `committed_repository: Annotated[SqlAlchemyCommittedAllocationRepository, Depends(current_committed_allocation_repository)]`. Replace the `compute_month_account_allocation(...)` block (`:248-254`) with:

```python
    result, allocation_provenance = resolve_month_account_allocation(
        month=month,
        session=session,
        deduction_repository=deduction_repository,
        revenue_repository=revenue_repository,
        link_repository=link_repository,
        committed_repository=committed_repository,
        adsense_account_id=adsense_account_id,
    )
```

Replace the final assembly (`:281-283`) with:

```python
    payload = _result_to_api(result)
    payload.update(allocation_provenance_to_api(allocation_provenance))
    payload["audit_events"] = audit_events
    return payload
```

- [ ] **Step 3c: Wire net-revenue GET to the resolver**

In `backend/ums_smart_revenue/api/revenue.py`, import the resolver:

```python
from ums_smart_revenue.finance.account_allocation_read import (
    allocation_provenance_to_api,
    resolve_month_account_allocation,
)
```

Add to the net-revenue route signature: `session: Annotated[Session, Depends(current_db_session)]` and `committed_repository: Annotated[SqlAlchemyCommittedAllocationRepository, Depends(current_committed_allocation_repository)]`. Replace the `compute_month_account_allocation(...)` block (`:1130-1135`) with:

```python
        account_result, allocation_provenance = resolve_month_account_allocation(
            month=month,
            session=session,
            deduction_repository=deduction_component_repository,
            revenue_repository=revenue_repository,
            link_repository=link_repository,
            committed_repository=committed_repository,
        )
```

(The `filter_account_allocations_to_scope(account_result.lines, channel_ids)` and `build_month_net_revenue_summary(...)` calls are unchanged.) After `summary_api = summary.to_api()` (`:1166`) add:

```python
    summary_api.update(allocation_provenance_to_api(allocation_provenance))
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/api/test_allocation_api.py tests/api/test_net_revenue_api.py -q`
Expected: PASS (existing + new). The existing PR-5 OPEN reader-untouched regression in `test_committed_allocation_api.py` still passes (OPEN → live).

Run: `python -m pytest tests/api/test_committed_allocation_api.py -q`
Expected: PASS (unchanged).

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/api/revenue.py backend/ums_smart_revenue/api/allocation.py tests/api/test_allocation_api.py tests/api/test_net_revenue_api.py
git commit -m "feat(api): read-switch wiring for allocation GET + net-revenue (provider relocated to revenue.py)"
```

---

## Task 4: Explain endpoint provenance

**Files:**
- Modify: `backend/ums_smart_revenue/api/revenue.py` (explain route `:1407-1423`)
- Modify: `backend/ums_smart_revenue/finance/explanations.py` (`build_channel_month_revenue_explanation` `:140`, `_build_net_revenue_explanation` `:229`, account component dict `:323-340`)
- Test: the explain test module (e.g. `tests/api/test_revenue_explain_api.py` — use the actual current path) and/or `tests/finance/test_explanations.py`

- [ ] **Step 1: Write the failing test**

Add a test asserting that for a **LOCKED** month a net explanation's persisted account component carries `allocation_source == "committed_snapshot"` and a non-null `committed_run`, and for an OPEN month it is `live_compute` with `committed_run is None`. Seed a mapped channel, commit a snapshot, lock the month, POST `/revenue/channels/{channel}/months/{month}/explain?metric=net_revenue_usd`, and read back the persisted explanation's `components` (find the dict with `key == "account_allocated_deduction_usd"`). Reuse the explain module's existing app/seed/principal helpers.

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest <explain test module> -q`
Expected: FAIL — the account component dict has no `allocation_source` key.

- [ ] **Step 3a: Thread provenance through the explanation builders**

In `backend/ums_smart_revenue/finance/explanations.py`, import the serializer:

```python
from ums_smart_revenue.finance.account_allocation_read import (
    AllocationProvenance,
    allocation_provenance_to_api,
)
```

Add `account_allocation_provenance: AllocationProvenance | None = None` to `build_channel_month_revenue_explanation` (`:140-149`) and pass it into `_build_net_revenue_explanation(...)` (`:152-157`); add the same parameter to `_build_net_revenue_explanation` (`:229-236`). In the `account_allocated_deduction_usd` component dict (`:323-340`), add the two keys (spread the serialized provenance when present):

```python
        account_component = {
            "key": "account_allocated_deduction_usd",
            "label": "Account-allocated deductions",
            "value": _decimal_to_api(summary.account_allocated_deduction_amount_usd),
            "count": len(account_allocated),
            "allocations": [
                {
                    "adsense_account_id": line.adsense_account_id,
                    "component_kind": line.component_kind,
                    "source_system": line.source_system,
                    "component_key": line.component_key,
                    "basis_source_kind": line.basis_source_kind,
                    "basis_share": _decimal_to_api(line.basis_share),
                    "allocated_amount_usd": _decimal_to_api(line.allocated_amount_usd),
                }
                for line in account_allocated
            ],
        }
        if account_allocation_provenance is not None:
            account_component.update(allocation_provenance_to_api(account_allocation_provenance))
        components.append(account_component)
```

- [ ] **Step 3b: Wire the explain route**

In `backend/ums_smart_revenue/api/revenue.py` explain route, add the DI params `session: Annotated[Session, Depends(current_db_session)]` and `committed_repository: Annotated[SqlAlchemyCommittedAllocationRepository, Depends(current_committed_allocation_repository)]`. Replace the `account_allocations = list(compute_month_account_allocation(...).lines)` block (`:1407-1414`) with a resolver call inside the `if is_net_metric:` branch, and pass provenance to the builder (`:1415-1423`):

```python
        account_allocation_provenance = None
        if is_net_metric:
            deduction_components = deduction_component_repository.list_month_components(
                month=month,
                youtube_channel_ids={channel_id},
                component_kinds=NET_APPLICABLE_COMPONENT_KINDS,
            )
            account_result, account_allocation_provenance = resolve_month_account_allocation(
                month=month,
                session=session,
                deduction_repository=deduction_component_repository,
                revenue_repository=revenue_repository,
                link_repository=link_repository,
                committed_repository=committed_repository,
            )
            account_allocations = list(account_result.lines)
        explanation = build_channel_month_revenue_explanation(
            facts=facts,
            manual_overrides=overrides,
            month=month,
            youtube_channel_id=channel_id,
            metric=metric,
            deduction_components=deduction_components,
            account_allocations=account_allocations,
            account_allocation_provenance=account_allocation_provenance,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest <explain test module> tests/finance/test_explanations.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/api/revenue.py backend/ums_smart_revenue/finance/explanations.py <explain test module>
git commit -m "feat(finance): explain net-metric records account-allocation source provenance"
```

---

## Task 5: Exports — resolver + disclosure token

**Files:**
- Modify: `backend/ums_smart_revenue/api/exports.py` (`_FinanceExportSourceSummaries` `:107-114`; `_build_finance_source_summaries_for_export` `:1085-1134`; builder call sites — PDF `:635`, PPTX `:754`, preview `:840`)
- Modify: `backend/ums_smart_revenue/reports/finance_workbook.py` (`FinanceWorkbookPreview` dataclass + `to_api` `:113`; `build_finance_workbook_preview` `:122`; `build_finance_workbook_xlsx` `:169`)
- Modify: `backend/ums_smart_revenue/reports/executive_pdf.py` (`ExecutivePdfReport` dataclass; `build_executive_pdf_report` `:105`; `build_executive_pdf_bytes`)
- Modify: `backend/ums_smart_revenue/reports/branded_slide_pack.py` (`BrandedSlidePackReport` dataclass; `build_branded_slide_pack_report` `:118`; `build_branded_slide_pack_pptx`)
- Test: `tests/api/test_exports_account_allocation.py` (+ the report-builder test modules)

- [ ] **Step 1: Write the failing tests**

In `tests/api/test_exports_account_allocation.py` (reuses its `_engine`/seed helpers), add: seed a mapped month, commit a snapshot, lock the month, build the source bundle via `_build_finance_source_summaries_for_export`, and assert `summaries.account_allocation_provenance.source == "committed_snapshot"`. Add a builder-level test asserting `build_finance_workbook_preview(...).to_api()["source_summaries"]["account_allocation_provenance"]["allocation_source"] == "committed_snapshot"`, and that the XLSX/PDF/PPTX bytes contain the token substring `committed snapshot v1` for a locked snapshot vs `live compute` for an open month (assert on the rendered text — for XLSX load the workbook and read the disclosure cell; for PDF/PPTX assert the token bytes/text are present).

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/api/test_exports_account_allocation.py -q`
Expected: FAIL — `_FinanceExportSourceSummaries` has no `account_allocation_provenance`.

- [ ] **Step 3a: Bundle + builder wiring**

In `backend/ums_smart_revenue/api/exports.py`:
- Add to `_FinanceExportSourceSummaries` (`:107-114`): `account_allocation_provenance: AllocationProvenance` (import `AllocationProvenance`, `resolve_month_account_allocation` from `finance.account_allocation_read`).
- In `_build_finance_source_summaries_for_export`, replace the `account_result = compute_month_account_allocation(...)` block (`:1085-1090`) with:

```python
    account_result, account_allocation_provenance = resolve_month_account_allocation(
        month=export_job.month,
        session=session,
        deduction_repository=SqlAlchemyDeductionComponentRepository(session),
        revenue_repository=revenue_repository,
        link_repository=SqlAlchemyChannelAccountLinkRepository(session),
        committed_repository=SqlAlchemyCommittedAllocationRepository(session),
    )
```

- Add `account_allocation_provenance=account_allocation_provenance` to the `_FinanceExportSourceSummaries(...)` return (`:1129-1134`).
- At the three builder call sites, pass the provenance kwarg: `build_executive_pdf_report(..., account_allocation_provenance=source_summaries.account_allocation_provenance)` (`:635`), `build_branded_slide_pack_report(..., account_allocation_provenance=source_summaries.account_allocation_provenance)` (`:754`), and `build_finance_workbook_preview(..., account_allocation_provenance=source_summaries.account_allocation_provenance)` (`:840`).

- [ ] **Step 3b: Store provenance on the report objects + render the token**

For each report builder, add `account_allocation_provenance: AllocationProvenance` as a keyword, store it on the returned report dataclass (add the field to `FinanceWorkbookPreview` / `ExecutivePdfReport` / `BrandedSlidePackReport`), and render `account_allocation_disclosure_token(provenance)` (import it):

- `reports/finance_workbook.py`: `build_finance_workbook_preview` (`:122`) stores it; `FinanceWorkbookPreview.to_api` adds to the `source_summaries` map (`:113-118`): `"account_allocation_provenance": allocation_provenance_to_api(self.account_allocation_provenance)`; `build_finance_workbook_xlsx(preview)` (`:169`) writes a labeled disclosure row `Account allocation source` → `account_allocation_disclosure_token(preview.account_allocation_provenance)` in the executive-summary key/value sheet (adjacent to the PR-4 channel-direct/account-allocated rows).
- `reports/executive_pdf.py`: `build_executive_pdf_report` (`:105`) stores it; `build_executive_pdf_bytes(report)` renders a line `Account allocation source: <token>` in the executive-summary metadata block (where the PR-4 gross/net split rows render).
- `reports/branded_slide_pack.py`: `build_branded_slide_pack_report` (`:118`) stores it; `build_branded_slide_pack_pptx(report)` renders a footer/notes bullet `<token>` on the deductions slide.

(`build_finance_workbook_xlsx` keeps its single `preview` parameter — no new kwarg — and reads the stored provenance from the preview, per the spec.)

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/api/test_exports_account_allocation.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/api/exports.py backend/ums_smart_revenue/reports/finance_workbook.py backend/ums_smart_revenue/reports/executive_pdf.py backend/ums_smart_revenue/reports/branded_slide_pack.py tests/api/test_exports_account_allocation.py
git commit -m "feat(reports): exports prefer committed snapshot + render allocation-source disclosure token"
```

---

## Task 6: Docs status + full validation gate

**Files:** `Docs/15_DELIVERY_BACKLOG.md`, `Docs/01_IMPLEMENTATION_PLAN.md`

- [ ] **Step 1: Update `Docs/15_DELIVERY_BACKLOG.md`**

In the Spec 2b allocation-engine entry's `Remaining:` line (the PR-5 block added `Remaining: read-switch to committed snapshots; PAYMENT-grain ...; other allocation methods.`), mark the read-switch shipped and drop it from remaining. Read the current line first and edit to the same effect:

```
  PR-6 shipped (this branch): read-switch — allocation GET, net-revenue, explain, and exports
  prefer the committed snapshot for LOCKED months (lock-aware + live fallback; OPEN stays live),
  with lossless reconstruction and full allocation_source/committed_run provenance on every
  surface. No migration / no auth / no write-path change.
  Remaining: PAYMENT-grain (needs a payment→account hop); other allocation methods.
```

- [ ] **Step 2: Update `Docs/01_IMPLEMENTATION_PLAN.md`**

In the Spec 2b allocation entry, append a `PR-6 SHIPPED (this branch): read-switch …; readers prefer committed snapshots for LOCKED months, provenance on all four readers.` line and update `Remaining:` to drop the read-switch (keep PAYMENT-grain + other methods). Read the live line and edit to the same effect.

- [ ] **Step 3: Whitespace + commit**

Run: `git diff --check` → no output.

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "docs(plan): mark Spec 2b read-switch shipped"
```

- [ ] **Step 4: Full validation gate**

```
python -m ruff check backend tests scripts
$env:UMS_TEST_DATABASE_URL='postgresql+psycopg://postgres:ums@localhost:55432/test_ums'
python -m pytest tests/finance/test_account_allocation_read.py tests/finance/test_committed_allocation.py tests/api/test_allocation_api.py tests/api/test_net_revenue_api.py tests/api/test_committed_allocation_api.py tests/api/test_exports_account_allocation.py -q
python -m pytest -q
git diff --check
```
Expected: ruff clean; targeted set green; **full suite green** (PG container running); `git diff --check` no output. The PR-5 OPEN reader-untouched regression must still pass.

---

## Self-Review

**1. Spec coverage**

| Spec section | Task |
|---|---|
| §3 lock-aware + fallback policy | Task 2 (resolver) |
| §4 central resolver + AllocationProvenance | Task 2 |
| §4.1 lossless `rebuild_result_from_run` | Task 2 (+ reconstruction-equality test) |
| §4.2 account-filter recompute + notes-drop + zero-amount edge | Task 2 (`filter_committed_result_to_account` + equivalence test) |
| §4.3 `get_latest_committed` + public `tenant_id` | Task 1 |
| §5 four readers swap to resolver | Tasks 3 (GET + net-revenue), 4 (explain), 5 (exports) |
| §6.1 API provenance (GET + net-revenue) | Task 3 |
| §6.2 explain JSON provenance | Task 4 |
| §6.3 export bundle/builder/artifact + preview payload | Task 5 |
| §5 import-cycle fix (provider → revenue.py) | Task 3 (Step 3a) |
| §4 single tenant source (`committed_repository.tenant_id`) | Task 1 + Task 2 |
| §9 testing incl. PR-5 OPEN regression stays green | Tasks 1–5 + Task 6 gate |
| Docs/01 + Docs/15 | Task 6 |

No gaps. Non-goals (PAYMENT-grain, other methods, recalculate/commit/write-path/auth/schema) are untouched; no migration.

**2. Placeholder scan:** The new module, the `summarize_account_allocation` extraction, the reader edits, and the explanation edit are complete code. Reader/explain/export **test bodies** are described against the named existing test-module helpers (seed/app/principal/commit) rather than duplicating ~40 lines of fixtures verbatim, because those helpers are the established correct fixtures and re-pasting them risks drift — the resolver-module tests (Task 2) are fully inlined as the executable reference. Export artifact **rendering coordinates** (exact cell/row) are intentionally implementer-placed against a presence-asserting test in the named location (token string + sheet/section), per the spec; field names, builder params, the token helper, and the to_api key are all fixed.

**3. Type consistency:** `AllocationProvenance(source, commit_version, committed_at, run_id)` is produced by the resolver (Task 2) and consumed by `allocation_provenance_to_api` / `account_allocation_disclosure_token` (Task 2), the readers (Task 3), the explanation builder (Task 4), and the export builders (Task 5). `resolve_month_account_allocation(...)` keyword args match across all four call sites (no `tenant_id` param — tenant via `committed_repository.tenant_id`). `summarize_account_allocation(*, component_count, allocated_component_count, lines, unallocated)` is defined in Task 2 and used by both `build_account_allocation` and `filter_committed_result_to_account`. `get_latest_committed` / `tenant_id` (Task 1) match their resolver uses.
