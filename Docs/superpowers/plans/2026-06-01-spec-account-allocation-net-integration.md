# Account-Allocation Net-Revenue Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fold Spec 2b PR-1 account-allocated net-applicable (TAX/DEDUCTION) lines into the missing-net (COMPONENT_DERIVED) path of `build_month_net_revenue_summary`, on both the net-revenue API and finance-export surfaces. Read/compute only.

**Architecture:** A neutral `finance/deduction_policy.py` holds the two shared net-policy constants (breaks the `net_revenue` ↔ `allocation` import cycle). A shared `finance/allocation_inputs.py` orchestrator gathers allocation inputs from repositories. The pure builders gain `account_allocations` + `unallocated_account_issues` params and apply allocations only on the missing-net path. The net-revenue route and the exports source-summary path both call the orchestrator and pass results into the builder.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, Pydantic, Decimal, pytest + SQLite.

**Spec:** `Docs/superpowers/specs/2026-06-01-spec-account-allocation-net-integration-design.md` (approved). Branch `spec/account-allocation-net-integration` off `main` (`8a6df2a`).

**Standing constraints:** strict TDD; complete code in every step; NO `Co-Authored-By`/Claude trailer in any commit; do NOT push or open a PR (a separate finishing step handles that after approval). This PR introduces **no new Postgres-specific tests** (no migration/lock; the new tests run on SQLite), but **final validation MUST run the full suite with Postgres enabled** (`UMS_TEST_DATABASE_URL` set) so the existing PG-tier suites pass true-green — see "Final validation". Make doc edits in single sequential passes (no concurrent edits to the same file).

---

### Verified anchors (quote-checked against `8a6df2a` / HEAD `30b21df`)

- `net_revenue.py`: constants `SOURCE_SYSTEM_TO_SOURCE_KIND` (str→str) + `NET_APPLICABLE_COMPONENT_KINDS` at `:21-28`; `_applicable_deduction_components` `:169-186`; `_component_derived_channel_summary` `:189-221`; `_calculated_channel_summary` `:263-295` (`deduction_amount = adjusted_gross − adjusted_net`); `_missing_net_source_summary` `:224-260`; `build_channel_net_revenue_summary` `:298-397` (missing-net branch at `:357`); `_deduction_components_by_channel` `:447-457`; `build_month_net_revenue_summary` `:474-533`; `ChannelNetRevenueSummary` `:31-80` (+`to_api` `:51`); `MonthNetRevenueSummary` `:83-115` (+`to_api` `:98`).
- `allocation.py`: imports the two constants from `net_revenue` at `:16-19` (the cycle edge); `AllocationLine` `:98`, `UnallocatedIssue` `:114`, `AccountAllocationResult` `:148`, `build_account_allocation` `:311`.
- `api/allocation.py`: input-gathering block `:160-191`; the module imports the two repo providers from `api/revenue.py` (`current_deduction_component_repository`, `current_revenue_fact_repository`) and `current_channel_account_link_repository` from `api/channel_account_links.py`.
- `api/revenue.py`: net route `get_month_net_revenue` `:1039-1118`; perms `VIEW_REVENUE`+`VIEW_CONFIDENCE` on `target_scope` `:1067-1068`; inputs `:1071-1089`; audit `REVENUE_VIEWED` + `summary_api["audit_event"]` `:1103-1117`; payment-match dual-audit precedent `:748-751,798-801`. Imports `NET_APPLICABLE_COMPONENT_KINDS` from `net_revenue` at `:57`.
- `api/exports.py`: finance net import `:40-44`; `_FinanceExportSourceSummaries` `:96-101`; `_build_finance_source_summaries_for_export` `:987-1065` (channel_ids `:1005`, builds own `revenue_repository` `:1010`, `build_month_net_revenue_summary(...)` `:1035-1039`); `_record_finance_export_artifact_audit` `:1068-1141` (global-only `PAYMENT_VIEWED`+`BANK_RECONCILIATION_VIEWED` at `:1106-1128`); export perm gate `_require_finance_export_artifact_permissions` `:1332-1364` (already requires VIEW_FINALIZED_PAYMENTS + VIEW_BANK_RECONCILIATION).
- Tests: builder tests `tests/finance/test_net_revenue.py` (helpers `revenue_fact`, `manual_override`, `net_revenue_module`) + `tests/finance/test_net_revenue_deduction_components.py` (`component` factory); endpoint tests `tests/api/test_net_revenue_api.py` (singular `audit_event` at the happy-path test); export tests `tests/api/test_exports_api.py`; allocation endpoint tests `tests/api/test_allocation_api.py` (must stay green). Trusted-gateway token `"pytest-trusted-gateway-token"`; `finance_viewer` has VIEW_REVENUE+VIEW_CONFIDENCE+VIEW_FINALIZED_PAYMENTS; `assistant_analyst` lacks VIEW_REVENUE/VIEW_FINALIZED_PAYMENTS.

---

## Task 0: Neutral `deduction_policy` module (break the import cycle)

**Files:**
- Create: `backend/ums_smart_revenue/finance/deduction_policy.py`
- Modify: `backend/ums_smart_revenue/finance/net_revenue.py` (replace constant defs with re-export)
- Modify: `backend/ums_smart_revenue/finance/allocation.py` (import from deduction_policy)
- Test: `tests/finance/test_deduction_policy.py`

- [ ] **Step 1: Write the failing test**

Create `tests/finance/test_deduction_policy.py`:

```python
from ums_smart_revenue.finance import deduction_policy, net_revenue


def test_deduction_policy_holds_net_policy_constants():
    """The neutral module is the source of truth for the two net-policy constants."""
    assert deduction_policy.SOURCE_SYSTEM_TO_SOURCE_KIND == {
        "adsense_management": "ADSENSE",
        "youtube_reporting": "YOUTUBE_CMS",
        "youtube_analytics": "YOUTUBE_ANALYTICS",
    }
    assert deduction_policy.NET_APPLICABLE_COMPONENT_KINDS == frozenset({"TAX", "DEDUCTION"})


def test_net_revenue_reexports_same_objects():
    """net_revenue MUST re-export the same constant objects for back-compat."""
    assert net_revenue.SOURCE_SYSTEM_TO_SOURCE_KIND is deduction_policy.SOURCE_SYSTEM_TO_SOURCE_KIND
    assert net_revenue.NET_APPLICABLE_COMPONENT_KINDS is deduction_policy.NET_APPLICABLE_COMPONENT_KINDS


def test_net_revenue_and_allocation_import_together_no_cycle():
    """Importing both modules together must not raise (cycle is gone)."""
    import importlib

    importlib.import_module("ums_smart_revenue.finance.net_revenue")
    importlib.import_module("ums_smart_revenue.finance.allocation")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/finance/test_deduction_policy.py -v`
Expected: FAIL — `ModuleNotFoundError: ums_smart_revenue.finance.deduction_policy`.

- [ ] **Step 3: Create the neutral module**

Create `backend/ums_smart_revenue/finance/deduction_policy.py`:

