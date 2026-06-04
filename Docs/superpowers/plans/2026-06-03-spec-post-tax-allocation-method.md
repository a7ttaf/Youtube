# Post-Tax Allocation Method Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `post_tax_revenue_proportional` as a second committable account-allocation method alongside `gross_revenue_proportional`, by parameterizing the allocation engine, selecting a gross-vs-net basis in the orchestrator, un-gating the commit path, aligning the dry-run net check, and renaming the persisted basis field honestly.

**Architecture:** The Hamilton distribution (`_proportional_allocation`) is basis-agnostic and reused unchanged; only the basis *number* differs per method, so basis construction branches in the orchestrator (`compute_month_account_allocation`). The engine threads `allocation_method` for the result label and the method-aware zero-basis issue code. The commit path is un-gated to a two-method allowlist (service + DB CHECK + a new migration); `/revenue/recalculate`'s dry-run net check moves from channel grain to `(channel, source_kind)` grain; and the persisted basis field `basis_gross_usd` is renamed to `basis_amount_usd`.

**Tech Stack:** Python 3 · FastAPI · SQLAlchemy 2.x · Alembic · PostgreSQL (source of truth) · SQLite (unit tests) · pytest · ruff.

**Spec:** `Docs/superpowers/specs/2026-06-03-spec-post-tax-allocation-method-design.md`
**Branch:** `spec/post-tax-allocation-method` off `origin/main` `f4e8cf7` (spec commits `c96b314` + `d6959a8`).

**Standing constraints:** strict TDD per task (failing test → run-to-fail → minimal impl → run-to-pass → commit); review after each task; **no push, no PR, no merge**; every commit message **trailer-free** (no Co-Authored-By, no Generated footer); use `python -m pytest` (not bare `pytest`) and `python -m ruff`; **do not** use `git checkout`/`restore`/`reset` on files. PG-tier tests need `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:ums@localhost:55432/test_ums` (disposable container `ums-mig-pg-test`).

**Re-confirm anchors:** line numbers below are verified against `f4e8cf7`, but each task says "read the file region first" — re-confirm before editing in case an earlier task shifted lines.

---

## File Structure

**Modify (backend):**
- `backend/ums_smart_revenue/finance/allocation.py` — engine: constants, `AllocationLine` field rename, parameterized `build_account_allocation` / `_allocate_component`, method-aware zero-basis code.
- `backend/ums_smart_revenue/finance/allocation_inputs.py` — orchestrator: `allocation_method` param + gross/net basis selection.
- `backend/ums_smart_revenue/finance/committed_allocation.py` — commit gate allowlist + pass method; persist renamed field.
- `backend/ums_smart_revenue/finance/account_allocation_read.py` — reconstruction reads renamed column.
- `backend/ums_smart_revenue/finance/recalculation.py` — dry-run `(channel, source_kind)`-grain net check.
- `backend/ums_smart_revenue/api/allocation.py` — API JSON key rename (GET + commit responses).
- `backend/ums_smart_revenue/db/finance_models.py` — committed-lines column rename + finite CHECK; runs method CHECK allowlist.

**Create:**
- `backend/ums_smart_revenue/db/alembic/versions/20260603_0001_post_tax_allocation_method.py` — column rename + finite CHECK recreate (Task 1) + runs method CHECK expansion (Task 4).

**Tests (12 files):** `tests/finance/test_allocation.py`, `tests/finance/test_allocation_inputs.py`, `tests/finance/test_committed_allocation.py`, `tests/finance/test_account_allocation_read.py`, `tests/finance/test_explanations.py`, `tests/finance/test_net_revenue_account_allocations.py`, `tests/api/test_allocation_api.py`, `tests/api/test_committed_allocation_api.py`, `tests/api/test_revenue_recalculation_api.py`, `tests/api/test_exports_account_allocation.py`, `tests/db/test_committed_allocation_models.py`, `tests/db/test_committed_allocation_migration_postgres.py`.

**Docs:** `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md` (status update, Task 6).

**Do NOT change:** historical specs/plans (point-in-time records); the merged migration `20260602_0001`; `explanations.py` (surfaces `basis_share`, not the renamed field); the report/export builders' output (token-only); auth gates; `/revenue/recalculate` committed-write path; lock/commit semantics.

---

## Task 1: Rename `basis_gross_usd` → `basis_amount_usd` (mechanical, no behavior change) + new migration (column-rename half)

This is a pure rename across active code + the 4 tests that reference the identifier + a new migration that renames the DB column. No behavior changes. The runs method CHECK is untouched here (Task 4).

**Files:**
- Modify: `backend/ums_smart_revenue/finance/allocation.py:107` (field), `:287` (construction)
- Modify: `backend/ums_smart_revenue/finance/committed_allocation.py:189` (persist)
- Modify: `backend/ums_smart_revenue/finance/account_allocation_read.py:61` (reconstruct)
- Modify: `backend/ums_smart_revenue/api/allocation.py:162`, `:426` (JSON keys)
- Modify: `backend/ums_smart_revenue/db/finance_models.py:997` (column), `:1011-1012` (finite CHECK expression)
- Create: `backend/ums_smart_revenue/db/alembic/versions/20260603_0001_post_tax_allocation_method.py`
- Test: `tests/db/test_committed_allocation_models.py`, `tests/db/test_committed_allocation_migration_postgres.py`, `tests/finance/test_net_revenue_account_allocations.py`, `tests/finance/test_explanations.py`, `tests/api/test_allocation_api.py`

- [ ] **Step 1: Update the model test to expect the renamed column (failing test)**

Read `tests/db/test_committed_allocation_models.py` first. Replace every `basis_gross_usd` with `basis_amount_usd` (4 hits: lines ~75, 174, 209, 243 — both `CommittedAllocationLineORM(basis_gross_usd=...)` constructions and `row.basis_gross_usd` assertions). Example transformation for a construction site:

```python
# before
CommittedAllocationLineORM(
    run_id=run.id, adsense_account_id="pub-1", youtube_channel_id="chA",
    component_kind="DEDUCTION", source_system="adsense_management",
    component_key="k1", basis_source_kind="ADSENSE",
    basis_gross_usd=Decimal("700.000000"), basis_share=Decimal("0.700000"),
    allocated_amount_usd=Decimal("70.000000"), net_applicable=True,
)
# after: basis_gross_usd=  ->  basis_amount_usd=
```

- [ ] **Step 2: Run the model test to verify it fails**

Run: `python -m pytest tests/db/test_committed_allocation_models.py -q`
Expected: FAIL — `TypeError: 'basis_amount_usd' is an invalid keyword argument for CommittedAllocationLineORM` (model still has `basis_gross_usd`).

