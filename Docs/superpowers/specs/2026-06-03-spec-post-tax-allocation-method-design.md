# Post-Tax Allocation Method — Design (Phase 4 Spec 2b)

**Status:** Approved design — implementation pending plan + plan approval · **Date:** 2026-06-03 · **Base:** branch from current `origin/main` `f4e8cf7` (PR #66 merged; verified HEAD/main/origin/main aligned, no open PRs)

## 1. Purpose & scope

Add `post_tax_revenue_proportional` as a second **committable** account-allocation method alongside the only method implemented today, `gross_revenue_proportional`. One backend PR:

1. Parameterize the allocation engine by `allocation_method`.
2. Select the gross-vs-net **basis** in the orchestrator; reuse the Hamilton distribution unchanged.
3. Un-gate the commit path (service gate, DB CHECK, migration) to the two-method allowlist.
4. Align the `/revenue/recalculate` dry-run's net-source check to the engine's `(channel, source_kind)` grain (correctness fix).
5. Rename the persisted basis field `basis_gross_usd` → `basis_amount_usd` for honest, method-neutral semantics (no compatibility alias).

**Out of scope:** `company_level`, `manual`, `no_allocation`; PAYMENT-grain allocation; the `/revenue/recalculate` committed-write path (stays dry-run-only); any change to lock/commit semantics, authorization gates, or net-revenue / explanation / export *math*.

## 2. Decided finance semantics

The post_tax (net) proportional weight is **source `RevenueFactEntry.net_revenue_usd`**, built per `(youtube_channel_id, source_kind)` exactly source-aligned with today's gross basis. Fail-closed; **never** falls back to gross; **never** uses derived net (gross − allocated deductions), which would be circular. Source net is nullable but **non-negative** — enforced at both layers: the DB CHECK `net_revenue_usd IS NULL OR net_revenue_usd >= 0` (`backend/ums_smart_revenue/db/finance_models.py:191-192`) and the validator `net_revenue_usd must be a finite decimal >= 0` (`backend/ums_smart_revenue/finance/revenue_facts.py:430-431`). The non-positive-total fail-closed case is therefore practically a **zero** total → issue code `ZERO_NET_BASIS`.

## 3. Architecture (Approach 1 — parameterized basis in the orchestrator)

The conserved largest-remainder distribution `_proportional_allocation` (`backend/ums_smart_revenue/finance/allocation.py:60-94`) is already basis-agnostic — it splits an amount across `(channel, weight)` pairs — so it is **reused unchanged**. Only the basis *number* differs per method, and basis construction already lives in the orchestrator, so that is where the method branches.

### 3.1 Orchestrator basis selection

`compute_month_account_allocation` (`backend/ums_smart_revenue/finance/allocation_inputs.py:38`) gains `allocation_method: str = "gross_revenue_proportional"` and builds the `(channel, source_kind) → Decimal` basis map from:

- **gross:** `Σ fact.gross_revenue_usd` (today's behavior at `allocation_inputs.py:51-54`, unchanged).
- **post_tax:** `Σ fact.net_revenue_usd`, **omitting any `(channel, source_kind)` key for which *any* fact in that group has null net** — no silent partial-net sums. Concretely: collect the keys that have a null-net fact, sum net for the rest, then drop every null-net key from the map so the engine sees those channels as missing-basis.

It then passes the map and the method to the engine. The defaulted kwarg means the **read-switch live paths stay on gross**: `compute_month_account_allocation`'s only non-commit caller, `resolve_month_account_allocation` (`backend/ums_smart_revenue/finance/account_allocation_read.py:170`), passes no method and keeps gross; the live-read endpoints are unchanged (see §9).

### 3.2 Method-threaded engine

`build_account_allocation` (`allocation.py:338`) and `_allocate_component` (`allocation.py:205`) take the generic basis map + `allocation_method` (defaulted to gross). They thread the method into `AccountAllocationResult.allocation_method` and select the method-aware zero-basis issue code and detail text:

- `BASIS_MISSING` / `BASIS_INCOMPLETE` keep their (already method-neutral) names; their detail strings become method-neutral ("source-aligned basis" rather than "source-aligned gross").
- The zero-basis code `ZERO_GROSS_BASIS` becomes `ZERO_NET_BASIS` for post_tax. The `basis_total <= 0` guards (`allocation.py:268-275` and `_proportional_allocation` at `:76-80`) are unchanged in logic; for post_tax the trigger is a zero net total.

The `gross_basis` parameter (`allocation.py:208,343`) is renamed to the method-neutral `basis`; internal local variable names that say `gross` (e.g. the loop var at `allocation.py:292`, `gross_basis` construction) are renamed to `weight` / `basis` for honest semantics. The module docstring and contract comments that say "raw-gross-proportional" are updated to cover both methods.

## 4. Un-gate the commit path

- **Service gate:** `committed_allocation.py:141` changes from `if allocation_method != ALLOCATION_METHOD` (single gross method) to an allowlist check — reject with `CommittedAllocationValidationError("unsupported allocation method: …")` for anything outside `{gross_revenue_proportional, post_tax_revenue_proportional}`. The `compute_month_account_allocation` call (`committed_allocation.py:146`) passes `allocation_method=allocation_method`. Reject-on-unallocated (`:152-155`) is unchanged — post_tax fails closed to `UNALLOCATED` exactly like gross, and the run is rejected if any component is unallocated.
- **API request:** `CommitAllocationRequest.allocation_method` (`backend/ums_smart_revenue/api/allocation.py:65`) keeps its gross default; the commit route already threads it (`api/allocation.py:371`). `_request_fingerprint` (`api/allocation.py:89-94`) already includes `allocation_method`, so a gross commit and a post_tax commit produce distinct idempotency fingerprints automatically — no fingerprint change needed.

## 5. Dry-run alignment (correctness fix)

`backend/ums_smart_revenue/finance/recalculation.py` currently counts missing net at **channel** grain: `net_revenue_channel_ids = {fact.youtube_channel_id … if fact.net_revenue_usd is not None}` and `missing = len(source_channel_ids - net_revenue_channel_ids)` (`recalculation.py:113-119`). A channel with net for one `source_kind` but null net for another counts as "has net," so `build_recalculation_preview` can return `READY_FOR_REVIEW` while the commit engine fails that `(channel, source_kind)` closed to `UNALLOCATED`.

Fix: redefine the net-source check at **`(channel, source_kind)`** grain, mirroring §3.1. A "net source" is a `(channel, source_kind)` key; it is *missing* if any fact in that group has null net. `RecalculationSourceSummary.net_revenue_source_count` and `missing_net_revenue_source_count` (`recalculation.py:43-44`, populated at `:119-128`) are recomputed at this grain, and the `NET_REVENUE_SOURCE_MISSING` blocking issue (`recalculation.py:194-208`) fires on the stricter condition. `NET_REVENUE_REQUIRED_METHODS` (`recalculation.py:16`) is unchanged.

## 6. Migration (new file on top of the merged migration)

A new Alembic migration `backend/ums_smart_revenue/db/alembic/versions/20260603_0001_post_tax_allocation_method.py`, `revision = "20260603_0001"`, `down_revision = "20260602_0001"`. The merged migration `20260602_0001_committed_account_allocation.py` is **not edited** (it is a point-in-time record that created `basis_gross_usd`; the new migration renames the column going forward).

**Upgrade:**
1. Expand the runs method CHECK: drop `ck_committed_allocation_runs_method` and recreate it as `allocation_method IN ('gross_revenue_proportional','post_tax_revenue_proportional')` (replacing the `= 'gross_revenue_proportional'` expression at the model `finance_models.py:944` / merged migration `20260602_0001:100-103`).
2. Drop the Postgres-only finite CHECK `ck_committed_allocation_lines_amounts_finite` (dialect-guarded), which references `basis_gross_usd` (`finance_models.py:1010-1018`).
3. Rename column `committed_allocation_lines.basis_gross_usd` → `basis_amount_usd`.
4. Recreate the finite CHECK (dialect-guarded) with its expression referencing `basis_amount_usd`.

**Downgrade** reverses all four (drop recreated CHECK → rename column back → recreate original finite CHECK on `basis_gross_usd` → restore the runs method CHECK to gross-only).

The ORM model (`finance_models.py`) is updated to the final state — `basis_amount_usd` column (`:997`) and the finite CHECK expression (`:1011-1012`) referencing it — so `alembic` autogenerate parity holds (model state == migration-head state). SQLite unit tests build via `create_all` from the renamed model directly; the Postgres migration test exercises the up/down DDL.

## 7. The `basis_gross_usd` → `basis_amount_usd` rename (active code; no alias)

The persisted basis field stores *the weight that drove this line's split*; for a post_tax line that weight is net, so `basis_gross_usd` becomes a misleading number-source label (rule #4). Renamed everywhere it is an **active** identifier — **9 active-code occurrences across 6 files** (verified inventory):

| File | Lines | Role |
|------|-------|------|
| `backend/ums_smart_revenue/finance/allocation.py` | 107, 287 | `AllocationLine` dataclass field + construction |
| `backend/ums_smart_revenue/finance/committed_allocation.py` | 189 | persist into `CommittedAllocationLineORM` |
| `backend/ums_smart_revenue/finance/account_allocation_read.py` | 61 | `rebuild_result_from_run` reconstruction |
| `backend/ums_smart_revenue/api/allocation.py` | 162, 426 | API JSON key on GET + commit responses |
| `backend/ums_smart_revenue/db/finance_models.py` | 997, 1011, 1012 | ORM column + finite CHECK expression |

**API response-shape change:** the JSON key `"basis_gross_usd"` becomes `"basis_amount_usd"` on the two account-allocation endpoints (`api/allocation.py:162` GET line serializer, `:426` commit response). This is an intentional, pre-alpha breaking rename with **no compatibility alias**.

**No other output surface carries it (corrects an earlier overstatement):**
- The explanation builder surfaces only `basis_share` per line (`backend/ums_smart_revenue/finance/explanations.py:342`), **not** `basis_gross_usd`; explanation output is unaffected by the rename.
- The report/export builders (`reports/finance_workbook.py`, `executive_pdf.py`, `branded_slide_pack.py`) surface no per-line basis — only the account-allocation disclosure token; export output is unaffected.

So the renamed field is consumed in output only by the two account-allocation API endpoints. `test_explanations.py` and `test_exports_account_allocation.py` are touched only because they construct `AllocationLine` fixtures, not because explain/export payloads carry the field.

## 8. Rename scope across docs & history (redline decision)

The `basis_gross_usd` identifier also appears in non-active artifacts that this PR **deliberately does not rewrite**:

- **The merged migration `20260602_0001` (2 occurrences, lines 127, 213):** not edited — per the explicit directive, the new migration (§6) performs the rename forward; the merged migration accurately records the column it originally created.
- **Historical planning docs/specs (16 occurrences across 7 files):** the `2026-05-31` / `2026-06-01` / `2026-06-02` / `2026-06-03` allocation specs and plans are point-in-time records of already-merged PRs (#58–66). They are **not** rewritten — doing so would falsify the historical record and violate CLAUDE.md rule #12 ("never remove planning docs … unless the operator explicitly asks"). They remain accurate as descriptions of what those PRs shipped at the time.

**Scope statement:** this PR updates `basis_gross_usd` → `basis_amount_usd` in **active backend code + active tests** only, plus this new spec, the new plan, and the new migration. Historical docs/plans and the merged migration keep their original text. No memory files reference the identifier.

## 9. No public live-read method selector (redline wording)

post_tax is **computed live in-process at commit time, then persisted** — the commit endpoint `POST …/account-allocations/commit` accepts `allocation_method` in its request body (a write path) and runs `compute_month_account_allocation` with that method. **No public live-read method selector is added.** The account-allocation GET endpoint takes only `adsense_account_id` (`api/allocation.py:220-242`); `resolve_month_account_allocation` has no `allocation_method` parameter and keeps gross for live computation. post_tax is therefore externally exposed only through **committed snapshots and their reconstruction** (`rebuild_result_from_run`), for LOCKED months, via the existing read-switch.

## 10. Tests (focused, complete file set)

Verified the full set of affected test files (12). Strict TDD per the plan.

**Engine / orchestrator**
- `tests/finance/test_allocation.py` — post_tax basis distribution; method-neutral `basis` param + ~13 `build_account_allocation` / `gross_basis=` call sites updated to `basis=`; `ZERO_NET_BASIS` at zero net total; `basis_amount_usd` field; gross path byte-identical regression; conservation holds for post_tax.
- `tests/finance/test_allocation_inputs.py` — `compute_month_account_allocation(allocation_method=…)`: gross map unchanged; post_tax map built from net with `(channel, source_kind)` null-net omission; default kwarg = gross.

**Commit + reconstruction**
- `tests/finance/test_committed_allocation.py` — post_tax commit persists `allocation_method='post_tax_revenue_proportional'` + `basis_amount_usd`; gross still commits; out-of-allowlist method (e.g. `company_level`) rejected; reject-on-unallocated for post_tax.
- `tests/finance/test_account_allocation_read.py` — `rebuild_result_from_run` round-trips a post_tax snapshot losslessly (incl. `basis_amount_usd`); live path stays gross.
- `tests/api/test_committed_allocation_api.py` — commit API accepts post_tax; response JSON uses `basis_amount_usd`; idempotency fingerprint differs by method.
- `tests/api/test_allocation_api.py` — GET line serializer emits `basis_amount_usd` (key renamed).

**Dry-run**
- `tests/api/test_revenue_recalculation_api.py` — a channel with net for one `source_kind` but null for another → `NET_REVENUE_SOURCE_MISSING` blocks for post_tax (would have passed under the old channel-grain check); gross dry-run unaffected; source-summary counts at the new grain.

**DB / migration**
- `tests/db/test_committed_allocation_models.py` — ORM column is `basis_amount_usd`; runs row with post_tax method inserts; bogus method rejected (model CHECK).
- `tests/db/test_committed_allocation_migration_postgres.py` — Postgres up/down: method CHECK accepts both methods and rejects a third; column renamed; finite CHECK still rejects non-finite `basis_amount_usd`.

**Rename regressions (fixtures only)**
- `tests/finance/test_explanations.py`, `tests/finance/test_net_revenue_account_allocations.py`, `tests/api/test_exports_account_allocation.py` — `AllocationLine` fixtures updated to `basis_amount_usd`; assert explain still surfaces `basis_share` and exports still surface only the disclosure token (no behavior change).

## 11. Non-goals

No `company_level` / `manual` / `no_allocation` methods. No PAYMENT-grain allocation. No `/revenue/recalculate` committed-write path (stays dry-run-only and continues to reject `dry_run=false`). No change to lock/commit semantics, authorization gates, trusted-gateway/principal loading, or net-revenue / explanation / export *math* (they consume the snapshot, which now may carry a post_tax method and the renamed field). No new live-read method selector.

## 12. Database & blast-radius statement

- **Tables/ORM affected:** `committed_allocation_runs` (method CHECK), `committed_allocation_lines` (column rename + finite CHECK), `CommittedAllocation*` ORM + `AllocationLine` dataclass. Reads `monthly_channel_revenue_facts.net_revenue_usd` (no schema change there).
- **Source of truth:** PostgreSQL/warehouse remains the financial source of truth.
- **Authorization/audit:** unchanged — no gate is weakened; the commit path keeps its existing permission checks and `ALLOCATION_COMMITTED` audit.
- **Finance results:** post_tax produces a *new* committable allocation; existing gross snapshots and all current readers are unchanged. Fail-closed (`UNALLOCATED`) discipline preserved and extended to post_tax.
- **Graph projection:** **No graph projection impact detected** — the committed-allocation tables and `AllocationLine` are not projected to Neo4j (read-only projection unaffected; search-backed).
- **Migration safety:** additive method allowlist + a column rename on a pre-alpha, disposable-data table; up/down both provided and Postgres-tested. **Disposable pre-alpha data reset accepted** is not required (rename preserves existing rows); no backfill needed.

## 13. Validation gate (run before any push)

- `python -m ruff check backend tests scripts`
- Targeted: `python -m pytest` on the 12 files in §10.
- Full suite: `python -m pytest -q` with the Postgres container (`UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:ums@localhost:55432/test_ums`, container `ums-mig-pg-test`).
- Alembic up/down review for `20260603_0001` against the disposable Postgres database.
- `git diff --check`.

No push, PR, or merge without explicit authorization; every commit message trailer-free.