```python
"""Shared net-revenue deduction policy constants (Phase 4 Spec 2b PR-2).

Neutral leaf module: holds the two constants both net_revenue and allocation
need, so neither has to import the other (breaks the net_revenue <-> allocation
import cycle). Imports nothing from finance.* compute modules.
"""

# ============================================================================
# Purpose: Map a Google source_system to the RevenueFactSourceKind it backs, so
#   a deduction component is only applied to a net derived from the SAME source
#   (no cross-source mixing). NET_APPLICABLE_COMPONENT_KINDS is the closed set
#   of component kinds that reduce a component-derived net.
# Database/ORM: None.
# Standards: explicit, closed maps; unknown source_system -> no match -> ignored.
# Blast Radius: Finance net-revenue derivation + account allocation net_applicable.
# ============================================================================
SOURCE_SYSTEM_TO_SOURCE_KIND: dict[str, str] = {
    "adsense_management": "ADSENSE",
    "youtube_reporting": "YOUTUBE_CMS",
    "youtube_analytics": "YOUTUBE_ANALYTICS",
}
# Only blind, source-labeled reductions reduce a component-derived net; signed
# FX_VARIANCE / TRANSFER_FEE / UNRESOLVED_PAYMENT_GAP kinds never reduce net.
NET_APPLICABLE_COMPONENT_KINDS: frozenset[str] = frozenset({"TAX", "DEDUCTION"})
```

- [ ] **Step 4: Re-export from `net_revenue.py`**

In `backend/ums_smart_revenue/finance/net_revenue.py`, replace the constant block at lines 13-28 (the contract comment + both definitions) with a re-export import. The current block is:

```python
# ============================================================================
# Purpose: Map a Google source_system to the RevenueFactSourceKind it backs, so
#   a channel-scoped deduction component is only applied to a net derived from
#   the SAME source (no cross-source mixing). Used only on the missing-net path.
# Database/ORM: None.
# Standards: explicit, closed map; unknown source_system -> no match -> ignored.
# Blast Radius: Finance net-revenue derivation (missing-net path only).
# ============================================================================
SOURCE_SYSTEM_TO_SOURCE_KIND: dict[str, str] = {
    "adsense_management": "ADSENSE",
    "youtube_reporting": "YOUTUBE_CMS",
    "youtube_analytics": "YOUTUBE_ANALYTICS",
}
# Only blind, source-labeled reductions reduce a component-derived net; signed
# FX_VARIANCE / TRANSFER_FEE / UNRESOLVED_PAYMENT_GAP kinds never reduce net.
NET_APPLICABLE_COMPONENT_KINDS: frozenset[str] = frozenset({"TAX", "DEDUCTION"})
```

Replace it with (keep it in the same spot, after the existing `from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry` import line at `:11`):

```python
# Re-exported from the neutral deduction_policy module so existing
# `from ums_smart_revenue.finance.net_revenue import NET_APPLICABLE_COMPONENT_KINDS`
# (and SOURCE_SYSTEM_TO_SOURCE_KIND) call sites keep working unchanged, while
# finance.allocation imports them from deduction_policy to avoid an import cycle.
from ums_smart_revenue.finance.deduction_policy import (  # noqa: F401  (re-export)
    NET_APPLICABLE_COMPONENT_KINDS,
    SOURCE_SYSTEM_TO_SOURCE_KIND,
)
```

(`noqa: F401` because they are re-exported, not used locally by name in every path — though several functions DO use them, the import is intentionally a re-export; if ruff does not flag it, drop the noqa. The implementer should run ruff and remove `# noqa: F401` if unused-import is not reported.)

- [ ] **Step 5: Switch `allocation.py` to import from `deduction_policy`**

In `backend/ums_smart_revenue/finance/allocation.py`, replace the import at lines 16-19:

```python
from ums_smart_revenue.finance.net_revenue import (
    NET_APPLICABLE_COMPONENT_KINDS,
    SOURCE_SYSTEM_TO_SOURCE_KIND,
)
```

with:

```python
from ums_smart_revenue.finance.deduction_policy import (
    NET_APPLICABLE_COMPONENT_KINDS,
    SOURCE_SYSTEM_TO_SOURCE_KIND,
)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python -m pytest tests/finance/test_deduction_policy.py tests/finance/test_net_revenue.py tests/finance/test_allocation.py -v`
Expected: all pass (new policy tests + existing net_revenue + existing allocation suites unaffected).

- [ ] **Step 7: ruff + commit**

Run: `python -m ruff check backend/ums_smart_revenue/finance/deduction_policy.py backend/ums_smart_revenue/finance/net_revenue.py backend/ums_smart_revenue/finance/allocation.py tests/finance/test_deduction_policy.py`
Fix any F401 (drop the noqa if not needed). Then:

```bash
git add backend/ums_smart_revenue/finance/deduction_policy.py backend/ums_smart_revenue/finance/net_revenue.py backend/ums_smart_revenue/finance/allocation.py tests/finance/test_deduction_policy.py
git commit -m "refactor(finance): extract net-policy constants to deduction_policy (break import cycle)"
```

---

## Task 1: `compute_month_account_allocation` service + refactor allocation endpoint

**Files:**
- Create: `backend/ums_smart_revenue/finance/allocation_inputs.py`
- Modify: `backend/ums_smart_revenue/api/allocation.py` (call the new service)
- Test: `tests/finance/test_allocation_inputs.py`

- [ ] **Step 1: Write the failing test**

Create `tests/finance/test_allocation_inputs.py`:

```python
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.finance.allocation_inputs import compute_month_account_allocation
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

MONTH = "2026-04"
TENANT = UUID(UMS_TENANT_ID)


def _engine(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    return engine


def _seed(session):
    session.add(
        YouTubeChannelORM(
            id=uuid4(), tenant_id=TENANT, youtube_channel_id="chA",
            channel_name="A", active=True,
        )
    )
    session.add(
        AdsenseContentOwnerLinkORM(
            id=uuid4(), tenant_id=TENANT, adsense_account_id="pub-1",
            content_owner_id="owner-1", verification_status="VERIFIED",
            provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
            effective_month_start="2026-01",
        )
    )
    session.add(
        ContentOwnerChannelLinkORM(
            id=uuid4(), tenant_id=TENANT, content_owner_id="owner-1",
            youtube_channel_id="chA", provenance_kind="SOURCE_ROW",
            active=True, effective_month_start="2026-01",
        )
    )
    session.add(
        MonthlyChannelRevenueFactORM(
            id=uuid4(), tenant_id=TENANT, month=MONTH,
            youtube_channel_id="chA", source_kind="ADSENSE",
            gross_revenue_usd=Decimal("500.00"),
        )
    )
    session.add(
        DeductionComponentORM(
            id=uuid4(), tenant_id=TENANT, month=MONTH,
            component_kind="DEDUCTION", scope_kind="ACCOUNT", scope_id="pub-1",
            amount_usd=Decimal("100.00"), currency_code="USD",
            source_system="adsense_management",
            source_table="google_revenue_source_rows",
            component_key="acct-ded-1", raw_payload={},
        )
    )
    session.commit()


def test_compute_month_account_allocation_matches_endpoint_inputs(tmp_path):
    """The service produces the same allocation the PR-1 endpoint computed inline."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        result = compute_month_account_allocation(
            month=MONTH,
            deduction_repository=SqlAlchemyDeductionComponentRepository(session),
            revenue_repository=SqlAlchemyRevenueFactRepository(session),
            link_repository=SqlAlchemyChannelAccountLinkRepository(session),
        )
    assert result.allocation_method == "gross_revenue_proportional"
    assert len(result.lines) == 1
    line = result.lines[0]
    assert line.adsense_account_id == "pub-1"
    assert line.youtube_channel_id == "chA"
    assert line.allocated_amount_usd == Decimal("100.000000")
    assert line.net_applicable is True
    assert result.unallocated == ()
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/finance/test_allocation_inputs.py -v`
Expected: FAIL — `ModuleNotFoundError: ums_smart_revenue.finance.allocation_inputs`.

- [ ] **Step 3: Create the orchestrator**

Create `backend/ums_smart_revenue/finance/allocation_inputs.py`:

```python
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
```

- [ ] **Step 4: Refactor `api/allocation.py` to call the service**