- [ ] **Step 3: Rename the ORM column + finite CHECK expression**

In `backend/ums_smart_revenue/db/finance_models.py`, in `CommittedAllocationLineORM` (around `:997`):

```python
# before
    basis_gross_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
# after
    basis_amount_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
```

And the finite CHECK (around `:1010-1018`):

```python
        CheckConstraint(
            "basis_amount_usd > '-Infinity'::numeric "
            "AND basis_amount_usd < 'Infinity'::numeric "
            "AND basis_share > '-Infinity'::numeric "
            "AND basis_share < 'Infinity'::numeric "
            "AND allocated_amount_usd > '-Infinity'::numeric "
            "AND allocated_amount_usd < 'Infinity'::numeric",
            name="ck_committed_allocation_lines_amounts_finite",
        ).ddl_if(dialect="postgresql"),
```

- [ ] **Step 4: Rename the dataclass field + construction + persist + reconstruct + API keys**

`backend/ums_smart_revenue/finance/allocation.py:107`:
```python
    basis_amount_usd: Decimal
```
`allocation.py:287` (inside `_allocate_component`'s `AllocationLine(...)` construction) — change the keyword only (the local `gross` var is renamed in Task 2):
```python
            basis_amount_usd=gross,
```
`backend/ums_smart_revenue/finance/committed_allocation.py:189`:
```python
                basis_amount_usd=ln.basis_amount_usd, basis_share=ln.basis_share,
```
`backend/ums_smart_revenue/finance/account_allocation_read.py:61`:
```python
            basis_amount_usd=row.basis_amount_usd,
```
`backend/ums_smart_revenue/api/allocation.py:162` and `:426` (both JSON serializers):
```python
                "basis_amount_usd": decimal_to_api(ln.basis_amount_usd),
```

- [ ] **Step 5: Rename in the two finance tests that construct AllocationLine**

Read each first, then replace `basis_gross_usd` → `basis_amount_usd`:
- `tests/finance/test_net_revenue_account_allocations.py:44` (AllocationLine fixture keyword).
- `tests/finance/test_explanations.py:133` (AllocationLine fixture keyword).

- [ ] **Step 5b: Assert the GET endpoint emits the renamed JSON key (spec §10)**

Read `tests/api/test_allocation_api.py` first. It currently has no `basis_gross_usd` reference, so the GET serializer rename (api/allocation.py:162) doesn't break it — this step ADDS coverage for the renamed response key. Find the existing GET-success test (the one that asserts a 200 from `GET /revenue/months/{month}/account-allocations` and inspects the response `allocations` list) and add this assertion to it (the `allocations` list shape is `_result_to_api`'s per-line dicts, so the key lives on each element):

```python
    assert body["allocations"]
    assert all("basis_amount_usd" in a for a in body["allocations"])
    assert all("basis_gross_usd" not in a for a in body["allocations"])
```

If no GET-success test asserts on `allocations` yet, add a minimal one mirroring the file's existing authorized-GET setup (seed → principal/headers → `client.get(...)` → 200), then add the three assertions above.

- [ ] **Step 6: Create the new migration (column-rename half only)**

Create `backend/ums_smart_revenue/db/alembic/versions/20260603_0001_post_tax_allocation_method.py`. (The runs method CHECK expansion is appended in Task 4.)

```python
"""Rename committed_allocation_lines.basis_gross_usd -> basis_amount_usd.

(Task 4 extends THIS migration to also expand the committed_allocation_runs
method allowlist to post_tax; in Task 1 it renames the column only — keep the
docstring's first line matching the migration's actual content at each step.)

Revision ID: 20260603_0001
Revises: 20260602_0001
Create Date: 2026-06-03

Spec: Docs/superpowers/specs/2026-06-03-spec-post-tax-allocation-method-design.md
"""

import sqlalchemy as sa
from alembic import op

revision = "20260603_0001"
down_revision = "20260602_0001"
branch_labels = None
depends_on = None


def _finite(column: str) -> str:
    """Postgres-only finite (non-NaN, non-Inf) guard for a numeric column."""
    return f"{column} > '-Infinity'::numeric AND {column} < 'Infinity'::numeric"


# ============================================================================
# Purpose: Rename the committed-allocation line basis column to the honest,
#   method-neutral basis_amount_usd (it stores net for post_tax lines), and
#   recreate the Postgres-only finite CHECK so its expression references the
#   new column. (Task 4 appends the runs method-allowlist expansion.)
# Database/ORM: committed_allocation_lines / CommittedAllocationLineORM.
# Standards: batch_alter_table keeps the rename SQLite-compatible (on Postgres
#   it is a direct ALTER); the finite CHECK is Postgres-only (dialect-guarded),
#   matching finance_models.py .ddl_if(dialect="postgresql").
# Blast Radius: Finance write schema; column rename on pre-alpha data preserves
#   rows. PostgreSQL remains source of truth. No auth/audit/Neo4j impact.
# ============================================================================
def upgrade() -> None:
    """Rename basis_gross_usd -> basis_amount_usd (+ recreate the finite CHECK)."""
    is_pg = op.get_bind().dialect.name == "postgresql"
    if is_pg:
        op.drop_constraint(
            "ck_committed_allocation_lines_amounts_finite",
            "committed_allocation_lines", type_="check",
        )
    with op.batch_alter_table("committed_allocation_lines") as batch:
        batch.alter_column(
            "basis_gross_usd", new_column_name="basis_amount_usd",
            existing_type=sa.Numeric(20, 6), existing_nullable=False,
        )
    if is_pg:
        op.create_check_constraint(
            "ck_committed_allocation_lines_amounts_finite",
            "committed_allocation_lines",
            f"{_finite('basis_amount_usd')} AND {_finite('basis_share')} "
            f"AND {_finite('allocated_amount_usd')}",
        )


def downgrade() -> None:
    """Rename basis_amount_usd -> basis_gross_usd (+ recreate the finite CHECK)."""
    is_pg = op.get_bind().dialect.name == "postgresql"
    if is_pg:
        op.drop_constraint(
            "ck_committed_allocation_lines_amounts_finite",
            "committed_allocation_lines", type_="check",
        )
    with op.batch_alter_table("committed_allocation_lines") as batch:
        batch.alter_column(
            "basis_amount_usd", new_column_name="basis_gross_usd",
            existing_type=sa.Numeric(20, 6), existing_nullable=False,
        )
    if is_pg:
        op.create_check_constraint(
            "ck_committed_allocation_lines_amounts_finite",
            "committed_allocation_lines",
            f"{_finite('basis_gross_usd')} AND {_finite('basis_share')} "
            f"AND {_finite('allocated_amount_usd')}",
        )
```

