# Account-Level Deduction Allocation — Design Spec

**Phase:** Phase 4 reconciliation — **Spec 2b, PR-1** (the allocation engine, first
slice). This is the carved-out compute+read slice of the original "Spec 2 = allocation
rules" note. The canonical channel↔account map it consumes shipped as **Spec 2a**
(`2026-05-31-spec-channel-account-map-design.md`, merged PR #57).

**Status:** Designed 2026-05-31. Off `main` (`714fde8`, which already has the MERGED
Phase 4 PR-A `deduction_components` substrate/ingestion, PR-B net-revenue consumption +
read endpoint, and Spec 2a channel↔account map substrate). Branch `spec/account-allocation`.

**Goal:** Distribute **ACCOUNT-grain** deduction evidence (`deduction_components` rows
with `scope_kind == "ACCOUNT"`) down to individual YouTube channels, using **only the
operator-verified channel↔account map**, by **source-aligned gross-revenue-proportional**
share — and surface the result through a read-only month endpoint. Any account that cannot
be allocated against a complete, source-aligned basis stays **UNALLOCATED with a blocking
issue**. No money is invented, no existing number changes.

**Architecture:** A pure compute service (`finance/allocation.py`, no DB I/O — mirrors
`finance/net_revenue.py` and `finance/recalculation.py`) takes the month's ACCOUNT
components, the verified `{account → channels}` map, and a per-`(channel, source_kind)`
raw gross basis, and returns per-channel allocation lines + unallocated blocking issues +
a conserved summary. A thin route (`api/allocation.py`, mounted in `app.py`) gathers those
inputs from existing repositories, calls the service, and serializes the result.
PostgreSQL is the source of truth; this slice is read-only with **no graph projection
impact** and **no change to `net_revenue`**.

---

## 1. Context and problem

`deduction_components` (shipped PR-A) records financial deductions at three grains via
`scope_kind ∈ {CHANNEL, ACCOUNT, PAYMENT}` (`db/finance_models.py:603`, CHECK
`ck_deduction_components_scope_kind`):

- **CHANNEL-grain** (`scope_id = youtube_channel_id`) — already consumed by net-revenue
  (PR-B) on the missing-net path (`finance/net_revenue.py:169-187`).
- **ACCOUNT-grain** (`scope_id = source_account_id`, the AdSense publisher account) — the
  subject of this spec. Not tied to any single channel.
- **PAYMENT-grain** (`scope_id = bank_reference`) — a bank settlement reference, not an
  account. **Out of scope** (no payment→account hop exists yet).

ACCOUNT-grain evidence is real money (account-level tax, deductions, AdSense
earnings→payment gap) that belongs to channels but is reported only at the account level.
Spec 2a built the verified map and its read contract:

```python
# finance/channel_account_links.py:675
def list_verified_adsense_account_channels(
    self, *, tenant_id: UUID | str, month: str, adsense_account_id: str
) -> list[str]:  # youtube_channel_ids; [] when unmapped/unverified for the month
```

This spec is the consumer that turns "account X owes deduction D in month M" plus
"account X maps to channels [A, B, C] in month M" into per-channel allocated amounts —
**only when the map is verified and a trustworthy basis exists.**

### Why "source-aligned raw gross" and not adjusted gross

ACCOUNT-scoped AdSense evidence is **external source/account evidence**. The account-level
amount was assessed by Google/the bank against the channels' **source-reported** earnings,
not against UMS's internally override-adjusted figures. Weighting the split by
manual-override-adjusted gross would make the allocation shift the moment an operator
approves an override — the wrong dependency for source-scoped evidence. The basis is
therefore **raw `RevenueFactEntry.gross_revenue_usd`**, further restricted to the **source
kind that matches the component's `source_system`** (the same anti-cross-source principle
net-revenue already applies at `net_revenue.py:184-185`).

---

## 2. Scope

In scope for this PR (Spec 2b PR-1):

1. **`finance/allocation.py`** — a pure, DB-free compute service:
   `gross_revenue_proportional` distribution of ACCOUNT-grain components across their
   verified channels, source-aligned raw-gross basis, deterministic rounding with exact
   per-component amount conservation, fail-closed on missing/incomplete basis, and an
   explicit `net_applicable` flag per line.
