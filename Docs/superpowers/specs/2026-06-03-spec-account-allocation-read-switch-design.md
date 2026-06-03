# Account-Allocation Read-Switch — Design (Phase 4 Spec 2b PR-6)

**Status:** Draft for review · **Date:** 2026-06-03 · **Branch:** `spec/account-allocation-read-switch` (off `main` `7c06670`, PR-5 merged)

## 1. Goal

Make the four account-allocation **readers** prefer the committed snapshot persisted by PR-5
over live compute, with a defined lock-aware selection policy, lossless reconstruction, and
full source provenance on every surface. This is the deferred read half of the persisted
allocation feature; PR-5's write path is unchanged.

## 2. Scope / non-goals

In scope — switch these four live `compute_month_account_allocation` call sites (all paths use
`backend/ums_smart_revenue/`):

| # | Reader | Call site |
|---|---|---|
| 1 | Allocation GET (`get_account_allocations`) | `api/allocation.py:248` |
| 2 | Net-revenue GET (`get_month_net_revenue`) | `api/revenue.py:1130` |
| 3 | Explain net-metric (`explain_channel_month_revenue_metric`) | `api/revenue.py:1408` |
| 4 | Exports (`_build_finance_source_summaries_for_export`) | `api/exports.py:1085` |

Non-goals (explicitly deferred): PAYMENT-grain allocation (no `bank_reference → source_account_id`
resolver exists); other allocation methods beyond `gross_revenue_proportional`; any change to
`POST /revenue/recalculate`, the commit endpoint, the write path, lock/commit semantics, auth
gates, or the DB schema. **No migration.** PostgreSQL stays the source of truth; no Neo4j /
graph-projection impact.

## 3. Selection policy (lock-aware + fallback)

The resolver reads finance-month close status with the existing pure-read
`get_month_close_status(session, month, *, tenant_id=None)`
(`backend/ums_smart_revenue/finance/month_close.py:206` — SELECT only, no advisory/FOR UPDATE
lock, never mutates close state):

```
tenant_id = committed_repository.tenant_id            # single tenant source (see §4)
status = get_month_close_status(session, month, tenant_id=tenant_id)
if status == "LOCKED":
    outcome = committed_repository.get_latest_committed(month)   # highest commit_version
    if outcome is not None:
        -> rebuild result from snapshot;  provenance = committed_snapshot(version, committed_at, run_id)
    else:
        -> live compute;                  provenance = live_fallback
else:  # "OPEN" or None (no close row)
    -> live compute;                      provenance = live_compute
```

Rationale: OPEN months still accept new data, so live compute is correct (a snapshot committed
on an open month is a draft). A LOCKED month is frozen, so its latest committed snapshot is the
authoritative number; if a month was locked without ever committing an allocation, live compute
is the safe, non-erroring fallback (flagged distinctly as `live_fallback`).

## 4. Central resolver — `backend/ums_smart_revenue/finance/account_allocation_read.py` (new)

```python
@dataclass(frozen=True)
class AllocationProvenance:
    source: str                      # "committed_snapshot" | "live_compute" | "live_fallback"
    commit_version: int | None       # set only for committed_snapshot
    committed_at: datetime | None    # set only for committed_snapshot
    run_id: UUID | None              # set only for committed_snapshot


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
    ...
```

The resolver is the single decision point; all four readers call it instead of
`compute_month_account_allocation`. Live compute is delegated unchanged (the `adsense_account_id`
is forwarded so the live path filters components exactly as today). Snapshot reconstruction is
below.

**Single tenant source (Redline #2-clarify):** the resolver takes **no** separate `tenant_id`
parameter. It derives the one tenant from `committed_repository.tenant_id` (a new public
property, §4.3) and passes that exact value to `get_month_close_status(session, month,
tenant_id=...)`, so the close-status read and the committed-run lookup can never diverge
cross-tenant. The deduction/revenue/link repos are constructed with the same request tenant by
their DI providers.

### 4.1 Reconstruction — `rebuild_result_from_run(outcome) -> AccountAllocationResult`

`CommitAllocationOutcome` (run + lines + unallocated + notes) maps **field-for-field** to the
live dataclasses (verified identical):

- `AllocationLine` ← `CommittedAllocationLineORM` (all 10 fields)
- `UnallocatedIssue` ← `CommittedAllocationUnallocatedORM` (all 6 fields)
- `AllocationNote` ← `CommittedAllocationNoteORM` (all 3 fields)
- `AllocationSummary` ← the run header's 7 persisted summary columns (used directly — **not**
  recomputed — so the full-month reconstruction is lossless, including count fields)