- [ ] **Step 7: Update the migration test column assertions**

Read `tests/db/test_committed_allocation_migration_postgres.py` first. Replace `basis_gross_usd` → `basis_amount_usd` in the 4 hits (lines ~232, 262, 312 [docstring], 381), and add an assertion that after `alembic upgrade head` the `committed_allocation_lines` table has column `basis_amount_usd` and not `basis_gross_usd`. Use the file's existing reflection/upgrade helper (e.g. the inspector pattern already used there). Representative assertion to add inside the existing upgrade-then-inspect test:

```python
    columns = {c["name"] for c in inspector.get_columns("committed_allocation_lines")}
    assert "basis_amount_usd" in columns
    assert "basis_gross_usd" not in columns
```

- [ ] **Step 8: Run the affected tests + a stale-identifier grep**

Run: `python -m pytest tests/db/test_committed_allocation_models.py tests/finance/test_net_revenue_account_allocations.py tests/finance/test_explanations.py tests/api/test_allocation_api.py -q`
Expected: PASS.
Run (PG): `python -m pytest tests/db/test_committed_allocation_migration_postgres.py -q` (with `UMS_TEST_DATABASE_URL` set)
Expected: PASS.
Run: `python -m ruff check backend tests`
Expected: clean.
Confirm no stale active-code/test references remain (docs/merged-migration are intentionally excluded):
Use Grep for `basis_gross_usd` over `backend/ums_smart_revenue/finance`, `backend/ums_smart_revenue/api`, `backend/ums_smart_revenue/db/finance_models.py`, and `tests/` — expected: zero hits (the only remaining hits are in `Docs/` and the merged migration `20260602_0001`).

- [ ] **Step 9: Commit**

```bash
git add backend/ums_smart_revenue/finance/allocation.py backend/ums_smart_revenue/finance/committed_allocation.py backend/ums_smart_revenue/finance/account_allocation_read.py backend/ums_smart_revenue/api/allocation.py backend/ums_smart_revenue/db/finance_models.py backend/ums_smart_revenue/db/alembic/versions/20260603_0001_post_tax_allocation_method.py tests/db/test_committed_allocation_models.py tests/db/test_committed_allocation_migration_postgres.py tests/finance/test_net_revenue_account_allocations.py tests/finance/test_explanations.py tests/api/test_allocation_api.py
git commit -m "refactor(finance): rename allocation basis_gross_usd -> basis_amount_usd"
```

---

## Task 2: Parameterize the pure engine by `allocation_method`

Add method-neutral parameterization to `allocation.py`: rename the `gross_basis` param to `basis`, add `allocation_method`, emit a method-aware zero-basis issue code, set the result method from the parameter, and rename the local `gross` loop var. Default method = gross → byte-identical to today. Update the two callers' keyword (`gross_basis=` → `basis=`).

**Files:**
- Modify: `backend/ums_smart_revenue/finance/allocation.py:21` (constants), `:34-45` (`_basis_source_kind` docstring), `:205-294` (`_allocate_component`), `:338-380` (`build_account_allocation`), `:1-7` + `:324-337` (docstrings)
- Modify: `backend/ums_smart_revenue/finance/allocation_inputs.py:63` (caller keyword)
- Test: `tests/finance/test_allocation.py`

- [ ] **Step 1: Write the failing tests**

Read `tests/finance/test_allocation.py` to the end first (note: it calls `build_account_allocation(..., gross_basis={...})`, reuses the `_component(...)` helper, and ends after its last allocation test). Append the following three test functions at the END of the file; they assume the new `basis=` keyword + `allocation_method`:

```python
def test_post_tax_method_labels_result_and_lines():
    """post_tax method threads through to the result method label."""
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="100.00")],
        verified_channels={"pub-1": ["chA", "chB"]},
        basis={("chA", "ADSENSE"): Decimal("700"), ("chB", "ADSENSE"): Decimal("300")},
        allocation_method="post_tax_revenue_proportional",
    )
    assert result.allocation_method == "post_tax_revenue_proportional"
    by_channel = {ln.youtube_channel_id: ln.allocated_amount_usd for ln in result.lines}
    assert by_channel == {"chA": Decimal("70.000000"), "chB": Decimal("30.000000")}


def test_post_tax_zero_basis_emits_zero_net_basis_code():
    """A zero net basis total fails closed with ZERO_NET_BASIS for post_tax."""
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00")],
        verified_channels={"pub-1": ["chA"]},
        basis={("chA", "ADSENSE"): Decimal("0")},
        allocation_method="post_tax_revenue_proportional",
    )
    assert result.lines == ()
    assert result.unallocated[0].issue_code == "ZERO_NET_BASIS"


def test_gross_zero_basis_still_emits_zero_gross_basis_code():
    """The gross path keeps ZERO_GROSS_BASIS (default method unchanged)."""
    result = build_account_allocation(
        month="2026-04",
        components=[_component(amount="10.00")],
        verified_channels={"pub-1": ["chA"]},
        basis={("chA", "ADSENSE"): Decimal("0")},
    )
    assert result.unallocated[0].issue_code == "ZERO_GROSS_BASIS"
    assert result.allocation_method == "gross_revenue_proportional"


def test_unsupported_method_fails_closed():
    """The engine fails closed for a method outside the {gross, post_tax} allowlist."""
    with pytest.raises(allocation.AllocationValidationError):
        build_account_allocation(
            month="2026-04",
            components=[_component(amount="10.00")],
            verified_channels={"pub-1": ["chA"]},
            basis={("chA", "ADSENSE"): Decimal("100")},
            allocation_method="company_level",
        )
```

Also update EVERY existing call site in this file: replace the keyword `gross_basis=` with `basis=` at each `build_account_allocation(...)` call. Find them with Grep for `gross_basis=` in `tests/finance/test_allocation.py` (≈13 sites) rather than relying on line numbers, then update each — none should remain after this step.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/finance/test_allocation.py -q`
Expected: FAIL — `TypeError: build_account_allocation() got an unexpected keyword argument 'basis'` / `'allocation_method'`.

- [ ] **Step 3: Add engine constants + method-aware zero-basis mapping**

`backend/ums_smart_revenue/finance/allocation.py` (near `:21`):

```python
ALLOCATION_METHOD = "gross_revenue_proportional"
POST_TAX_ALLOCATION_METHOD = "post_tax_revenue_proportional"
COMMITTABLE_ALLOCATION_METHODS = frozenset(
    {ALLOCATION_METHOD, POST_TAX_ALLOCATION_METHOD}
)