2. **`deduction_ingestion.py` repository** — add an ACCOUNT-only read method
   `list_account_components(month, adsense_account_id=None)` on
   `SqlAlchemyDeductionComponentRepository` that filters `scope_kind == "ACCOUNT"` **in SQL**
   (mirrors `list_month_components`), so PAYMENT/CHANNEL/bank-grain rows are never fetched.
   Own repository tests.
3. **`api/allocation.py`** — a new thin router, mounted in `app.py`, exposing
   `GET /revenue/months/{month}/account-allocations` (optional `adsense_account_id`
   filter). Gathers inputs via existing repositories (incl. the ACCOUNT-only query above),
   calls the service, returns allocations + unallocated issues + summary. Records
   `REVENUE_VIEWED` + `PAYMENT_VIEWED`.
4. **Docs** — `Docs/01_IMPLEMENTATION_PLAN.md` and `Docs/15_DELIVERY_BACKLOG.md` status
   updates marking Spec 2b PR-1 shipped and the remainder still pending.

Allocation method for this PR is **`gross_revenue_proportional` only** (one of the five
`recalculation.ALLOCATION_METHODS`; the rest stay dry-run-preview-only).

---

## 3. Non-goals (explicit)

- **No persistence.** No `allocation_results`/`allocation_run` table, no ORM, no
  migration. The endpoint computes on read.
- **No committed/`recalculate` writes.** `recalculation.py`'s rejected commit path
  (`recalculation.py:104-107`) is untouched.
- **No `net_revenue` change.** ACCOUNT-grain stays out of the net figure in this PR
  (net-revenue both pre-filters non-CHANNEL at the API path and drops it in the builder).
  A later PR consumes the `net_applicable=true` lines; this code is not re-touched for it.
- **No PAYMENT-grain / bank allocation.** The endpoint's input domain is ACCOUNT-grain
  only; it enumerates no PAYMENT/bank-grain rows (hence no `BANK_RECONCILIATION_VIEWED`).
  The pure service still classifies any non-ACCOUNT component it is handed as
  `UNSUPPORTED_SCOPE` (a tested defensive guard) so the engine never silently drops
  evidence. PAYMENT-grain stays visible via the existing deduction-components read.
- **No other allocation methods** (`post_tax_revenue_proportional`, `company_level`,
  `manual`, `no_allocation`).
- **No map mutation** and **none of the deferred Spec 2a follow-ups** (PR #57 N2 supersede,
  N8/V8d derived-link deactivate/reactivate, N9 close-race, N10 authz-perf). They remain
  separately sequenced.
- **No new permission or audit-event type.** Reuses `VIEW_REVENUE`,
  `VIEW_FINALIZED_PAYMENTS`, `REVENUE_VIEWED`, `PAYMENT_VIEWED`.

---

## 4. Allocation contract (the pure service)

`finance/allocation.py` performs **no database access**. It is given fully-resolved inputs
and returns a frozen result. This keeps the allocation math exhaustively unit-testable
without a DB (same pattern as `net_revenue`/`recalculation`).

### 4.1 Inputs

```python
def build_account_allocation(
    *,
    month: str,                                   # validated YYYY-MM by the caller
    components: Iterable[DeductionComponent],     # reuses the existing domain type
    verified_channels: Mapping[str, Sequence[str]],   # adsense_account_id -> [youtube_channel_id]
    gross_basis: Mapping[tuple[str, str], Decimal],   # (youtube_channel_id, source_kind) -> raw gross_usd
) -> AccountAllocationResult:
```

- `components` is the full month set; the service filters to `scope_kind == "ACCOUNT"`
  itself and routes everything else to `UNSUPPORTED_SCOPE` (so a mis-passed CHANNEL/PAYMENT
  row is visible, not silently ignored). `DeductionComponent` is the existing type consumed
  by `net_revenue` (fields used here: `month`, `scope_kind`, `scope_id`, `component_kind`,
  `source_system`, `amount_usd`, `component_key`).
- `verified_channels[account]` is precomputed by the route via
  `list_verified_adsense_account_channels(tenant_id, month, account)` for each distinct
  ACCOUNT `scope_id`. The service treats a missing/empty entry as unmapped.