In `backend/ums_smart_revenue/api/allocation.py`, replace the input-gathering block inside `get_account_allocations` (the lines that build `components`, `facts`, `gross_basis`, `tenant_id`, `accounts`, `verified_channels`, and call `build_account_allocation`) with a single service call. The block to replace is:

```python
    # _require_valid_month is the single 422 boundary gate; it validates the
    # same month string passed to every repo, so repo-level month validation
    # is unreachable here.
    components = deduction_repository.list_account_components(
        month=month, adsense_account_id=adsense_account_id
    )
    facts = revenue_repository.list_month_facts(month=month)

    gross_basis: dict[tuple[str, str], Decimal] = {}
    for fact in facts:
        key = (fact.youtube_channel_id, fact.source_kind)
        gross_basis[key] = gross_basis.get(key, Decimal("0")) + fact.gross_revenue_usd

    tenant_id = link_repository.tenant_id
    accounts = sorted({component.scope_id for component in components})
    verified_channels = {
        account: link_repository.list_verified_adsense_account_channels(
            tenant_id=tenant_id, month=month, adsense_account_id=account
        )
        for account in accounts
    }

    result = build_account_allocation(
        month=month,
        components=components,
        verified_channels=verified_channels,
        gross_basis=gross_basis,
    )
```

with:

```python
    # _require_valid_month is the single 422 boundary gate; the same month is
    # passed to the orchestrator, so repo-level month validation is unreachable.
    result = compute_month_account_allocation(
        month=month,
        deduction_repository=deduction_repository,
        revenue_repository=revenue_repository,
        link_repository=link_repository,
        adsense_account_id=adsense_account_id,
    )
```

Update the imports in `api/allocation.py`: remove the now-unused `build_account_allocation` (keep `AccountAllocationResult` — still used by `_result_to_api`) from the `finance.allocation` import, remove `from decimal import Decimal` if no longer used elsewhere in the file (it is not, after this change), and add:

```python
from ums_smart_revenue.finance.allocation_inputs import compute_month_account_allocation
```

(Resulting `finance.allocation` import becomes `from ums_smart_revenue.finance.allocation import AccountAllocationResult`.)

- [ ] **Step 5: Run tests to verify green (incl. the unchanged endpoint suite)**

Run: `python -m pytest tests/finance/test_allocation_inputs.py tests/api/test_allocation_api.py -v`
Expected: new service test passes; all 7 existing allocation endpoint tests stay green (behavior-preserving refactor).

- [ ] **Step 6: ruff + commit**

Run: `python -m ruff check backend/ums_smart_revenue/finance/allocation_inputs.py backend/ums_smart_revenue/api/allocation.py tests/finance/test_allocation_inputs.py`
Fix any unused-import (the `Decimal`/`build_account_allocation` removals above). Then:

```bash
git add backend/ums_smart_revenue/finance/allocation_inputs.py backend/ums_smart_revenue/api/allocation.py tests/finance/test_allocation_inputs.py
git commit -m "refactor(finance): shared compute_month_account_allocation orchestrator + reuse in endpoint"
```

---

## Task 2: Pure builder — apply account allocations on the missing-net path

**Files:**
- Modify: `backend/ums_smart_revenue/finance/net_revenue.py`
- Test: `tests/finance/test_net_revenue_account_allocations.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/finance/test_net_revenue_account_allocations.py`:

```python
from decimal import Decimal

from ums_smart_revenue.finance.allocation import AllocationLine, UnallocatedIssue
from ums_smart_revenue.finance.net_revenue import (
    build_channel_net_revenue_summary,
    build_month_net_revenue_summary,
)
from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry

MONTH = "2026-04"
CH = "chA"


def _fact(*, source_kind="ADSENSE", gross="1000.00", net=None, channel=CH):
    return RevenueFactEntry(
        id=f"f-{source_kind}-{channel}",
        month=MONTH,
        youtube_channel_id=channel,
        source_kind=source_kind,
        source_report_id=None,
        gross_revenue_usd=Decimal(gross),
        net_revenue_usd=(Decimal(net) if net is not None else None),
        views=0,
        watch_time_minutes=Decimal("0"),
        confidence_score=Decimal("0.9800"),
        imported_by=None,
    )


def _alloc(*, channel=CH, account="pub-1", kind="DEDUCTION", amount="100.000000",
           source_system="adsense_management", net_applicable=True, key="k1"):
    return AllocationLine(
        adsense_account_id=account,
        youtube_channel_id=channel,
        component_kind=kind,
        source_system=source_system,
        component_key=key,
        basis_source_kind="ADSENSE",
        basis_gross_usd=Decimal("1000.000000"),
        basis_share=Decimal("1.000000"),
        allocated_amount_usd=Decimal(amount),
        net_applicable=net_applicable,
    )


def _issue(*, account="pub-9", kind="DEDUCTION", amount="40.000000", code="ACCOUNT_UNMAPPED_OR_UNVERIFIED", key="u1"):
    return UnallocatedIssue(
        scope_id=account, component_kind=kind, component_key=key,
        amount_usd=Decimal(amount), issue_code=code, detail="unmapped",
    )


def test_missing_net_applies_account_allocation():
    """Missing-net channel: net = adjusted_gross - account_allocated; breakdown set."""
    summary = build_channel_net_revenue_summary(
        facts=[_fact(net=None, gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        account_allocations=[_alloc(amount="100.000000")],
    )
    assert summary.status == "COMPONENT_DERIVED"
    assert summary.net_revenue_usd == Decimal("900.000000")
    assert summary.deduction_amount_usd == Decimal("100.000000")
    assert summary.channel_direct_deduction_amount_usd == Decimal("0")
    assert summary.account_allocated_deduction_amount_usd == Decimal("100.000000")
    # sum identity holds on COMPONENT_DERIVED
    assert (
        summary.channel_direct_deduction_amount_usd
        + summary.account_allocated_deduction_amount_usd
        == summary.deduction_amount_usd
    )


def test_source_net_channel_ignores_account_allocation():
    """Source-net channel: net + deduction unchanged; breakdown fields are None."""
    summary = build_channel_net_revenue_summary(
        facts=[_fact(net="880.00", gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        account_allocations=[_alloc(amount="100.000000")],
    )
    assert summary.status == "CALCULATED"
    assert summary.net_revenue_usd == Decimal("880.00")
    assert summary.deduction_amount_usd == Decimal("120.00")  # 1000 - 880, source-derived
    assert summary.channel_direct_deduction_amount_usd is None
    assert summary.account_allocated_deduction_amount_usd is None


def test_source_alignment_blocks_cross_kind_allocation():
    """An adsense_management allocation does not reduce a YOUTUBE_CMS-primary net."""
    summary = build_channel_net_revenue_summary(
        facts=[_fact(source_kind="YOUTUBE_CMS", net=None, gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        account_allocations=[_alloc(source_system="adsense_management")],
    )
    # no applicable allocation, no channel-direct -> missing-net source
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"
    assert summary.account_allocated_deduction_amount_usd is None


def test_basis_source_kind_mismatch_does_not_override_source_system():
    """A wrong basis_source_kind cannot make a cross-source allocation apply."""
    line = _alloc(source_system="adsense_management")
    object.__setattr__(line, "basis_source_kind", "YOUTUBE_CMS")  # frozen dataclass tweak
    summary = build_channel_net_revenue_summary(
        facts=[_fact(source_kind="YOUTUBE_CMS", net=None, gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        account_allocations=[line],
    )
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"  # source_system gate still blocks


def test_non_net_applicable_allocation_never_reduces_net():
    summary = build_channel_net_revenue_summary(
        facts=[_fact(net=None, gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        account_allocations=[_alloc(kind="UNRESOLVED_PAYMENT_GAP", net_applicable=False)],
    )
    assert summary.status == "NET_REVENUE_SOURCE_MISSING"


def test_channel_direct_plus_account_allocated_sum():
    """Both contributions apply additively on the missing-net path."""
    from ums_smart_revenue.finance.deduction_components import DeductionComponent

    channel_direct = DeductionComponent(
        id="dc1", month=MONTH, component_kind="DEDUCTION", scope_kind="CHANNEL",
        scope_id=CH, amount_usd=Decimal("30.00"), amount_native=None,
        currency_code="USD", source_system="adsense_management",
        source_table="google_revenue_source_rows", source_id=None,
        source_key=None, source_report_id=None, raw_payload={}, component_key="cd1",
    )
    summary = build_channel_net_revenue_summary(
        facts=[_fact(net=None, gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        deduction_components=[channel_direct],
        account_allocations=[_alloc(amount="100.000000")],
    )
    assert summary.net_revenue_usd == Decimal("870.000000")  # 1000 - 30 - 100
    assert summary.channel_direct_deduction_amount_usd == Decimal("30.00")
    assert summary.account_allocated_deduction_amount_usd == Decimal("100.000000")
    assert summary.deduction_amount_usd == Decimal("130.000000")


def test_safety_dedup_skips_duplicate_component_key():
    """An allocated line sharing a component_key with an applied channel-direct
    component is skipped (defensive; disjoint by construction)."""
    from ums_smart_revenue.finance.deduction_components import DeductionComponent

    shared_key = "dup-key"
    channel_direct = DeductionComponent(
        id="dc1", month=MONTH, component_kind="DEDUCTION", scope_kind="CHANNEL",
        scope_id=CH, amount_usd=Decimal("30.00"), amount_native=None,
        currency_code="USD", source_system="adsense_management",
        source_table="google_revenue_source_rows", source_id=None,
        source_key=None, source_report_id=None, raw_payload={}, component_key=shared_key,
    )
    summary = build_channel_net_revenue_summary(
        facts=[_fact(net=None, gross="1000.00")],
        manual_overrides=[],
        month=MONTH,
        youtube_channel_id=CH,
        deduction_components=[channel_direct],
        account_allocations=[_alloc(amount="100.000000", key=shared_key)],
    )
    # the duplicate-key allocation is skipped; only channel-direct applies
    assert summary.net_revenue_usd == Decimal("970.00")  # 1000 - 30
    assert summary.account_allocated_deduction_amount_usd == Decimal("0")


def test_month_unallocated_surface_global():
    """Month builder populates the unallocated surface from net-applicable issues."""
    summary = build_month_net_revenue_summary(
        month=MONTH,
        facts=[_fact(net=None, gross="1000.00")],
        manual_overrides=[],
        account_allocations=[_alloc(amount="100.000000")],
        unallocated_account_issues=[
            _issue(amount="40.000000", kind="DEDUCTION"),
            _issue(amount="5.000000", kind="UNRESOLVED_PAYMENT_GAP", code="UNSUPPORTED_SCOPE", key="u2"),
        ],
    )
    # only the net-applicable (DEDUCTION) issue is surfaced; reconciliation kind excluded
    assert summary.unallocated_account_deduction_total_usd == Decimal("40.000000")
    assert len(summary.unallocated_account_issues) == 1
    assert summary.unallocated_account_issues[0]["issue_code"] == "ACCOUNT_UNMAPPED_OR_UNVERIFIED"


def test_month_unallocated_surface_scoped_is_none():
    """When the caller withholds issues (scoped), both surface fields are None."""
    summary = build_month_net_revenue_summary(
        month=MONTH,
        facts=[_fact(net=None, gross="1000.00")],
        manual_overrides=[],
        account_allocations=[_alloc(amount="100.000000")],
        unallocated_account_issues=None,
    )
    assert summary.unallocated_account_deduction_total_usd is None
    assert summary.unallocated_account_issues is None


def test_default_no_allocations_is_unchanged_behavior():
    """Omitting the new params reproduces PR-B behavior exactly."""
    summary = build_month_net_revenue_summary(
        month=MONTH,
        facts=[_fact(net="880.00", gross="1000.00")],
        manual_overrides=[],
    )
    assert summary.channels[0].net_revenue_usd == Decimal("880.00")
    assert summary.unallocated_account_deduction_total_usd is None
    assert summary.unallocated_account_issues is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `python -m pytest tests/finance/test_net_revenue_account_allocations.py -v`
Expected: FAIL — `TypeError: build_channel_net_revenue_summary() got an unexpected keyword argument 'account_allocations'` / missing dataclass fields.

- [ ] **Step 3: Add dataclass fields + `to_api` deltas**

In `backend/ums_smart_revenue/finance/net_revenue.py`, add the import of the PR-1 types near the existing `from ums_smart_revenue.finance.revenue_facts import RevenueFactEntry` line (the cycle is gone after Task 0):

```python
from ums_smart_revenue.finance.allocation import AllocationLine, UnallocatedIssue
```

In `ChannelNetRevenueSummary` (after `deduction_amount_usd: Decimal | None`, line 44), add:

```python
    channel_direct_deduction_amount_usd: Decimal | None
    account_allocated_deduction_amount_usd: Decimal | None
```

In its `to_api()` (after the `"deduction_amount_usd"` entry, line 74), add:

```python
            "channel_direct_deduction_amount_usd": _decimal_to_api(
                self.channel_direct_deduction_amount_usd
            ),
            "account_allocated_deduction_amount_usd": _decimal_to_api(
                self.account_allocated_deduction_amount_usd
            ),
```

In `MonthNetRevenueSummary` (after `total_deduction_amount_usd: Decimal`, line 95), add:

```python
    unallocated_account_deduction_total_usd: Decimal | None
    unallocated_account_issues: list[dict[str, str]] | None
```

In its `to_api()` (after `"total_deduction_amount_usd"`, before `"channels"`, line 113), add:

```python
            "unallocated_account_deduction_total_usd": _decimal_to_api(
                self.unallocated_account_deduction_total_usd
            ),
            "unallocated_account_issues": self.unallocated_account_issues,
```

- [ ] **Step 4: Update the four `ChannelNetRevenueSummary` constructors for the new fields**

Each existing constructor must set the two new channel fields. Apply:

- `_component_derived_channel_summary` (`:189-221`) — change its signature to accept the split and set the breakdown. Replace the function with:

```python
def _component_derived_channel_summary(
    *,
    primary: RevenueFactEntry,
    month: str,
    youtube_channel_id: str,
    approved_total: Decimal,
    adjusted_gross: Decimal,
    channel_direct_total: Decimal,
    account_allocated_total: Decimal,
    approved_count: int,
    pending_count: int,
) -> ChannelNetRevenueSummary:
    """Build a channel summary whose missing net is derived from components."""
    component_total = channel_direct_total + account_allocated_total
    component_derived_net = adjusted_gross - component_total
    return ChannelNetRevenueSummary(
        month=month,
        youtube_channel_id=youtube_channel_id,
        status="COMPONENT_DERIVED",
        primary_source_kind=primary.source_kind,
        baseline_gross_revenue_usd=primary.gross_revenue_usd,
        baseline_net_revenue_usd=None,
        approved_manual_override_total_usd=approved_total,
        adjusted_gross_revenue_usd=adjusted_gross,
        net_revenue_usd=component_derived_net,
        deduction_amount_usd=component_total,
        deduction_percentage=_deduction_percentage(
            deduction_amount=component_total,
            gross_revenue_usd=adjusted_gross,
        ),
        confidence="D_ESTIMATED",
        channel_direct_deduction_amount_usd=channel_direct_total,
        account_allocated_deduction_amount_usd=account_allocated_total,
        approved_manual_override_count=approved_count,
        pending_manual_override_count=pending_count,
        issues=[],
    )