# Method-aware zero-basis issue (code, detail). Net is non-negative (DB CHECK +
# validator), so the post_tax trigger is a zero total, not a negative one.
_ZERO_BASIS_ISSUE: dict[str, tuple[str, str]] = {
    ALLOCATION_METHOD: (
        "ZERO_GROSS_BASIS",
        "verified channels have zero or negative source-aligned gross",
    ),
    POST_TAX_ALLOCATION_METHOD: (
        "ZERO_NET_BASIS",
        "verified channels have zero source-aligned net",
    ),
}
```

- [ ] **Step 4: Parameterize `_allocate_component`**

Replace the `_allocate_component` signature + body (`allocation.py:205-294`). Rename `gross_basis` → `basis`, add `allocation_method`, rename the local `gross` loop var → `weight`, and select the zero-basis issue by method. Full replacement:

```python
def _allocate_component(
    component: DeductionComponent,
    verified_channels: Mapping[str, Sequence[str]],
    basis: Mapping[tuple[str, str], Decimal],
    allocation_method: str,
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
        (channel_id, basis[(channel_id, source_kind)])
        for channel_id in channels
        if (channel_id, source_kind) in basis
    ]
    if not present:
        return _ComponentOutcome(
            (),
            _issue(component, "BASIS_MISSING",
                   "no source-aligned basis for any verified channel"),
            False,
        )
    if len(present) != len(channels):
        return _ComponentOutcome(
            (),
            _issue(component, "BASIS_INCOMPLETE",
                   "some verified channels missing source-aligned basis"),
            False,
        )

    basis_total = sum((weight for _, weight in present), Decimal("0"))
    if basis_total <= 0:
        zero_code, zero_detail = _ZERO_BASIS_ISSUE[allocation_method]
        return _ComponentOutcome(
            (),
            _issue(component, zero_code, zero_detail),
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
            basis_amount_usd=weight,
            basis_share=(weight / basis_total).quantize(_SCALE),
            allocated_amount_usd=allocated[channel_id],
            net_applicable=net_applicable,
        )
        for channel_id, weight in present
    )
    return _ComponentOutcome(lines, None, True)
```

- [ ] **Step 5: Parameterize `build_account_allocation`**

Replace its signature + the `_allocate_component` call + the result construction (`allocation.py:338-380`). Rename `gross_basis` → `basis`, add `allocation_method=ALLOCATION_METHOD`, pass the method through, and set `allocation_method=allocation_method` on the result:

```python
def build_account_allocation(
    *,
    month: str,
    components: Iterable[DeductionComponent],
    verified_channels: Mapping[str, Sequence[str]],
    basis: Mapping[tuple[str, str], Decimal],
    allocation_method: str = ALLOCATION_METHOD,
) -> AccountAllocationResult:
    """Compute per-channel allocation + unallocated issues for one month.

    Precondition: each verified_channels[account] is expected to contain
    DISTINCT channel ids (the Spec 2a read contract
    list_verified_adsense_account_channels guarantees this via .distinct());
    duplicates would double-count.
    """
    # Fail closed for ALL callers (not only the public commit gate): an
    # unsupported method must raise clearly here rather than mislabel the result
    # or KeyError in the _ZERO_BASIS_ISSUE lookup. AllocationValidationError is
    # already defined in this module (allocation.py:30); no import needed.
    if allocation_method not in COMMITTABLE_ALLOCATION_METHODS:
        raise AllocationValidationError(
            f"unsupported allocation method: {allocation_method}"
        )
    lines: list[AllocationLine] = []
    unallocated: list[UnallocatedIssue] = []
    notes = _multi_account_notes(verified_channels)

    component_count = 0
    allocated_component_count = 0
    for component in components:
        component_count += 1
        outcome = _allocate_component(
            component, verified_channels, basis, allocation_method
        )
        lines.extend(outcome.lines)
        if outcome.issue is not None:
            unallocated.append(outcome.issue)
        if outcome.allocated:
            allocated_component_count += 1

    summary = summarize_account_allocation(
        component_count=component_count,
        allocated_component_count=allocated_component_count,
        lines=lines,
        unallocated=unallocated,
    )
    return AccountAllocationResult(
        month=month,
        allocation_method=allocation_method,
        lines=tuple(lines),
        unallocated=tuple(unallocated),
        notes=tuple(notes),
        summary=summary,
    )
```

- [ ] **Step 6: Update the orchestrator caller keyword**

`backend/ums_smart_revenue/finance/allocation_inputs.py:63` — rename the keyword only (the gross-vs-net basis selection comes in Task 3; the local map is still named `gross_basis` here until Task 3 renames it):

```python
    return build_account_allocation(
        month=month,
        components=components,
        verified_channels=verified_channels,
        basis=gross_basis,
    )
```

- [ ] **Step 7: Update method-neutral wording in docstrings/comments**

In `allocation.py`: module docstring (`:1-7`) and the `build_account_allocation` contract comment (`:324-337`) say "raw-gross-proportional" / "raw-gross"; reword to cover both methods, e.g. "source-aligned proportional (gross or post-tax net) share". Update `_basis_source_kind`'s docstring (`:34-45`) "raw-gross source kind" → "source-aligned source kind" (the function resolves the source kind that weights the split, regardless of whether the weights come from gross or net). Keep `_multi_account_notes`'s "gross is weighted separately" note method-neutral ("basis is weighted separately"). These are comment-only; no logic change.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `python -m pytest tests/finance/test_allocation.py tests/finance/test_allocation_inputs.py -q`
Expected: PASS (gross path byte-identical; post_tax labeling + ZERO_NET_BASIS verified).
Run: `python -m ruff check backend tests`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add backend/ums_smart_revenue/finance/allocation.py backend/ums_smart_revenue/finance/allocation_inputs.py tests/finance/test_allocation.py
git commit -m "feat(finance): parameterize account allocation engine by allocation_method"
```

---

## Task 3: Orchestrator gross-vs-post_tax basis selection

Add `allocation_method` to `compute_month_account_allocation` and build the basis from gross (default) or source net (post_tax, omitting any `(channel, source_kind)` key with a null-net fact).

**Files:**
- Modify: `backend/ums_smart_revenue/finance/allocation_inputs.py:38-68`
- Test: `tests/finance/test_allocation_inputs.py`

- [ ] **Step 1: Write the failing tests**

Read `tests/finance/test_allocation_inputs.py` first (reuse its `_engine`/`_seed`). Append:

```python
def test_post_tax_uses_source_net_basis(tmp_path):
    """post_tax weights the split by source net_revenue_usd, not gross."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)
        # Add a second verified channel (chB) + facts so the split is observable.
        session.add(
            YouTubeChannelORM(
                id=uuid4(), tenant_id=TENANT, youtube_channel_id="chB",
                channel_name="B", active=True,
            )
        )
        session.add(
            ContentOwnerChannelLinkORM(
                id=uuid4(), tenant_id=TENANT, content_owner_id="owner-1",
                youtube_channel_id="chB", provenance_kind="SOURCE_ROW",
                active=True, effective_month_start="2026-01",
            )
        )
        # chA: gross 500 / net 300; chB: gross 500 / net 100 -> net split 75/25.
        session.execute(
            MonthlyChannelRevenueFactORM.__table__.update()
            .where(MonthlyChannelRevenueFactORM.youtube_channel_id == "chA")
            .values(net_revenue_usd=Decimal("300.00"))
        )
        session.add(
            MonthlyChannelRevenueFactORM(
                id=uuid4(), tenant_id=TENANT, month=MONTH,
                youtube_channel_id="chB", source_kind="ADSENSE",
                gross_revenue_usd=Decimal("500.00"), net_revenue_usd=Decimal("100.00"),
            )
        )
        session.commit()
        result = compute_month_account_allocation(
            month=MONTH,
            deduction_repository=SqlAlchemyDeductionComponentRepository(session),
            revenue_repository=SqlAlchemyRevenueFactRepository(session),
            link_repository=SqlAlchemyChannelAccountLinkRepository(session),
            allocation_method="post_tax_revenue_proportional",
        )
    assert result.allocation_method == "post_tax_revenue_proportional"
    by_channel = {ln.youtube_channel_id: ln.allocated_amount_usd for ln in result.lines}
    # $100 deduction split by net 300:100 -> 75 / 25 (NOT the 50/50 gross split).
    assert by_channel == {"chA": Decimal("75.000000"), "chB": Decimal("25.000000")}


def test_post_tax_omits_key_with_any_null_net(tmp_path):
    """A (channel, source_kind) with any null-net fact is dropped -> fail closed."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        _seed(session)  # chA fact has net_revenue_usd = None (not set in _seed)
        result = compute_month_account_allocation(
            month=MONTH,
            deduction_repository=SqlAlchemyDeductionComponentRepository(session),
            revenue_repository=SqlAlchemyRevenueFactRepository(session),
            link_repository=SqlAlchemyChannelAccountLinkRepository(session),
            allocation_method="post_tax_revenue_proportional",
        )
    assert result.lines == ()
    assert result.unallocated[0].issue_code == "BASIS_MISSING"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/finance/test_allocation_inputs.py -q`
Expected: FAIL — `TypeError: compute_month_account_allocation() got an unexpected keyword argument 'allocation_method'`.

- [ ] **Step 3: Implement gross/net basis selection**

Replace `backend/ums_smart_revenue/finance/allocation_inputs.py`'s imports + `compute_month_account_allocation` with the parameterized version + two private basis builders:

```python
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
```

Then replace the `compute_month_account_allocation` body (keep its contract comment block, updating "raw-gross basis" wording to "source-aligned basis (gross or post-tax net)"):

```python
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
```