- `gross_basis` is precomputed by the route from `RevenueFactEntry` for the month,
  aggregating `gross_revenue_usd` by `(youtube_channel_id, source_kind)`. **A
  `(channel, source_kind)` absent from this mapping means "no source-aligned gross fact"
  (missing) — distinct from a present value of `0`.**

### 4.2 `gross_revenue_proportional` with source-aligned basis

A zero-amount component (`A == 0`) short-circuits to **allocated with no lines** (nothing to
distribute; mapping/basis checks skipped). Otherwise, for each ACCOUNT component `C` with
`amount_usd = A`, `account = C.scope_id`:

1. `channels = verified_channels.get(account, [])`. If empty →
   `UNALLOCATED(ACCOUNT_UNMAPPED_OR_UNVERIFIED)`.
2. `source_kind = _basis_source_kind(C.source_system)` (see §5). If unresolved →
   `UNALLOCATED(BASIS_MISSING)`.
3. Look up `basis[ch] = gross_basis[(ch, source_kind)]` for each `ch in channels`:
   - If **none** of the channels has a basis entry → `UNALLOCATED(BASIS_MISSING)`.
   - If **some but not all** channels have a basis entry → `UNALLOCATED(BASIS_INCOMPLETE)`
     (fail closed: never allocate a known-incomplete basis, which would over-concentrate
     the deduction on the channels that happen to have gross).
   - A **present** entry of `0` is a valid basis value (the channel earned zero via this
     source kind; it contributes zero weight and receives zero). Only an **absent** entry is
     missing/incomplete. Distinguishing absent from present-zero relies on facts carrying
     zero-gross rows; the plan verifies this against `RevenueFactEntry`.
4. `basis_total = Σ basis[ch]`. If `basis_total == 0` (all present but zero) →
   `UNALLOCATED(ZERO_GROSS_BASIS)`.
5. Otherwise allocate: `raw_share[ch] = A × basis[ch] / basis_total`, with deterministic
   largest-remainder rounding (§4.3). One `AllocationLine` per `(component, channel)`.

This intentionally produces blocking issues whenever the map or the source-aligned gross is
incomplete — consistent with the UNALLOCATED-first philosophy. Operators see exactly which
accounts/components are not yet allocatable and why, rather than a quietly-wrong number.

### 4.3 Rounding and exact conservation

`amount_usd` is `Numeric(20, 6)`. Each line is rounded to **6 decimal places**. To
guarantee the per-component invariant `Σ allocated_amount_usd == A` **exactly** (no
rounding drift), use **largest-remainder (Hamilton) apportionment**:

1. `floor_share[ch] = floor(A × basis[ch] / basis_total, 6dp)`.
2. `residual = A − Σ floor_share[ch]` (a nonnegative multiple of `1e-6`).
3. Distribute the `residual` one `1e-6` unit at a time to channels ordered by descending
   fractional remainder, then ascending `youtube_channel_id` as a deterministic tiebreak.

This is fully deterministic (no float dependence; `Decimal` throughout) and conserves the
amount to the last micro-unit. Per the §4.2 short-circuit, a zero-amount component (`A == 0`)
produces **no** lines, counts as allocated, and contributes `0` — conserving trivially.

### 4.4 `net_applicable` classification

Each `AllocationLine` carries `net_applicable: bool`, computed as
`C.component_kind in NET_APPLICABLE_COMPONENT_KINDS`, **importing the existing frozenset
from `finance/net_revenue.py`** (`{"TAX", "DEDUCTION"}`) — single source of truth, zero
drift. Thus:

- `TAX`, `DEDUCTION` → `net_applicable = True` (the lines a future net-integration PR will
  consume).
- `UNRESOLVED_PAYMENT_GAP`, `TRANSFER_FEE`, `FX_VARIANCE` → `net_applicable = False`
  (allocated and visible as **reconciliation evidence**, never net-reducing).

---

## 5. Source-aligned gross basis rules

`_basis_source_kind(source_system)` resolves the basis source kind, reusing
`SOURCE_SYSTEM_TO_SOURCE_KIND` from `net_revenue.py` plus the payment-gap special case:

| component `source_system` | basis source kind | note |
|---|---|---|
| `adsense_management`  | `ADSENSE`            | from `SOURCE_SYSTEM_TO_SOURCE_KIND` |
| `youtube_reporting`   | `YOUTUBE_CMS`        | from `SOURCE_SYSTEM_TO_SOURCE_KIND` |
| `youtube_analytics`   | `YOUTUBE_ANALYTICS`  | from `SOURCE_SYSTEM_TO_SOURCE_KIND` |
| `adsense_payment_gap` | `ADSENSE`            | special case (gap source has no entry in the map) |
| anything else         | unresolved → `None`  | → `UNALLOCATED(BASIS_MISSING)` |

Rules:

- **Never** fall back to adjusted gross, manual overrides, a different source kind, or a
  "primary" gross. If the required source-aligned basis is missing or incomplete for the
  verified channels, fail closed into UNALLOCATED.
- The basis is **raw** `gross_revenue_usd` (pre-override), aggregated per
  `(channel, source_kind)`.

---

## 6. Blocking-issue taxonomy and fail-closed semantics

Every ACCOUNT component is either fully allocated (≥1 line, conserved) **or** recorded as a
single `UnallocatedIssue` carrying its full `amount_usd`. The codes:

| `issue_code` | Meaning | Blocking? |
|---|---|---|
| `ACCOUNT_UNMAPPED_OR_UNVERIFIED` | `list_verified_adsense_account_channels` returned `[]` for the account-month (no verified account↔owner link, or no active owner↔channel link). | yes |
| `BASIS_MISSING` | Source kind unresolved, or **no** verified channel has a source-aligned gross fact. | yes |
| `BASIS_INCOMPLETE` | **Some but not all** verified channels have a source-aligned gross fact. | yes |
| `ZERO_GROSS_BASIS` | All verified channels have gross present, but `Σ == 0`. | yes |
| `UNSUPPORTED_SCOPE` | A PAYMENT-grain or non-ACCOUNT component reached the allocator. | yes |
| `CHANNEL_IN_MULTIPLE_ACCOUNTS` | A channel is reachable from ≥2 accounts in the month. | **no — informational only** |

`CHANNEL_IN_MULTIPLE_ACCOUNTS` is emitted once per offending channel as an informational
note (Spec 2a's overlap invariant is per `(tenant, account)`, so two accounts mapping to
the same owner→channel is possible; that channel's full source gross would then weight both
account bases). Allocation still proceeds for each account independently; the note exists so
the over-weighting is visible to operators, not silently applied.

`UNSUPPORTED_SCOPE` is a **service-layer defensive guard**, not a normal API output: the
pure service classifies any non-ACCOUNT component it is handed (so the engine never silently
drops evidence), and this is exercised by service tests. Via the API it does not fire,
because the route's input domain is ACCOUNT-grain only (§8) — PAYMENT-grain evidence stays
visible, with its own bank audit, through the existing
`GET /revenue/months/{month}/deduction-components` endpoint.

---

## 7. Result shape (service output)

```python
@dataclass(frozen=True)
class AllocationLine:
    adsense_account_id: str
    youtube_channel_id: str
    component_kind: str
    source_system: str
    component_key: str          # provenance: ties the line back to its source component
    basis_source_kind: str
    basis_gross_usd: Decimal    # this channel's source-aligned raw gross
    basis_share: Decimal        # basis_gross_usd / basis_total, 6dp (display/audit aid)
    allocated_amount_usd: Decimal
    net_applicable: bool

@dataclass(frozen=True)
class UnallocatedIssue:
    scope_id: str               # AdSense account for ACCOUNT rows; offending scope_id for the UNSUPPORTED_SCOPE guard
    component_kind: str
    component_key: str
    amount_usd: Decimal
    issue_code: str
    detail: str                 # human-safe; no secrets/raw payload

@dataclass(frozen=True)
class AllocationNote:           # informational, non-blocking
    note_code: str              # e.g. CHANNEL_IN_MULTIPLE_ACCOUNTS
    youtube_channel_id: str
    detail: str

@dataclass(frozen=True)
class AllocationSummary:
    component_count: int                   # all input components
    allocated_component_count: int         # incl. zero-amount short-circuit (no lines)
    unallocated_component_count: int       # incl. UNSUPPORTED_SCOPE guard cases
    allocated_total_usd: Decimal
    unallocated_total_usd: Decimal         # Σ amount of every unallocated component
    net_applicable_total_usd: Decimal      # Σ allocated where net_applicable
    reconciliation_total_usd: Decimal      # Σ allocated where not net_applicable

@dataclass(frozen=True)
class AccountAllocationResult:
    month: str
    allocation_method: str       # "gross_revenue_proportional"
    lines: tuple[AllocationLine, ...]
    unallocated: tuple[UnallocatedIssue, ...]
    notes: tuple[AllocationNote, ...]
    summary: AllocationSummary
```