```

- `_missing_net_source_summary` (`:224-260`) — add the two fields as `None` (after `confidence="E_MISSING",`):

```python
        confidence="E_MISSING",
        channel_direct_deduction_amount_usd=None,
        account_allocated_deduction_amount_usd=None,
```

- `_calculated_channel_summary` (`:263-295`) — add the two fields as `None` (after `confidence=...,`):

```python
        confidence="D_ESTIMATED" if pending_count else "B_RECONCILED",
        channel_direct_deduction_amount_usd=None,
        account_allocated_deduction_amount_usd=None,
```

- `_empty_channel_summary` (`:536-572`) — add the two fields as `None` (after `confidence="E_MISSING",`):

```python
        confidence="E_MISSING",
        channel_direct_deduction_amount_usd=None,
        account_allocated_deduction_amount_usd=None,
```

- [ ] **Step 5: Add the allocation applicability helper + grouping helper**

In `net_revenue.py`, add after `_applicable_deduction_components` (`:186`):

```python
def _applicable_account_allocations(
    allocations: Iterable[AllocationLine],
    *,
    youtube_channel_id: str,
    primary_source_kind: str,
) -> list[AllocationLine]:
    """Return source-aligned net-applicable account allocations for a channel.

    Same source-alignment rule as _applicable_deduction_components; basis_source_kind
    is provenance only and is deliberately NOT used as a second alignment contract.
    """
    return [
        line
        for line in allocations
        if line.youtube_channel_id == youtube_channel_id
        and line.net_applicable
        and SOURCE_SYSTEM_TO_SOURCE_KIND.get(line.source_system) == primary_source_kind
    ]
```

And add after `_deduction_components_by_channel` (`:457`):

```python
def _account_allocations_by_channel(
    account_allocations: Iterable[AllocationLine],
) -> dict[str, list[AllocationLine]]:
    """Group account-allocation lines by YouTube channel."""
    grouped: dict[str, list[AllocationLine]] = defaultdict(list)
    for line in account_allocations:
        grouped[line.youtube_channel_id].append(line)
    return grouped
```

- [ ] **Step 6: Apply allocations on the missing-net branch of `build_channel_net_revenue_summary`**

Add `account_allocations: Iterable[AllocationLine] = ()` to the signature (after `deduction_components`, `:304`). Then replace the missing-net branch (`:357-387`) with:

```python
    if primary.net_revenue_usd is None:
        channel_direct = _applicable_deduction_components(
            deduction_components,
            month=resolved_month,
            youtube_channel_id=resolved_channel_id,
            primary_source_kind=primary.source_kind,
        )
        applied_keys = {component.component_key for component in channel_direct}
        account_allocated = [
            line
            for line in _applicable_account_allocations(
                account_allocations,
                youtube_channel_id=resolved_channel_id,
                primary_source_kind=primary.source_kind,
            )
            # Safety dedup (defensive; disjoint by construction): never apply an
            # allocated line whose component_key already applied as channel-direct.
            if line.component_key not in applied_keys
        ]
        if channel_direct or account_allocated:
            channel_direct_total = sum(
                (component.amount_usd for component in channel_direct),
                Decimal("0"),
            )
            account_allocated_total = sum(
                (line.allocated_amount_usd for line in account_allocated),
                Decimal("0"),
            )
            return _component_derived_channel_summary(
                primary=primary,
                month=resolved_month,
                youtube_channel_id=resolved_channel_id,
                approved_total=approved_total,
                adjusted_gross=adjusted_gross,
                channel_direct_total=channel_direct_total,
                account_allocated_total=account_allocated_total,
                approved_count=len(approved),
                pending_count=len(pending),
            )
        return _missing_net_source_summary(
            primary=primary,
            month=resolved_month,
            youtube_channel_id=resolved_channel_id,
            approved_total=approved_total,
            adjusted_gross=adjusted_gross,
            approved_count=len(approved),
            pending_count=len(pending),
        )
```

- [ ] **Step 7: Thread allocations + unallocated surface through `build_month_net_revenue_summary`**

Add the two params to the signature (after `deduction_components`, `:479`):

```python
    account_allocations: Iterable[AllocationLine] = (),
    unallocated_account_issues: Iterable[UnallocatedIssue] | None = None,
```

After `components_by_channel = _deduction_components_by_channel(...)` (`:490-493`), add:

```python
    allocations_by_channel = _account_allocations_by_channel(account_allocations)
```

In the per-channel `build_channel_net_revenue_summary(...)` call (`:497-503`), add the argument:

```python
            account_allocations=allocations_by_channel.get(channel_id, ()),
```

Before the `return MonthNetRevenueSummary(...)` (`:513`), compute the surface:

```python
    if unallocated_account_issues is None:
        unallocated_total: Decimal | None = None
        unallocated_api: list[dict[str, str]] | None = None
    else:
        net_applicable_issues = [
            issue
            for issue in unallocated_account_issues
            if issue.component_kind in NET_APPLICABLE_COMPONENT_KINDS
        ]
        unallocated_total = sum(
            (issue.amount_usd for issue in net_applicable_issues),
            Decimal("0"),
        )
        unallocated_api = [
            {
                "scope_id": issue.scope_id,
                "component_kind": issue.component_kind,
                "component_key": issue.component_key,
                "amount_usd": _decimal_to_api(issue.amount_usd),
                "issue_code": issue.issue_code,
                "detail": issue.detail,
            }
            for issue in net_applicable_issues
        ]
```

And add to the `MonthNetRevenueSummary(...)` constructor (after `total_deduction_amount_usd=...`, before `channels=channels`):

```python
        unallocated_account_deduction_total_usd=unallocated_total,
        unallocated_account_issues=unallocated_api,
```

- [ ] **Step 8: Run tests to verify pass**

Run: `python -m pytest tests/finance/test_net_revenue_account_allocations.py tests/finance/test_net_revenue.py tests/finance/test_net_revenue_deduction_components.py -v`
Expected: new suite passes; existing net_revenue suites still pass (note: existing tests that construct `ChannelNetRevenueSummary`/`MonthNetRevenueSummary` directly, if any, must be updated for the new required fields — search and fix them in this step; builder-based tests are unaffected since the new params default).

- [ ] **Step 9: ruff + commit**

Run: `python -m ruff check backend/ums_smart_revenue/finance/net_revenue.py tests/finance/test_net_revenue_account_allocations.py`

```bash
git add backend/ums_smart_revenue/finance/net_revenue.py tests/finance/test_net_revenue_account_allocations.py
git commit -m "feat(finance): apply account-allocated deductions on the missing-net path"
```

---

## Task 3: Net-revenue route — gather allocations, dual audit, scoped pin

**Files:**
- Modify: `backend/ums_smart_revenue/api/revenue.py` (`get_month_net_revenue`)
- Test: `tests/api/test_net_revenue_api.py`

- [ ] **Step 1: Write/adjust the failing tests**

In `tests/api/test_net_revenue_api.py`, the existing happy-path test asserts singular `audit_event` and does not require `VIEW_FINALIZED_PAYMENTS`. Update it and add coverage. First, change the happy-path assertion from:

```python
    assert response.json()["audit_event"]["event_type"] == "REVENUE_VIEWED"
    assert audit_log.event_type == "REVENUE_VIEWED"