(Confirm `RevenueFactEntry` is importable from `revenue_facts`; it is the dataclass returned by `list_month_facts`. If the type import causes a cycle, type the helpers as `list` without the element annotation.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/finance/test_allocation_inputs.py tests/finance/test_allocation.py -q`
Expected: PASS.
Run: `python -m ruff check backend tests`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/allocation_inputs.py tests/finance/test_allocation_inputs.py
git commit -m "feat(finance): select gross vs source-net basis in allocation orchestrator"
```

---

## Task 4: Un-gate the commit path (service allowlist + pass method + DB CHECK + migration) + reconstruction round-trip

**Files:**
- Modify: `backend/ums_smart_revenue/finance/committed_allocation.py:25` (import), `:141-144` (gate), `:146-151` (compute call), `:95` (commit_allocation contract comment)
- Modify: `backend/ums_smart_revenue/db/finance_models.py:943-946` (runs method CHECK), `:874-875` + `:884` (CommittedAllocationRunORM docstring/comment wording)
- Modify: `backend/ums_smart_revenue/db/alembic/versions/20260603_0001_post_tax_allocation_method.py` (append method-CHECK expansion to up/down)
- Test: `tests/finance/test_committed_allocation.py`, `tests/api/test_committed_allocation_api.py`, `tests/db/test_committed_allocation_models.py`, `tests/db/test_committed_allocation_migration_postgres.py`, `tests/finance/test_account_allocation_read.py`

- [ ] **Step 1: Write the failing tests**

Read each test file first and reuse its confirmed fixtures (named below). `_seed_account_deduction` / `_seed` / `_add_account` all seed `net_revenue_usd=None`, so each post_tax success test first updates chA's net to a non-null value (else post_tax fails closed → reject-on-unallocated).

`tests/finance/test_committed_allocation.py` — uses `_session(tmp_path)`, `_seed_account_deduction(session, *, mapped, status="OPEN")`, `_repos(session) -> (committed, ded, rev, link)`, `_commit(committed, ded, rev, link, *, key, fp, reason, method)`, and module constants `MONTH`/`ACTOR`; `MonthlyChannelRevenueFactORM` + `CommittedAllocationValidationError` are already imported. Add:

```python
def test_commit_post_tax_persists_method_and_basis_amount(tmp_path):
    """A post_tax commit persists allocation_method + basis_amount_usd."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True)
    # post_tax needs non-null source net for chA/ADSENSE (the seed leaves it None).
    session.execute(
        MonthlyChannelRevenueFactORM.__table__.update()
        .where(MonthlyChannelRevenueFactORM.youtube_channel_id == "chA")
        .values(net_revenue_usd=Decimal("800.00"))
    )
    session.commit()
    committed, ded, rev, link = _repos(session)
    outcome = _commit(
        committed, ded, rev, link, key="k-pt", fp="fp-pt",
        method="post_tax_revenue_proportional",
    )
    assert outcome.created is True
    assert outcome.run.allocation_method == "post_tax_revenue_proportional"
    assert outcome.lines[0].basis_amount_usd == Decimal("800.000000")


def test_commit_rejects_unsupported_method(tmp_path):
    """A method outside the {gross, post_tax} allowlist is rejected (company_level)."""
    session = _session(tmp_path)
    _seed_account_deduction(session, mapped=True)
    committed, ded, rev, link = _repos(session)
    with pytest.raises(CommittedAllocationValidationError):
        _commit(
            committed, ded, rev, link, key="k-bad", fp="fp-bad",
            method="company_level",
        )
```

`tests/finance/test_account_allocation_read.py` — uses `_session(tmp_path)`, `_add_account(session, *, account, channel, gross, deduction, mapped)`, module constants `MONTH`/`TENANT`/`ACTOR`; imports `resolve_month_account_allocation`, `SqlAlchemyCommittedAllocationRepository`, the three input repos, `FinanceMonthCloseORM`, `MonthlyChannelRevenueFactORM`. Add `from sqlalchemy import select` to the file's imports (it currently imports `create_engine, event`). This commits a post_tax run while OPEN, LOCKs the month, then proves the read-switch serves the snapshot (which runs `rebuild_result_from_run` end-to-end):

```python
def test_resolve_serves_committed_post_tax_snapshot_for_locked_month(tmp_path):
    """LOCKED month -> committed post_tax snapshot (reconstructed losslessly)."""
    session = _session(tmp_path)
    _add_account(
        session, account="pub-1", channel="chA",
        gross="1000.00", deduction="100.00", mapped=True,
    )
    session.execute(
        MonthlyChannelRevenueFactORM.__table__.update()
        .where(MonthlyChannelRevenueFactORM.youtube_channel_id == "chA")
        .values(net_revenue_usd=Decimal("800.00"))
    )
    session.commit()
    committed = SqlAlchemyCommittedAllocationRepository(session)
    ded = SqlAlchemyDeductionComponentRepository(session)
    rev = SqlAlchemyRevenueFactRepository(session)
    link = SqlAlchemyChannelAccountLinkRepository(session)
    committed.commit_allocation(
        month=MONTH, allocation_method="post_tax_revenue_proportional",
        idempotency_key="k-pt", request_fingerprint="fp-pt", reason="close",
        committed_by=ACTOR, deduction_repository=ded, revenue_repository=rev,
        link_repository=link,
    )
    # commit_allocation auto-created the OPEN close row; lock it so the
    # read-switch prefers the committed snapshot.
    close = session.scalars(
        select(FinanceMonthCloseORM).where(
            FinanceMonthCloseORM.tenant_id == TENANT,
            FinanceMonthCloseORM.month == MONTH,
        )
    ).one()
    close.status = "LOCKED"
    session.commit()
    result, provenance = resolve_month_account_allocation(
        month=MONTH, session=session, deduction_repository=ded,
        revenue_repository=rev, link_repository=link, committed_repository=committed,
    )
    assert provenance.source == "committed_snapshot"
    assert result.allocation_method == "post_tax_revenue_proportional"
    assert result.lines[0].basis_amount_usd == Decimal("800.000000")
```

`tests/db/test_committed_allocation_models.py` — uses `_engine(tmp_path)` + `_run(**overrides)` factory; `CommittedAllocationRunORM`, `IntegrityError`, `Session` already imported. The existing `test_method_check_rejects_other_method` (company_level → `IntegrityError`) STAYS VALID under the allowlist — only update its docstring to "rejects a method outside the {gross, post_tax} allowlist". Add:

```python
def test_method_check_accepts_post_tax(tmp_path):
    """allocation_method CHECK accepts post_tax_revenue_proportional."""
    engine = _engine(tmp_path)
    with Session(engine) as session:
        session.add(_run(
            allocation_method="post_tax_revenue_proportional", idempotency_key="key-pt"
        ))
        session.commit()
        assert session.query(CommittedAllocationRunORM).count() == 1
```

`tests/api/test_committed_allocation_api.py` — uses `build_database_url(tmp_path)`, `_seed(database_url, *, mapped=True, status="OPEN")`, `_principal(...)`, `COMMIT_PATH`, `MONTH`; imports `create_app`, `current_principal_from_headers`, `TestClient`, `create_engine`, `Session`, `MonthlyChannelRevenueFactORM`. Add (mirroring the file's existing 201-commit test's app + dependency-override + client setup):

```python
def test_commit_post_tax_returns_201_with_basis_amount(tmp_path):
    """POST commit with post_tax persists the method + the renamed basis key."""
    database_url = build_database_url(tmp_path)
    _seed(database_url, mapped=True)
    # post_tax needs non-null source net (the seed leaves chA net = None).
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.execute(
            MonthlyChannelRevenueFactORM.__table__.update()
            .where(MonthlyChannelRevenueFactORM.youtube_channel_id == "chA")
            .values(net_revenue_usd=Decimal("800.00"))
        )
        session.commit()
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: _principal()
    client = TestClient(app)
    response = client.post(
        COMMIT_PATH,
        json={"idempotency_key": "k-pt", "reason": "post-tax close",
              "allocation_method": "post_tax_revenue_proportional"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["run"]["allocation_method"] == "post_tax_revenue_proportional"
    assert body["allocations"]
    assert all("basis_amount_usd" in a for a in body["allocations"])
    assert all("basis_gross_usd" not in a for a in body["allocations"])
```

`tests/db/test_committed_allocation_migration_postgres.py` — the file provides the `alembic_config` + `fresh_engine` fixtures, `command.upgrade(alembic_config, "head")`, and the `_insert_run_sql(**ov)` helper (overridable `method`/`key`, valid sentinels for the rest); `IntegrityError` is already imported. Add:

```python
def test_runs_method_check_accepts_post_tax_rejects_third(alembic_config, fresh_engine):
    """After upgrade to head, the runs method CHECK allows post_tax and rejects a third."""
    command.upgrade(alembic_config, "head")
    ok_sql, ok_params = _insert_run_sql(method="post_tax_revenue_proportional", key="k-pt")
    with fresh_engine.begin() as conn:
        conn.execute(ok_sql, ok_params)  # post_tax is allowlisted -> succeeds
    bad_sql, bad_params = _insert_run_sql(method="company_level", key="k-bad")
    with pytest.raises(IntegrityError), fresh_engine.begin() as conn:
        conn.execute(bad_sql, bad_params)  # violates ck_committed_allocation_runs_method
```

For the Task 1 column-rename assertion in this same file: the existing `basis_gross_usd` references (≈ lines 232, 262, 312, 381) test the head schema, which after the new migration has `basis_amount_usd` — replace them accordingly, and the Step 7 assertion uses the file's `inspect(fresh_engine)` inspector (already imported).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/finance/test_committed_allocation.py tests/finance/test_account_allocation_read.py tests/db/test_committed_allocation_models.py -q` and (PG) `python -m pytest tests/api/test_committed_allocation_api.py tests/db/test_committed_allocation_migration_postgres.py -q`
Expected: FAIL — post_tax rejected by the service gate (`unsupported allocation method`) and by the DB CHECK.

- [ ] **Step 3: Un-gate the service path**

`backend/ums_smart_revenue/finance/committed_allocation.py:25` — extend the import:
```python
from ums_smart_revenue.finance.allocation import (
    COMMITTABLE_ALLOCATION_METHODS,
    AccountAllocationResult,
)
```
(`ALLOCATION_METHOD` may no longer be needed here once the gate uses the allowlist — remove it from the import if unused to keep ruff clean.)

`committed_allocation.py:141-144` — replace the single-method gate:
```python
        if allocation_method not in COMMITTABLE_ALLOCATION_METHODS:
            raise CommittedAllocationValidationError(
                f"unsupported allocation method: {allocation_method}"
            )
```

`committed_allocation.py:146-151` — pass the method into the compute:
```python
        result: AccountAllocationResult = compute_month_account_allocation(
            month=month,
            deduction_repository=deduction_repository,
            revenue_repository=revenue_repository,
            link_repository=link_repository,
            allocation_method=allocation_method,
        )
```

Update the `commit_allocation` contract comment (`:95-96`) and the `CommittedAllocationRunORM` docstring/comments in `finance_models.py` (`:875`, `:884`) from "gross_revenue_proportional" to "gross or post-tax (allowlisted)".

- [ ] **Step 4: Expand the DB model method CHECK**

`backend/ums_smart_revenue/db/finance_models.py:943-946`:
```python
        CheckConstraint(
            "allocation_method IN "
            "('gross_revenue_proportional', 'post_tax_revenue_proportional')",
            name="ck_committed_allocation_runs_method",
        ),
```

- [ ] **Step 5: Append the method-CHECK expansion to the migration**

Edit `20260603_0001_post_tax_allocation_method.py`. In `upgrade()`, at the END (after the lines column rename block), add:
```python
    # Expand the runs method allowlist to gross + post_tax.
    with op.batch_alter_table("committed_allocation_runs") as batch:
        batch.drop_constraint("ck_committed_allocation_runs_method", type_="check")
        batch.create_check_constraint(
            "ck_committed_allocation_runs_method",
            "allocation_method IN "
            "('gross_revenue_proportional', 'post_tax_revenue_proportional')",
        )
```
In `downgrade()`, at the START (before the lines rename-back block), add the reverse:
```python
    # Restore the gross-only method CHECK.
    with op.batch_alter_table("committed_allocation_runs") as batch:
        batch.drop_constraint("ck_committed_allocation_runs_method", type_="check")
        batch.create_check_constraint(
            "ck_committed_allocation_runs_method",
            "allocation_method = 'gross_revenue_proportional'",
        )
```
Now that this migration does BOTH halves, update its module docstring + Purpose comment to describe both. Replace the Task 1 rename-only first line and drop the "(Task 4 extends…)" parenthetical with the final wording, e.g.:
```python
"""Rename committed_allocation_lines.basis_gross_usd -> basis_amount_usd and
expand the committed_allocation_runs method allowlist to post_tax.

Revision ID: 20260603_0001
...
"""
```
and update the `# Purpose:` comment to mention the runs method-allowlist expansion alongside the column rename. The docstring must match the migration's actual content at this final state.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python -m pytest tests/finance/test_committed_allocation.py tests/finance/test_account_allocation_read.py tests/db/test_committed_allocation_models.py -q`
Expected: PASS.
Run (PG): `python -m pytest tests/api/test_committed_allocation_api.py tests/db/test_committed_allocation_migration_postgres.py -q`
Expected: PASS.
Run: `python -m ruff check backend tests`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add backend/ums_smart_revenue/finance/committed_allocation.py backend/ums_smart_revenue/db/finance_models.py backend/ums_smart_revenue/db/alembic/versions/20260603_0001_post_tax_allocation_method.py tests/finance/test_committed_allocation.py tests/finance/test_account_allocation_read.py tests/db/test_committed_allocation_models.py tests/api/test_committed_allocation_api.py tests/db/test_committed_allocation_migration_postgres.py
git commit -m "feat(finance): commit post_tax_revenue_proportional allocations (service + DB allowlist)"
```

---

## Task 5: Align the dry-run net-source check to `(channel, source_kind)` grain

`/revenue/recalculate`'s dry-run currently checks net presence at channel grain, so it can report `READY_FOR_REVIEW` while the commit engine fails closed. Move it to `(channel, source_kind)` grain (a key is missing-net if any fact in the group has null net).

**Files:**
- Modify: `backend/ums_smart_revenue/finance/recalculation.py:113-128`, `:199-208` (issue message)
- Test: `tests/api/test_revenue_recalculation_api.py`

- [ ] **Step 1: Write the failing test**

Read `tests/api/test_revenue_recalculation_api.py` first. The route is **`POST /revenue/recalculate`** with `month` in the request body (see `recalculation_payload(**overrides)` and the existing `test_finance_admin_requests_recalculation_preview_with_audit`). Note: the file's shared `seed_database` puts ONE `source_kind` per channel, so its existing `source_summary` of `{net_revenue_source_count: 1, missing_net_revenue_source_count: 1}` holds identically under the new grain — the existing recalc tests are unaffected. To ISOLATE the grain change, this test needs its own seed: a single channel with net for one `source_kind` and null net for another, so channel grain would report "has net" (pass) but `(channel, source_kind)` grain reports one missing key (block). Reuse the file's `build_database_url`, `auth_headers`, `recalculation_payload`, and the imported ORMs/constants (`SECTOR_ID`, `COMPANY_ID`, `CHANNEL_A_ROW_ID`, `USER_ID`).

```python
def _seed_two_source_kinds(database_url: str) -> None:
    """One channel with YOUTUBE_CMS net=880 and ADSENSE net=None (grain isolation)."""
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            OrgUnitORM(id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV", active=True),
            OrgUnitORM(id=COMPANY_ID, parent_id=SECTOR_ID, type="COMPANY",
                       name="TV Company", active=True),
            YouTubeChannelORM(
                id=CHANNEL_A_ROW_ID, youtube_channel_id="channel-tv-a", channel_name="TV A",
                primary_org_unit_id=COMPANY_ID, cms_status="INSIDE_CMS",
                revenue_required=True, active=True,
            ),
            MonthlyChannelRevenueFactORM(
                id=uuid4(), month="2026-03", youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS", source_report_id="cms-1",
                gross_revenue_usd=Decimal("1000.00"), net_revenue_usd=Decimal("880.00"),
                views=1, watch_time_minutes=Decimal("1"),
                confidence_score=Decimal("0.95"), imported_by=USER_ID,
            ),
            MonthlyChannelRevenueFactORM(
                id=uuid4(), month="2026-03", youtube_channel_id="channel-tv-a",
                source_kind="ADSENSE", source_report_id="ad-1",
                gross_revenue_usd=Decimal("500.00"), net_revenue_usd=None,
                views=1, watch_time_minutes=Decimal("1"),
                confidence_score=Decimal("0.95"), imported_by=USER_ID,
            ),
            UserORM(id=USER_ID, email="recalculation@example.com",
                    display_name="Recalculation User"),
        ])
        session.commit()