**Conservation invariants (tested):**

- Per allocated component: `Σ line.allocated_amount_usd (over its channels) ==
  component.amount_usd`.
- Aggregate: `summary.allocated_total_usd + summary.unallocated_total_usd ==
  Σ amount_usd over all INPUT components`. Every input component is either allocated or
  recorded as exactly one `UnallocatedIssue` (including the `UNSUPPORTED_SCOPE` guard case,
  which counts in `unallocated_component_count` / `unallocated_total_usd`). Via the API the
  input is ACCOUNT-only, so this equals the sum over the month's ACCOUNT components.
- `summary.net_applicable_total_usd + summary.reconciliation_total_usd ==
  summary.allocated_total_usd`.

---

## 8. API endpoint

`api/allocation.py` defines `router = APIRouter(prefix="/revenue")`, mounted in `app.py`
alongside the other revenue routers (keeps `revenue.py` from growing, as Spec 2a did with
its own module).

```
GET /revenue/months/{month}/account-allocations
    ?adsense_account_id=<optional exact-match filter>
```

- **Path/validation:** `month` validated to `YYYY-MM` (calendar month 01–12) → `422` on
  malformed input, mirroring the existing month endpoints' boundary check.
- **Input gathering (route, thin):**
  1. `deduction_repository.list_account_components(month, adsense_account_id=...)` — an
     ACCOUNT-only query (`WHERE scope_kind == "ACCOUNT"` in SQL, mirroring
     `list_month_components`). PAYMENT/CHANNEL/bank-grain rows are never fetched, so the
     endpoint enumerates no bank-grain data (this is why no `BANK_RECONCILIATION_VIEWED`).
     The ACCOUNT-only guarantee lives at the query layer, not a route-side filter.
  2. For each distinct ACCOUNT `scope_id`, call
     `list_verified_adsense_account_channels(tenant_id, month, account)` → `verified_channels`.
  3. `revenue_repository.list_month_facts(month=month)` → aggregate
     `{(youtube_channel_id, source_kind): Σ gross_revenue_usd}` → `gross_basis`.
  4. `build_account_allocation(...)` → result → serialize.
- **Response (Pydantic):** `month`, `allocation_method`, `allocations[]` (the lines),
  `unallocated[]`, `notes[]`, `summary{}`. No `raw_payload`, no secrets.
- **Errors:** typed service/validation errors → `HTTPException` with safe messages at the
  boundary; no bare `except`.

The endpoint works for **both OPEN and LOCKED** months (it is a read; locked-month gating
applies only to writes, which this PR has none of).

---

## 9. Authorization and audit

**Permission gate** (modeled on the payment-match month read, `revenue.py:748-751`; a
month-path read that mixes revenue + finalized-payment context):

```python
_require_permission(user, Permission.VIEW_REVENUE, AccessScope.global_scope())
_require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(month))
```

`VIEW_FINALIZED_PAYMENTS` is scoped to **`finance_month(month)`**, **not** global — this is a
month-path endpoint, and `ROLE_PERMISSION_MODEL.md` plus existing month endpoints
(`revenue.py:751,836`, `adsense.py:297`) scope month-specific payment APIs to the requested
finance month. (Spec 2a's map-list is global only because it is an explicit cross-month
management view — `channel_account_links.py:149-151`.) Fail-closed throughout.

**Audit** (modeled on `channel_account_links.py:169-181`; verified that sensitive finance
reads DO audit):

- Record **`REVENUE_VIEWED`** and **`PAYMENT_VIEWED`** for a successfully-authorized read
  (account-derived revenue + payment context exposure). Audit must succeed for the read.
- **Omit `BANK_RECONCILIATION_VIEWED`** — this slice exposes no PAYMENT/bank-grain rows
  (matches the conditional pattern at `revenue.py:1016`).
- No new audit-event type; no `CHANGE_ALLOCATION_RULE` (that gates allocation *writes*,
  which are out of scope).