```

to (note: `finance_viewer` already has VIEW_FINALIZED_PAYMENTS, so the existing header is fine; the DB now has two audit rows):

```python
    assert {e["event_type"] for e in response.json()["audit_events"]} == {
        "REVENUE_VIEWED",
        "PAYMENT_VIEWED",
    }
    audit_kinds = {row.event_type for row in session.scalars(select(AuditLogORM)).all()}
    assert audit_kinds == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}
```

(Replace the `audit_log = session.scalars(select(AuditLogORM)).one()` line with `.all()` usage as above, since there are now two rows.)

Add these imports to the test file's import block (needed by the gate test):

```python
from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
```

Then append the gate + surface tests. **No static role grants VIEW_REVENUE without VIEW_FINALIZED_PAYMENTS** (verified against `auth/seed.py`), so the gate test injects a custom `direct_permissions` principal via `dependency_overrides` (precedent: `test_exports_api.py:349-390`); `USER_ID` is the file's existing seeded-user constant:

```python
def test_net_revenue_forbidden_without_finalized_payment_permission(tmp_path):
    """A principal with VIEW_REVENUE + VIEW_CONFIDENCE but NOT VIEW_FINALIZED_PAYMENTS
    is rejected by the new gate (fail-closed)."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="revenue-no-payments@example.com",
        direct_permissions=(
            PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope()),
            PermissionGrant(Permission.VIEW_CONFIDENCE, AccessScope.global_scope()),
            # deliberately NO VIEW_FINALIZED_PAYMENTS grant
        ),
    )
    client = TestClient(app)
    response = client.get("/revenue/months/2026-03/net-revenue?scope_type=global")
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_finalized_payments"


def test_net_revenue_scoped_omits_unallocated_surface(tmp_path):
    """A scoped (company) request serializes unallocated-account fields as null."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}",
        headers=auth_headers("finance_viewer", "company", str(COMPANY_ID)),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["unallocated_account_deduction_total_usd"] is None
    assert body["unallocated_account_issues"] is None


def test_net_revenue_global_includes_unallocated_surface(tmp_path):
    """A global request includes the unallocated-account surface (possibly empty)."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/revenue/months/2026-03/net-revenue?scope_type=global",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    # present (not None) at global scope — empty when nothing unallocated
    assert body["unallocated_account_deduction_total_usd"] is not None
    assert body["unallocated_account_issues"] is not None
```

(If `COMPANY_ID` is not already a module constant in this test file, use the existing company id symbol the file defines for the seeded company scope.)

- [ ] **Step 2: Run to verify failures**

Run: `python -m pytest tests/api/test_net_revenue_api.py -v`
Expected: FAIL — response still has singular `audit_event`; no `unallocated_*` keys; new gate not enforced.

- [ ] **Step 3: Update the route**

In `backend/ums_smart_revenue/api/revenue.py`, `get_month_net_revenue` (`:1039-1118`):

Add the channel-account-link repo provider to the dependencies (it already imports the deduction + revenue providers; add the link repo). At the top of the file ensure this import exists (add if missing):

```python
from ums_smart_revenue.api.channel_account_links import (
    current_channel_account_link_repository,
)
from ums_smart_revenue.finance.allocation_inputs import compute_month_account_allocation
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
```

Add the dependency parameter to the handler signature (after `deduction_component_repository`):

```python
    link_repository: Annotated[
        SqlAlchemyChannelAccountLinkRepository,
        Depends(current_channel_account_link_repository),
    ],
```

After the existing two permission checks (`:1067-1068`), add the finalized-payment gate (month-scoped):

```python
    _require_permission(
        user, Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(month), org_index
    )
```

In the input-gathering try-block (after `deduction_components = ...` at `:1079-1083`), compute allocations and pass them to the builder. Replace the `summary = build_month_net_revenue_summary(...)` call (`:1084-1089`) with:

```python
        account_result = compute_month_account_allocation(
            month=month,
            deduction_repository=deduction_component_repository,
            revenue_repository=revenue_repository,
            link_repository=link_repository,
        )
        summary = build_month_net_revenue_summary(
            month=month,
            facts=facts,
            manual_overrides=overrides,
            deduction_components=deduction_components,
            account_allocations=account_result.lines,
            unallocated_account_issues=(
                account_result.unallocated if scope_type == "global" else None
            ),
        )
```

Replace the single audit record + `summary_api["audit_event"]` (`:1103-1117`) with the dual-audit pattern (mirroring payment-match `:773-801`):

```python
    revenue_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_net_revenue_summary",
        entity_id=f"{month}:{scope_type}:{scope_id or 'global'}",
        scope=target_scope,
        details={
            "status": summary.status,
            "channel_count": summary.channel_count,
            "calculated_channel_count": summary.calculated_channel_count,
            "missing_net_source_count": summary.missing_net_source_count,
        },
    )
    payment_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="monthly_net_revenue_summary",
        entity_id=f"{month}:{scope_type}:{scope_id or 'global'}",
        scope=AccessScope.finance_month(month),
        details={
            "status": summary.status,
            "channel_count": summary.channel_count,
        },
    )
    summary_api["audit_events"] = [
        audit_record_to_api(revenue_record),
        audit_record_to_api(payment_record),
    ]
    return summary_api
```

- [ ] **Step 4: Run tests to verify pass**

Run: `python -m pytest tests/api/test_net_revenue_api.py -v`
Expected: all pass (updated happy-path + 3 new tests).

- [ ] **Step 5: ruff + commit**

Run: `python -m ruff check backend/ums_smart_revenue/api/revenue.py tests/api/test_net_revenue_api.py`

```bash
git add backend/ums_smart_revenue/api/revenue.py tests/api/test_net_revenue_api.py
git commit -m "feat(api): net-revenue consumes account allocations; dual REVENUE/PAYMENT audit + scoped pin"
```

---

## Task 4: Finance exports — same allocations, drift fix, scoped audit

**Files:**
- Modify: `backend/ums_smart_revenue/api/exports.py`
- Test: `tests/api/test_exports_account_allocation.py` (new)

- [ ] **Step 1: Write the failing tests**

There is **no HTTP path in the test suite that materializes a queued export into an artifact** (artifact routes require a COMPLETED job + served file), so these tests target the two changed functions **directly** — `_build_finance_source_summaries_for_export` and `_record_finance_export_artifact_audit` are importable module functions. All four arguments are trivially constructible: `OrgAccessIndex()`, an empty `ChannelGroupRegistry` (unused — `scope_channel_ids`/global resolves the channels, verified via `_resolved_export_channel_ids`), `InMemoryAuditSink`, and a literal `ExportJobEntry`. `datetime.now()` is disallowed in this environment — use a fixed `datetime(2026, 4, 1, tzinfo=UTC)`.

Create `tests/api/test_exports_account_allocation.py`:

```python
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.api.exports import (
    _build_finance_source_summaries_for_export,
    _record_finance_export_artifact_audit,
)
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import OrgAccessIndex
from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, YouTubeChannelORM
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistry
from ums_smart_revenue.reports.exports import ExportJobEntry
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

MONTH = "2026-04"
TENANT = UUID(UMS_TENANT_ID)


def _engine(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}")
    OrgBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    return engine


def _seed_missing_net_with_components(session):
    """A channel with gross but NO source net + a CHANNEL-direct DEDUCTION + an
    ACCOUNT deduction mapped to that channel via a VERIFIED link."""
    session.add(
        YouTubeChannelORM(
            id=uuid4(), tenant_id=TENANT, youtube_channel_id="chA",
            channel_name="A", active=True,
        )
    )
    session.add(
        MonthlyChannelRevenueFactORM(
            id=uuid4(), tenant_id=TENANT, month=MONTH, youtube_channel_id="chA",
            source_kind="ADSENSE", gross_revenue_usd=Decimal("1000.00"),
            net_revenue_usd=None,
        )
    )
    session.add(
        DeductionComponentORM(
            id=uuid4(), tenant_id=TENANT, month=MONTH, component_kind="DEDUCTION",
            scope_kind="CHANNEL", scope_id="chA", amount_usd=Decimal("30.00"),
            currency_code="USD", source_system="adsense_management",
            source_table="google_revenue_source_rows", component_key="cd-1",
            raw_payload={},
        )
    )
    session.add(
        AdsenseContentOwnerLinkORM(
            id=uuid4(), tenant_id=TENANT, adsense_account_id="pub-1",
            content_owner_id="owner-1", verification_status="VERIFIED",
            provenance_kind="OPERATOR_ASSERTED", provenance_payload={},
            effective_month_start="2026-01",
        )
    )
    session.add(
        ContentOwnerChannelLinkORM(
            id=uuid4(), tenant_id=TENANT, content_owner_id="owner-1",
            youtube_channel_id="chA", provenance_kind="SOURCE_ROW", active=True,
            effective_month_start="2026-01",
        )
    )
    session.add(
        DeductionComponentORM(
            id=uuid4(), tenant_id=TENANT, month=MONTH, component_kind="DEDUCTION",
            scope_kind="ACCOUNT", scope_id="pub-1", amount_usd=Decimal("100.00"),
            currency_code="USD", source_system="adsense_management",
            source_table="google_revenue_source_rows", component_key="ad-1",
            raw_payload={},
        )
    )
    session.add(
        FinanceMonthCloseORM(
            tenant_id=TENANT, month=MONTH, status="OPEN", allocation_rule_payload={}
        )
    )
    session.commit()


def _export_job(*, scope_type, scope_channel_ids):
    return ExportJobEntry(
        id="exp-1", export_type="FINANCE_EXCEL", scope_type=scope_type,
        scope_id=None if scope_type == "global" else "company-a", month=MONTH,
        currency="USD", requested_by="user-1", status="COMPLETED", file_url=None,
        month_lock_status="OPEN", include_confidence_notes=False,
        include_manual_override_notes=False,
        created_at=datetime(2026, 4, 1, tzinfo=UTC), completed_at=None,
        scope_channel_ids=scope_channel_ids,
    )


def test_export_net_reflects_channel_direct_and_account_deductions(tmp_path):
    """Regression: exports previously passed NO deduction_components, so export net
    diverged from API net. Now the export source summary nets out BOTH the
    channel-direct (30) and the account-allocated (100) deductions."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed_missing_net_with_components(session)
        summaries = _build_finance_source_summaries_for_export(
            export_job=_export_job(scope_type="global", scope_channel_ids=None),
            session=session,
            org_index=OrgAccessIndex(),
            group_registry=ChannelGroupRegistry(),
        )
    channel = summaries.net_revenue.channels[0]
    assert channel.status == "COMPONENT_DERIVED"
    assert channel.net_revenue_usd == Decimal("870.000000")  # 1000 - 30 - 100
    assert channel.channel_direct_deduction_amount_usd == Decimal("30.00")
    assert channel.account_allocated_deduction_amount_usd == Decimal("100.000000")