- `month` ← `run.month`; `allocation_method` ← `run.allocation_method`

### 4.2 Account-scoped filtering — `filter_committed_result_to_account(result, adsense_account_id)`

(Redline #2) Live compute with an `adsense_account_id` builds lines, **notes, and summary** from
the *filtered* component set (`backend/ums_smart_revenue/finance/allocation.py:327` notes,
`:340-356` summary). A snapshot filter must reproduce that, not merely drop lines/unallocated:

- `lines`   = lines where `adsense_account_id == account`
- `unallocated` = issues where `scope_id == account`
- `notes`   = `()` — the only note kind, `CHANNEL_IN_MULTIPLE_ACCOUNTS`, is produced by
  `_multi_account_notes(verified_channels)` from the cross-account map; a single-account view
  has exactly one account in scope, so live single-account compute emits no such note. Dropping
  notes under an account filter matches live. (A test pins this; if a future note kind is
  per-account, this rule is revisited.)
- `summary` = recomputed for the filtered set via a shared helper (next section).

To avoid drift, extract a pure helper from `build_account_allocation`:

```python
def summarize_account_allocation(
    *, component_count: int, allocated_component_count: int,
    lines: Sequence[AllocationLine], unallocated: Sequence[UnallocatedIssue],
) -> AllocationSummary:
    # 4 totals summed over lines/unallocated (the drift-prone arithmetic),
    # unallocated_component_count = len(unallocated)
```

`build_account_allocation` is refactored to call it (passing its loop-derived
`component_count` / `allocated_component_count`), so the live path is byte-identical. The
account filter derives the two count inputs from the filtered snapshot rows:

- `component_count` = number of **distinct `component_key`** across (filtered lines ∪ filtered unallocated)
- `allocated_component_count` = number of distinct `component_key` in filtered lines

**Known, tested edge (surfaced for review):** a *zero-amount no-op* component produces no line
and no unallocated issue, so it leaves no row in the snapshot. Live single-account compute would
still count it in `component_count` / `allocated_component_count`. Therefore a per-account
snapshot summary can under-count **only those two count fields** for accounts that contain a
zero-amount component; **all four monetary totals remain exact**. The spec accepts this as the
defined semantic of a per-account snapshot view (a snapshot counts components that produced
evidence). §9 tests both the normal-case equality and this specific count behavior — it is
proven, not assumed.

### 4.3 Repository addition — `backend/ums_smart_revenue/finance/committed_allocation.py`

Add a public read method (reuses the existing `get_latest_run` at `:235` and the private
child-loaders in `_replay` at `:214`) plus a public tenant accessor (mirrors
`finance/channel_account_links.py:206`, the single-tenant source for §3/§4):

```python
@property
def tenant_id(self) -> UUID:
    """The tenant UUID this repository is scoped to (read-only)."""
    return self._tenant_id

def get_latest_committed(self, month: str) -> CommitAllocationOutcome | None:
    run = self.get_latest_run(month)
    return None if run is None else self._replay(run)  # created flag is irrelevant for reads
```

No schema change.

## 5. Reader integrations (each swaps its direct compute call for the resolver)

All readers already hold `session` (`current_db_session`) and the three repos; they additionally
resolve `committed_repository`. **Import-cycle fix (Redline #1):** `api/allocation.py:24` already
imports the tenant-aware repo providers FROM `api/revenue.py`, so the committed-repo provider must
not stay in `allocation.py` (a reader in `revenue.py` importing it would create
`revenue.py → allocation.py → revenue.py`). **Relocate** `current_committed_allocation_repository`
from `api/allocation.py:54` to `api/revenue.py` beside the existing repo providers (~`:360`), and
import it from `revenue.py` in `api/allocation.py` (for both the existing POST commit route at
`:332` and the new GET resolver call) and in `api/exports.py`. One definition, no duplicate
provider, no cycle.

1. **Allocation GET** (`api/allocation.py:248`): call resolver (forwarding `adsense_account_id`);
   `_result_to_api(result)` (`api/allocation.py:150`) gains a provenance block (§6.1).
2. **Net-revenue GET** (`api/revenue.py:1130`): call resolver; `result.lines` still flows through
   the unchanged `filter_account_allocations_to_scope(result.lines, channel_ids)`
   (`finance/net_revenue.py:587`) and `build_month_net_revenue_summary`; the response gains the
   provenance block (§6.1).
3. **Explain net-metric** (`api/revenue.py:1408`): call resolver; thread provenance into
   `build_channel_month_revenue_explanation(..., account_allocation_provenance=...)` (§6.2).
4. **Exports** (`api/exports.py:1085`): `_build_finance_source_summaries_for_export` calls the
   resolver; the provenance is carried on the source bundle and rendered by the artifact builders
   (§6.3).

`filter_account_allocations_to_scope` and `build_month_net_revenue_summary` are **source-agnostic**
(operate on `AllocationLine`s) and are unchanged.

## 6. Provenance surfacing (additive only)

Shared serializer `allocation_provenance_to_api(p) -> dict` (in the new read module):

```json
{ "allocation_source": "committed_snapshot",
  "committed_run": { "commit_version": 3,
                     "committed_at": "2026-05-31T12:00:00+00:00",
                     "run_id": "…uuid…" } }
```
For `live_compute` / `live_fallback`: `allocation_source` is set and `committed_run` is `null`.

### 6.1 Allocation GET + net-revenue GET (API JSON)
Add top-level keys `allocation_source` and `committed_run` to:
- the `_result_to_api(...)` dict (`api/allocation.py:150`), and
- the net-revenue response dict built in `get_month_net_revenue` (`api/revenue.py`).

### 6.2 Explain (persisted JSON) — Redline #4
The account-allocated component dict is built at
`backend/ums_smart_revenue/finance/explanations.py:323-340` (keys `key`, `label`, `value`,
`count`, `allocations`). Add two keys to **that dict**:

```python
{ "key": "account_allocated_deduction_usd", "label": "Account-allocated deductions",
  "value": ..., "count": ..., "allocations": [...],
  "allocation_source": "committed_snapshot",                 # NEW
  "committed_run": {"commit_version": 3, "committed_at": "…", "run_id": "…"} | None }  # NEW
```

Threaded via a new `account_allocation_provenance: AllocationProvenance | None = None` kwarg on
`build_channel_month_revenue_explanation` (`:140`) → `_build_net_revenue_explanation` (`:229`).
Because the explanation's `components` are persisted by
`SqlAlchemyNumberExplanationRepository.record_explanation` (`explanations.py:103`), the two new
keys are persisted to `number_explanations` with no migration (JSON payload). When the metric is
not net or provenance is absent, the keys are omitted/`null` (back-compatible).

### 6.3 Exports (payload + artifacts) — Redline #3
The export source bundle `_FinanceExportSourceSummaries` (`api/exports.py:107-114`) currently has
**no** provenance field. Changes:

- **Bundle field:** add `account_allocation_provenance: AllocationProvenance` to
  `_FinanceExportSourceSummaries`; set it from the resolver in
  `_build_finance_source_summaries_for_export`.
- **Builder params (Redline #3):** `build_finance_workbook_xlsx(preview)`
  (`reports/finance_workbook.py:169`) takes ONLY the preview — it gets **no** new kwarg. Instead
  add `account_allocation_provenance: AllocationProvenance` as a keyword to
  `build_finance_workbook_preview` (`reports/finance_workbook.py:122`) and **store it on the
  returned `FinanceWorkbookPreview`**; `build_finance_workbook_xlsx` reads
  `preview.account_allocation_provenance`. Add the same keyword to `build_executive_pdf_report`
  (`reports/executive_pdf.py:105`) and `build_branded_slide_pack_report`
  (`reports/branded_slide_pack.py:118`), passed from the bundle at the export builder call sites —
  PDF at `api/exports.py:635`, PPTX at `api/exports.py:754`, workbook preview at
  `api/exports.py:840`.
- **Preview API payload:** add `"account_allocation_provenance"` (the §6 dict) to the
  `source_summaries` map in `FinanceWorkbookPreview.to_api` (`reports/finance_workbook.py:113`),
  read from the provenance stored on the preview.
- **Rendered artifact fields** (a single human-readable disclosure token
  `Account allocation: committed snapshot v{n} ({YYYY-MM-DD})` / `… live compute` /
  `… live fallback`, formatted by a shared `account_allocation_disclosure_token(p)` helper):
  - **XLSX:** a labeled disclosure row `Account allocation source` rendered adjacent to the
    existing channel-direct vs account-allocated split on the Deductions/Channel-Breakdown sheet.
  - **PDF:** a labeled line `Account allocation source: <token>` in the executive-summary
    metadata block (alongside the PR-4 gross/net split rows).
  - **PPTX:** a footer/notes bullet `Account allocation: <token>` on the deductions slide.

  (The implementation plan pins exact cell/row/line coordinates; the spec fixes the field names,
  the carrying payload, and the per-artifact label + location.)

## 7. Error handling / fallback

- Missing close row (`status is None`) → `live_compute` (treated as open).
- LOCKED with no committed run → `live_fallback` (never errors).
- The resolver performs no writes and acquires no lock (status read is pure). Typed errors from
  live compute or the repos propagate unchanged. No new HTTP error states; all four endpoints
  keep their current status codes and auth gates.

## 8. Blast radius

- **API contract:** additive only (`allocation_source` + `committed_run` on two API responses
  and the explain JSON; provenance keys + a disclosure token on the three export artifacts +
  preview payload). No field removed or renamed.
- **DB:** none (read-only consumption of PR-5 tables). No migration.
- **Auth/audit:** unchanged gates and audit events on all four readers.
- **Coupling:** readers gain a dependency on `committed_repository` + the pure close-status read.
- **Refactor:** `summarize_account_allocation` extracted from `build_account_allocation`
  (live path byte-identical, guarded by existing allocation tests).

## 9. Testing

Resolver / reconstruction (new `tests/finance/test_account_allocation_read.py`):
- LOCKED + snapshot exists → `committed_snapshot` provenance (correct `commit_version`/`run_id`);
  rebuilt `AccountAllocationResult` equals the live result for the same frozen inputs
  (reconstruction fidelity).
- LOCKED + no run → `live_fallback`. OPEN / no close row → `live_compute`.
- `filter_committed_result_to_account`: for a **multi-account** fully-allocated month,
  snapshot-account-filtered result == live-account-filtered compute, **per account** (lines,
  unallocated, notes empty, summary totals + count fields) — the normal-case equivalence.
- A dedicated test pins the zero-amount-no-op count behavior (§4.2): totals exact; the two count
  fields reflect only components that produced rows.

Reader-level:
- Each of the four readers: LOCKED month serves snapshot numbers + provenance; OPEN month serves
  live + `live_compute`.
- **The PR-5 OPEN-month reader-untouched regression stays green** (OPEN → live, so net-revenue is
  byte-identical before/after committing on an open month); add LOCKED-path counterparts asserting
  the snapshot now drives the number.
- Explain: persisted `number_explanations` carries `allocation_source` + `committed_run` on the
  account component for a LOCKED month.
- Exports: each artifact (XLSX/PDF/PPTX) + the preview payload render the disclosure token, and it
  reads `committed snapshot v{n}` for a LOCKED month vs `live compute` for an OPEN month.

Validation gate: `python -m ruff check backend tests scripts`; targeted pytest for the four
reader test modules + the new resolver module; full `python -m pytest -q` with the Postgres
container; `git diff --check`.

## 10. File inventory (all `backend/ums_smart_revenue/`)

Create: `finance/account_allocation_read.py`. Modify: `finance/committed_allocation.py`
(`get_latest_committed` + public `tenant_id` property), `finance/allocation.py` (extract
`summarize_account_allocation`), `api/revenue.py` (host the relocated
`current_committed_allocation_repository` + resolver call), `api/allocation.py` (import the
provider from `revenue.py`; resolver call), `api/exports.py`, `finance/explanations.py`,
`reports/finance_workbook.py`, `reports/executive_pdf.py`, `reports/branded_slide_pack.py`.
Tests under `tests/finance/`, `tests/api/`. Docs/01 + Docs/15 status (final task).