- `details` carry only safe scope info (month, optional account filter, counts) — no raw
  payloads, no secrets.

---

## 10. Blast radius (database / graph / finance)

- **Tables/ORM affected:** none written. Reads only: `deduction_components`,
  `adsense_content_owner_links` + `content_owner_channel_links` (via the Spec 2a read
  contract), and the monthly revenue facts. No schema change, **no Alembic migration**, no
  enum/CHECK change.
- **Is PostgreSQL still the source of truth?** Yes — unchanged.
- **Could existing migrations/tests/seeds/docs break?** No — additive new module + new
  route; no shared mutable state touched.
- **Neo4j / graph projection:** **No graph projection impact detected.** This is read-only
  compute over relational data; it neither reads nor writes the graph and cannot mutate any
  source-of-truth row.
- **Authorization/audit more permissive?** No — adds a fail-closed gate and audit on a new
  read; weakens nothing.
- **Finance results / month locks / overrides / payment matching changed?** No.
  `net_revenue`, payment-match, overrides, and month-close are untouched; ACCOUNT-grain
  remains excluded from net, so **no double-count risk**. This introduces a *new derived
  view* only.
- **Migration/rollback/reset note:** none required (no DB writes).

Statement: **No graph projection impact detected.** (Backed by: no projection code is
imported or invoked; the module is pure compute + a read route.)

---

## 11. Testing

Per `CLAUDE.md` finance-change requirements (source, formula, confidence, locks, overrides,
duplicates, missing data, rounding, export/API shape). **No Postgres tier needed** (no
migration, no advisory lock) — SQLite suffices for API tests; the service tests need no DB.

**Pure service (`tests/finance/test_allocation.py`):**

- Two-channel proportional split by source-aligned gross; exact per-component conservation.
- Largest-remainder residual determinism (e.g. `A=1.000000` over 3 equal-gross channels →
  `0.333334/0.333333/0.333333`, residual to deterministic tiebreak; sum exact).
- Single channel → 100% of `A`.
- Many channels → micro-unit penny distribution, sum exact.
- `ACCOUNT_UNMAPPED_OR_UNVERIFIED` when verified set empty.
- `BASIS_MISSING` when no channel has source-aligned gross **and** when `source_system` is
  unresolvable.
- `BASIS_INCOMPLETE` when some-but-not-all channels have gross (fail closed; nothing
  allocated for that component).
- `ZERO_GROSS_BASIS` when all present but `Σ == 0`.
- Source alignment: an `adsense_management` component ignores `YOUTUBE_CMS` gross of the
  same channels; `adsense_payment_gap` uses `ADSENSE` gross.
- `net_applicable` true for TAX/DEDUCTION, false for UNRESOLVED_PAYMENT_GAP/TRANSFER_FEE/
  FX_VARIANCE; `net_applicable_total + reconciliation_total == allocated_total`.
- `UNSUPPORTED_SCOPE` for a passed-in PAYMENT/CHANNEL component.
- `CHANNEL_IN_MULTIPLE_ACCOUNTS` informational note emitted; allocation still proceeds.
- Multi-account, multi-component aggregate conservation.
- Zero-amount component → zero lines, conserved.

**Repository (`list_account_components`, in the deduction-ingestion repository tests):**

- Returns only `scope_kind == "ACCOUNT"` rows; CHANNEL/PAYMENT rows for the same month are
  excluded (proves the SQL-layer guarantee — no bank-grain rows fetched).
- Respects `month`, tenant scope, and the optional `adsense_account_id` (scope_id) filter.
- Malformed month → `DeductionComponentValidationError`.

**API (`tests/api/test_allocation_api.py`):**

- `finance_viewer` (VIEW_REVENUE + VIEW_FINALIZED_PAYMENTS) → `200` with expected shape.
- Missing `VIEW_REVENUE` → `403`; missing `VIEW_FINALIZED_PAYMENTS` at the month scope →
  `403` (fail-closed; both gates enforced).
- Malformed `month` → `422`.
- Tenant isolation (no cross-tenant components/map/facts leak).
- `adsense_account_id` filter narrows to one account.
- `REVENUE_VIEWED` + `PAYMENT_VIEWED` recorded; `BANK_RECONCILIATION_VIEWED` **not**
  recorded.