def test_scoped_finance_export_records_payment_viewed():
    """A scoped (company) finance-artifact export now emits PAYMENT_VIEWED (was
    global-only); BANK_RECONCILIATION_VIEWED stays global-only."""
    sink = InMemoryAuditSink()
    user = UserPrincipal(user_id="user-1", email="exp@example.com")
    records = _record_finance_export_artifact_audit(
        audit_sink=sink,
        user=user,
        export_job=_export_job(scope_type="company", scope_channel_ids=("chA",)),
        group_registry=ChannelGroupRegistry(),
        artifact_type="finance_workbook_xlsx",
        include_download_event=False,
    )
    kinds = {record.event_type for record in records}
    assert "REVENUE_VIEWED" in kinds
    assert "PAYMENT_VIEWED" in kinds
    assert "BANK_RECONCILIATION_VIEWED" not in kinds  # scoped: no bank exposure


def test_global_finance_export_still_records_bank_reconciliation_viewed():
    """Global finance export keeps PAYMENT_VIEWED + BANK_RECONCILIATION_VIEWED."""
    sink = InMemoryAuditSink()
    user = UserPrincipal(user_id="user-1", email="exp@example.com")
    records = _record_finance_export_artifact_audit(
        audit_sink=sink,
        user=user,
        export_job=_export_job(scope_type="global", scope_channel_ids=None),
        group_registry=ChannelGroupRegistry(),
        artifact_type="finance_workbook_xlsx",
        include_download_event=False,
    )
    kinds = {record.event_type for record in records}
    assert {"REVENUE_VIEWED", "PAYMENT_VIEWED", "BANK_RECONCILIATION_VIEWED"} <= kinds
```

**Implementer notes:** (a) confirm `ChannelGroupRegistry` import path by grepping how `api/exports.py`/its tests construct one (recon: `ums_smart_revenue.org.channel_groups.ChannelGroupRegistry`, empty-constructible); if a different concrete store is the norm, use it. (b) Confirm the exact `ExportJobEntry` field list against `reports/exports.py:35-57` and fill every required field (the recon-verified set is above — `include_confidence_notes`/`include_manual_override_notes` are required bools, `artifact_*` default to None). (c) `_record_finance_export_artifact_audit` returns the list of `AuditRecord` it created — assert on that list directly; `event_type` values are plain strings (`"PAYMENT_VIEWED"` etc.).

- [ ] **Step 2: Run to verify failures**

Run: `python -m pytest tests/api/test_exports_account_allocation.py -v`
Expected: FAIL — `test_export_net_reflects_channel_direct_and_account_deductions` shows the channel as `NET_REVENUE_SOURCE_MISSING` (the export builder passes no components/allocations yet); `test_scoped_finance_export_records_payment_viewed` fails because a scoped export records no `PAYMENT_VIEWED` today. (`test_global_...` passes already — it documents the preserved global behavior.)

- [ ] **Step 3: Feed allocations + channel-direct components into the export builder**

In `backend/ums_smart_revenue/api/exports.py`, add imports near the existing finance imports (`:40-52`):

```python
from ums_smart_revenue.finance.allocation_inputs import compute_month_account_allocation
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.deduction_policy import NET_APPLICABLE_COMPONENT_KINDS
```

In `_build_finance_source_summaries_for_export` (`:987-1065`), replace the `net_revenue = build_month_net_revenue_summary(...)` call (`:1035-1039`) with:

```python
    deduction_components = SqlAlchemyDeductionComponentRepository(session).list_month_components(
        month=export_job.month,
        youtube_channel_ids=channel_ids,
        component_kinds=NET_APPLICABLE_COMPONENT_KINDS,
    )
    account_result = compute_month_account_allocation(
        month=export_job.month,
        deduction_repository=SqlAlchemyDeductionComponentRepository(session),
        revenue_repository=revenue_repository,
        link_repository=SqlAlchemyChannelAccountLinkRepository(session),
    )
    net_revenue = build_month_net_revenue_summary(
        month=export_job.month,
        facts=facts,
        manual_overrides=manual_overrides,
        deduction_components=deduction_components,
        account_allocations=account_result.lines,
        unallocated_account_issues=(
            account_result.unallocated if export_job.scope_type == "global" else None
        ),
    )