def test_post_tax_dry_run_blocks_on_missing_source_kind_net(tmp_path):
    """A channel with net for YOUTUBE_CMS but null net for ADSENSE blocks post_tax
    under the (channel, source_kind) grain (channel grain would have passed)."""
    database_url = build_database_url(tmp_path)
    _seed_two_source_kinds(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.post(
        "/revenue/recalculate",
        headers=auth_headers("finance_admin", "global"),
        json=recalculation_payload(
            scope_type="global", scope_id=None,
            allocation_method="post_tax_revenue_proportional",
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "BLOCKED"
    assert any(
        i["issue_type"] == "NET_REVENUE_SOURCE_MISSING"
        for i in body["blocking_issues"]
    )
    assert body["source_summary"]["net_revenue_source_count"] == 1
    assert body["source_summary"]["missing_net_revenue_source_count"] == 1
```

The existing gross dry-run tests (`test_finance_admin_requests_recalculation_preview_with_audit`, etc.) must still pass unchanged — gross has no net requirement, and their single-`source_kind`-per-channel seed yields the same counts under both grains.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/api/test_revenue_recalculation_api.py -q`
Expected: FAIL — under the old channel-grain check, channel-tv-a counts as "has net" (its YOUTUBE_CMS fact has net), so no blocking issue is raised and `status` is `READY_FOR_REVIEW`.

- [ ] **Step 3: Implement the `(channel, source_kind)`-grain check**

`backend/ums_smart_revenue/finance/recalculation.py:113-119` — replace the channel-grain computation:
```python
    source_channel_ids = {fact.youtube_channel_id for fact in fact_list}
    source_keys = {
        (fact.youtube_channel_id, fact.source_kind) for fact in fact_list
    }
    # A (channel, source_kind) key is missing-net if ANY fact in that group has
    # null net (mirrors the commit engine's fail-closed null-net omission), so
    # the dry-run cannot report READY while commit would go UNALLOCATED.
    null_net_keys = {
        (fact.youtube_channel_id, fact.source_kind)
        for fact in fact_list
        if fact.net_revenue_usd is None
    }
    net_revenue_source_keys = source_keys - null_net_keys
    missing_net_revenue_count = len(null_net_keys)
```
Then the `RecalculationSourceSummary` construction (`:120-129`) uses the new key counts:
```python
        net_revenue_source_count=len(net_revenue_source_keys),
        missing_net_revenue_source_count=missing_net_revenue_count,
```
(`source_channel_count=len(source_channel_ids)` stays channel-grain.)

Update the blocking-issue message (`:199-208`) to name the grain:
```python
                message=(
                    f"{missing_net_revenue_source_count} scoped "
                    f"(channel, source kind) source(s) in {month} have no net "
                    f"revenue for {allocation_method}."
                ),
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/api/test_revenue_recalculation_api.py -q`
Expected: PASS.
Run: `python -m ruff check backend tests`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add backend/ums_smart_revenue/finance/recalculation.py tests/api/test_revenue_recalculation_api.py
git commit -m "fix(finance): align recalculate net-source check to (channel, source_kind) grain"
```

---

## Task 6: Docs status update + export regression + full validation gate

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md`
- Test: `tests/api/test_exports_account_allocation.py` (rename-regression only)

- [ ] **Step 1: Export-path rename regression**

Read `tests/api/test_exports_account_allocation.py`. If it constructs `AllocationLine` fixtures, update any `basis_gross_usd=` → `basis_amount_usd=` (the inventory showed no `basis_gross_usd` hit in this file, so it likely consumes results rather than constructing lines — confirm with Grep). Add/keep an assertion that an export over a committed **post_tax** month still renders only the allocation-source disclosure token (no per-line basis field leaks into the export payload). Run:

Run: `python -m pytest tests/api/test_exports_account_allocation.py -q`
Expected: PASS.

- [ ] **Step 2: Update Docs/15_DELIVERY_BACKLOG.md**

Read the file, find the Phase 4 Spec 2b allocation-methods line, and mark `post_tax_revenue_proportional` as implemented (committable) inline with a done mark, noting `company_level` / `manual` / `no_allocation` remain out. Keep the existing format; no new tracker file.

- [ ] **Step 3: Update Docs/01_IMPLEMENTATION_PLAN.md**

Read the file, update the allocation-methods status section: gross + post_tax are now committable; basis field renamed to `basis_amount_usd`; dry-run net check now `(channel, source_kind)`-grain. Mark remaining methods + PAYMENT-grain as still pending.

- [ ] **Step 4: Full validation gate**

```bash
python -m ruff check backend tests scripts
git diff --check origin/main...HEAD
```
Then the full suite with the Postgres container. This repo is PowerShell-first; set the env var and run pytest in a SINGLE PowerShell invocation (shell state does not persist between separate invocations):
```powershell
# (container ums-mig-pg-test on :55432 must be running)
$env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/test_ums"
python -m pytest -q
```
(Bash equivalent, if using the Bash tool: `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:ums@localhost:55432/test_ums python -m pytest -q`.)
Expected: ruff clean; `git diff --check` clean; full suite green.

The authoritative up/down check is the in-process migration test `test_committed_allocation_migration_postgres.py` (it builds a fresh schema, runs `command.upgrade(..., "head")`, and exercises the new migration) — confirm it is green in the full-suite run above. As a supplementary CLI round-trip on the disposable container (only if `alembic.ini` resolves its URL from the environment in this repo — otherwise rely on the in-process test), run all lines in ONE PowerShell invocation:
```powershell
$env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/test_ums"
alembic -c alembic.ini upgrade head
alembic -c alembic.ini downgrade -1
alembic -c alembic.ini upgrade head
```
Expected: clean up → down → up (column renamed; method CHECK swapped; finite CHECK enforced on `basis_amount_usd`).

- [ ] **Step 5: Commit**

```bash
git add Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md tests/api/test_exports_account_allocation.py
git commit -m "docs(finance): mark post_tax allocation method implemented + export rename regression"
```

---

## Final review

After all tasks: dispatch a final code-reviewer over the whole branch diff (spec-compliance + finance-correctness + no stale `basis_gross_usd` in active code/tests + migration up/down + auth/lock semantics unchanged), then use `superpowers:finishing-a-development-branch`. **Do not push, open a PR, or merge** — present the branch state and await Mahmoud's explicit authorization (he merges himself).