- A PAYMENT-grain component present in the month does **not** appear in the response
  (ACCOUNT-only input domain) and triggers no bank audit.
- Response carries no `raw_payload`/secret fields.
- Unallocated list + summary totals present and conserved end-to-end.

**Baseline gate:** `python -m ruff check backend tests scripts`, `pytest -q`,
`git diff --check`.

---

## 12. Affected files (principal)

- **Create** `backend/ums_smart_revenue/finance/allocation.py` — pure service + dataclasses
  + typed errors (`AllocationError`, `AllocationValidationError`).
- **Modify** `backend/ums_smart_revenue/finance/deduction_ingestion.py` — add
  `list_account_components` (ACCOUNT-only SQL query) on
  `SqlAlchemyDeductionComponentRepository`.
- **Create** `backend/ums_smart_revenue/api/allocation.py` — router, request/response
  Pydantic models, auth + audit wiring (modeled on `api/channel_account_links.py` and
  `api/revenue.py`), input gathering.
- **Modify** `backend/ums_smart_revenue/app.py` — mount the new router.
- **Create** `tests/finance/test_allocation.py`, `tests/api/test_allocation_api.py`;
  **extend** the deduction-ingestion repository tests for `list_account_components`.
- **Modify** `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md` — status.

Imports reused (no duplication): `DeductionComponent`, `NET_APPLICABLE_COMPONENT_KINDS`,
`SOURCE_SYSTEM_TO_SOURCE_KIND` (from `net_revenue.py`); `RevenueFactEntry` + the revenue
repository; `list_verified_adsense_account_channels` (Spec 2a repo); `AccessScope`,
`Permission`, `_require_permission`; `AuditEventType`, `record_audit_event` + audit-sink
wiring.

---

## 13. Decisions log (resolved during brainstorming)

1. **Slice = compute + read API only.** No persistence, no committed writes, no
   net-revenue integration in this PR. Smallest reviewable slice that proves the allocation
   math + UNALLOCATED semantics first; net integration is a later PR.
2. **Basis = source-aligned raw gross, fail-closed.** Weight by raw
   `gross_revenue_usd` of the source kind matching the component's `source_system`; never
   adjusted gross / overrides / cross-source fallback; missing-or-incomplete basis →
   UNALLOCATED.
3. **Allocate all ACCOUNT kinds, classify with `net_applicable`.** UNRESOLVED_PAYMENT_GAP /
   TRANSFER_FEE / FX_VARIANCE are allocated as reconciliation evidence in a non-net bucket;
   only TAX/DEDUCTION are net-applicable (from the existing constant).
4. **PAYMENT-grain deferred.** No payment→account hop yet → `UNSUPPORTED_SCOPE`.
5. **Audit on read.** Record `REVENUE_VIEWED` + `PAYMENT_VIEWED` (sensitive finance reads
   audit here); no bank audit for ACCOUNT-only. (Corrected from an initial wrong
   "read-only ⇒ no audit" assumption, verified against the repo.)
6. **Auth is month-scoped for payments.** `VIEW_REVENUE@global` +
   `VIEW_FINALIZED_PAYMENTS@finance_month(month)`, matching month-path payment APIs (not
   Spec 2a's cross-month global list).
7. **`CHANNEL_IN_MULTIPLE_ACCOUNTS` is informational**, not an UNALLOCATED failure.
8. **ACCOUNT-only at the query layer.** A dedicated `list_account_components` SQL query
   (not a route-side filter over the full component set) guarantees no PAYMENT/bank-grain
   rows are ever fetched — defense-in-depth behind the no-bank-audit decision.

---

## 14. Decomposition note (what later Spec 2b PRs add)

This PR is the allocation **compute + read** foundation. Sequenced afterward, each its own
spec/plan/PR:

- **Net integration** — consume `net_applicable=true` allocation lines into
  `net_revenue` (account-derived channel net), with its own anti-double-count proof.
- **PAYMENT-grain allocation** — once a verified payment→account hop exists.
- **Persistence + committed allocation** — an `allocation_results` substrate + audited,
  locked-month-gated committed writes (turning `recalculation.py`'s rejected commit into a
  real path), plus the remaining allocation methods.
- **Map-hardening follow-ups** (PR #57 N2/N8/N9/N10/V8d) as their coverage becomes relevant
  to allocation correctness.