```

- [ ] **Step 4: Emit `PAYMENT_VIEWED` for all finance-artifact exports**

In `_record_finance_export_artifact_audit` (`:1068-1141`), restructure the global-only block (`:1106-1128`) so `PAYMENT_VIEWED` is always recorded and only `BANK_RECONCILIATION_VIEWED` stays global-only. Replace that `if export_job.scope_type == "global":` block with:

```python
    audit_records.append(
        record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.PAYMENT_VIEWED,
            entity_type="export_job",
            entity_id=export_job.id,
            scope=month_scope,
            details=details,
        )
    )
    if export_job.scope_type == "global":
        audit_records.append(
            record_audit_event(
                sink=audit_sink,
                actor=user,
                event_type=AuditEventType.BANK_RECONCILIATION_VIEWED,
                entity_type="export_job",
                entity_id=export_job.id,
                scope=month_scope,
                details=details,
            )
        )
```

- [ ] **Step 5: Run tests to verify pass (incl. existing export suite)**

Run: `python -m pytest tests/api/test_exports_account_allocation.py tests/api/test_exports_api.py -v`
Expected: the 3 new tests pass; the existing export suite still passes — **note** any existing test in `tests/api/test_exports_api.py` that asserted a scoped finance-artifact export emits ONLY `REVENUE_VIEWED` (no `PAYMENT_VIEWED`) must be updated to include `PAYMENT_VIEWED` (search for audit-set assertions on finance-artifact exports and update them here; this is the intended audit-behavior change).

- [ ] **Step 6: ruff + commit**

Run: `python -m ruff check backend/ums_smart_revenue/api/exports.py tests/api/test_exports_account_allocation.py`

```bash
git add backend/ums_smart_revenue/api/exports.py tests/api/test_exports_account_allocation.py tests/api/test_exports_api.py
git commit -m "feat(api): finance exports consume same allocations (no net drift) + PAYMENT_VIEWED on scoped exports"
```

---

## Task 5: Documentation status update

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`
- Modify: `Docs/15_DELIVERY_BACKLOG.md`

- [ ] **Step 1: Update `Docs/15_DELIVERY_BACKLOG.md`**

Locate the `⏳ Allocation engine (Spec 2b) — PR-1 shipped` bullet (~line 317). Update its "Remaining:" tail to mark PR-2 shipped. Change:

```
  Remaining: net-revenue integration of net-applicable lines; PAYMENT-grain (needs a
  payment→account hop); persisted/committed allocation; other allocation methods.
```

to:

```
  PR-2 shipped (this branch): net-revenue API + finance exports consume account-allocated
  net-applicable (TAX/DEDUCTION) lines on the missing-net path (COMPONENT_DERIVED), with
  per-channel channel_direct/account_allocated breakdown fields, a global-scope-only
  unallocated-account surface, VIEW_FINALIZED_PAYMENTS@finance_month + PAYMENT_VIEWED on the
  net route, and PAYMENT_VIEWED on all finance-artifact exports. Net-revenue audit envelope
  changed from `audit_event` to `audit_events` (plural). Remaining: PAYMENT-grain (needs a
  payment→account hop); persisted/committed allocation; other allocation methods; explain-path
  provenance; export breakdown columns.
```

- [ ] **Step 2: Update `Docs/01_IMPLEMENTATION_PLAN.md`**

Locate the `⏳ Allocation rules (Spec 2b) — PR-1 SHIPPED` bullet (~line 444). Update its "Remaining:" to lead with PR-2:

```
- ⏳ Allocation rules (Spec 2b) — PR-1 SHIPPED + PR-2 SHIPPED (this branch): PR-2 folds
  account-allocated net-applicable lines into net-revenue (API + finance exports) on the
  missing-net path, read/compute only (no persistence, no migration). Remaining: PAYMENT-grain,
  persisted/committed writes, other methods, explain-path provenance, export breakdown columns.
  Prerequisite SHIPPED
```

(Keep the rest of the existing bullet unchanged after "Prerequisite SHIPPED".)

- [ ] **Step 3: Verify doc hygiene + commit**

Run: `git diff --check`
Expected: no whitespace errors. Then:

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "docs(plan): mark Spec 2b PR-2 net-revenue integration shipped"
```

---

## Final validation (after all tasks)

The `AGENTS.md` baseline (`:116-119`) requires `pytest -q` to pass — a **true-green** run, not "green except known errors." The Postgres-tier suites RAISE (never skip) when `UMS_TEST_DATABASE_URL` is unset, so run the full suite with the disposable container set so there are **zero** errors and zero failures:

```bash
python -m ruff check backend tests scripts
git diff --check
# Ensure the PG container is up (ums-mig-pg-test, postgres:18-alpine, port 55432), then:
UMS_TEST_DATABASE_URL='postgresql+psycopg://postgres:ums@localhost:55432/test_ums' python -m pytest -q
```

Expected: ruff clean; `git diff --check` clean; pytest reports **N passed, 0 failed, 0 errors** (full suite, Postgres tier included — this PR adds no migration, so the PG suites must stay green, not error). If the container genuinely cannot be started in this environment, do NOT declare the branch ready on a partial run — record the exact blocker (command + why) per the AGENTS.md "if a validation gate cannot run" rule and surface it. Confirm no `Co-Authored-By`/Claude trailer on any commit (`git log --format=%B 8a6df2a..HEAD`). Do NOT push or open a PR until explicitly approved.

---

## Self-review (writing-plans)

**Spec coverage:** §2 scope → T0 (deduction_policy), T1 (allocation_inputs + endpoint refactor), T2 (builder: params, breakdown, `_applicable_account_allocations`, `_account_allocations_by_channel`, missing-net application + dedup, unallocated surface, `to_api` deltas), T3 (net route: gather, dual audit, scoped pin, VIEW_FINALIZED_PAYMENTS), T4 (exports: both inputs, drift fix, scoped PAYMENT_VIEWED), T5 (docs). §4.6 cycle → T0. §5.4 export gap → T4. §5.1 audit envelope → T3. §4.4 breakdown semantics → T2 (constructors). §4.5 unallocated surface + finding #3 null → T2 (month builder) + T3 (scoped→None) tests.

**Placeholder scan:** Every code step contains complete, runnable code with expected output — no `...` placeholders. T4's export tests are full direct-function unit tests in a new `tests/api/test_exports_account_allocation.py` (the recon confirmed no HTTP artifact-generation path exists in the suite to drive over the client, so the two changed functions are tested directly). The few "implementer note" lines (confirm `ChannelGroupRegistry` import path; confirm the exact `ExportJobEntry` field list against `reports/exports.py:35-57`) are verification reminders, not missing code.

**Type consistency:** `account_allocations`/`unallocated_account_issues` param names, `AllocationLine`/`UnallocatedIssue` fields, `_applicable_account_allocations`/`_account_allocations_by_channel` signatures, `compute_month_account_allocation` signature, the four new dataclass fields, and `audit_events` (plural) are used identically across T2/T3/T4. `deduction_policy` constants re-exported (T0) so existing `from net_revenue import …` sites (`api/revenue.py:57`, `allocation.py`) keep working.

**Ordering invariant:** Task 0 MUST land first (every later import of the constants + the `net_revenue` → `allocation` import depends on the cycle being broken). Tasks 1→4 are then linear (each imports the prior task's new symbol).

**Known implementer obligations flagged in-step:** (a) update any existing tests that construct `ChannelNetRevenueSummary`/`MonthNetRevenueSummary` directly for the new required fields (T2 Step 8); (b) update existing net-revenue endpoint test for the `audit_event`→`audit_events` change (T3 Step 1); (c) update any existing scoped-export audit-set assertion to include `PAYMENT_VIEWED` (T4 Step 5).
