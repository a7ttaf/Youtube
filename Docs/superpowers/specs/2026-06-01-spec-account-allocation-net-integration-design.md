# Account-Allocation Net-Revenue Integration — Design Spec

**Phase:** Phase 4 reconciliation — **Spec 2b, PR-2** (allocation engine, second slice).
Consumes the account-level allocation built in **Spec 2b PR-1**
(`2026-05-31-spec-account-allocation-design.md`, merged PR #58) and folds its
net-applicable lines into the month/channel **net-revenue** figure, on both the API and
the finance-export surfaces.

**Status:** Designed 2026-06-01. Off `main` (`8a6df2a`, which has the MERGED Phase 4 PR-A
deduction substrate/ingestion, PR-B channel-direct net consumption, Spec 2a channel↔account
map, and Spec 2b PR-1 account-allocation compute + read endpoint). Branch
`spec/account-allocation-net-integration`.

**Goal:** Make per-channel net revenue reflect **account-allocated** TAX/DEDUCTION evidence
— not just channel-direct components — so the Phase 4 "fully reconciled net" acceptance gate
advances. Allocation is **read/compute only**: no persistence, no committed recalculation
writes, no migration. The same numbers must appear in the net-revenue **API** and in finance
**exports** (no drift).

**Architecture:** A new service orchestrator `finance/allocation_inputs.py`
(`compute_month_account_allocation(...)`) gathers PR-1's allocation inputs from existing
repositories and returns PR-1's `AccountAllocationResult` — extracted from the duplicated
block in `api/allocation.py` so the allocation endpoint, the net-revenue route, and the
exports route all share one path (DRY). The **pure** builders in `finance/net_revenue.py`
gain one new pure input (`account_allocations: Iterable[AllocationLine] = ()`) and apply
net-applicable allocated lines **only on the missing-net (COMPONENT_DERIVED) path**.
`net_revenue` imports no repository/DB. PostgreSQL is the source of truth; **no graph
projection impact** (Neo4j is retired).

---

## 1. Context and problem

Net revenue today (`finance/net_revenue.py`, shipped PR-B) derives a channel's net two ways:

- **Source-net path** (`_calculated_channel_summary`, `net_revenue.py:263-295`): the primary
  source already reports `net_revenue_usd`. `deduction_amount_usd = adjusted_gross −
  adjusted_net` — a **source-derived** figure, independent of `deduction_components`.
  Status `CALCULATED` / `PENDING_OVERRIDE_REVIEW`, confidence `B_RECONCILED` / `D_ESTIMATED`.
- **Missing-net path** (`_component_derived_channel_summary`, `net_revenue.py:189-221`): the
  primary source has no net. Net is derived as `adjusted_gross − component_total`, where
  `component_total` sums **CHANNEL-grain** net-applicable components for that channel/month
  that are **source-aligned** to the primary source kind (`_applicable_deduction_components`,
  `net_revenue.py:169-186`). Status `COMPONENT_DERIVED`, confidence `D_ESTIMATED`.
- **No-net, no-components** → `NET_REVENUE_SOURCE_MISSING` (`_missing_net_source_summary`,
  `net_revenue.py:224-260`), `deduction_amount_usd = None`, confidence `E_MISSING`.

The gap: **ACCOUNT-grain** deduction evidence (`scope_kind == "ACCOUNT"`) — account-level
tax/deductions Google assessed against the whole publisher account — never reaches net. PR-1
built the allocation that splits each ACCOUNT component across its account's
operator-verified channels (`build_account_allocation`, source-aligned raw-gross-proportional,
fail-closed UNALLOCATED), but exposed it only through a standalone read endpoint. This spec
consumes PR-1's net-applicable allocated lines in the actual net figure.

### 1.1 Anti-double-count foundation (proven, not assumed)

A single Google source row becomes **either** a CHANNEL-grain **or** an ACCOUNT-grain
component, never both — `deduction_components.py:108-111`:

```python
if row.youtube_channel_id:
    scope_kind, scope_id = "CHANNEL", row.youtube_channel_id
else:
    scope_kind, scope_id = "ACCOUNT", row.source_account_id
```

So channel-direct deductions (consumed by PR-B) and account-allocated deductions (consumed
here) are **disjoint money by construction** — they are additive, and any dedup is a safety
net, not a load-bearing correction. This is why this spec **never modifies the source-net
path** and never re-touches channel-direct components: it only *adds* account-allocated
evidence to the same missing-net path, exactly once.

---

## 2. Scope

In scope for this PR (Spec 2b PR-2):

0. **`finance/deduction_policy.py`** (new, preliminary) — relocate the two shared net-policy
   constants `NET_APPLICABLE_COMPONENT_KINDS` and the **net_revenue**
   `SOURCE_SYSTEM_TO_SOURCE_KIND` (the `str → str` map at `net_revenue.py:21,28`) into a
   neutral leaf module, to break the import cycle this PR would otherwise create (§4.6).
   `net_revenue.py`, `allocation.py`, and `api/revenue.py` re-import them from there. **Do not
   touch** the identically-named but different `SOURCE_SYSTEM_TO_SOURCE_KIND` in
   `google_source_normalizer.py:74` (a `str → RevenueFactSourceKind` map with its own tests).
1. **`finance/allocation_inputs.py`** (new) — `compute_month_account_allocation(*, month,
   deduction_repository, revenue_repository, link_repository) -> AccountAllocationResult`,
   extracted verbatim-in-behavior from `api/allocation.py:167-191`. The PR-1 endpoint is
   refactored to call it (no behavior change; its tests stay green).
2. **`finance/net_revenue.py`** — add a pure `account_allocations: Iterable[AllocationLine]
   = ()` parameter to `build_month_net_revenue_summary` and
   `build_channel_net_revenue_summary`; apply net-applicable allocated lines on the
   missing-net path; add the per-channel breakdown fields and the month-level unallocated
   surface (§4). No DB/repository import added to this module.
3. **`api/revenue.py`** — the `GET /revenue/months/{month}/net-revenue` route gathers
   allocations via the new service, passes them to the builder, adds a
   `VIEW_FINALIZED_PAYMENTS` gate on the route's **`target_scope`** (the org scope, co-scoped
   with the existing checks — see §5.1) + a `PAYMENT_VIEWED` audit, and applies the
   scoped-visibility pin (§5).
4. **`api/exports.py`** — the finance-export source-summary path
   (`_build_finance_source_summaries_for_export`, `exports.py:1035`) currently calls the
   builder with **only `facts` + `manual_overrides`** — it passes **no** `deduction_components`
   today, so it *already* drifts from the API on channel-direct (PR-B) components. To truly
   kill drift, this PR makes exports fetch + pass **both** the same scoped channel-direct
   net-applicable `deduction_components` as the API **and** the account allocations (§5.4).
   Applies the same scoped-visibility pin; `PAYMENT_VIEWED` is recorded for **all**
   finance-artifact exports, not only global ones (§5.3). Export **layouts** are unchanged this
   PR (totals become correct; no new columns).
5. **Docs** — `Docs/01_IMPLEMENTATION_PLAN.md` + `Docs/15_DELIVERY_BACKLOG.md` status.

Allocation method remains `gross_revenue_proportional` (PR-1's only method).

---

## 3. Non-goals (explicit)

- **No source-net change.** CALCULATED / PENDING_OVERRIDE_REVIEW channels are never
  recomputed or downgraded; their `deduction_amount_usd` stays source-derived.
- **No persistence / no committed writes.** No `allocation_results` table, no migration;
  `recalculation.py`'s rejected commit path is untouched.
- **No PAYMENT-grain / reconciliation kinds in net.** Only `net_applicable` (TAX/DEDUCTION)
  allocated lines feed net; FX_VARIANCE / TRANSFER_FEE / UNRESOLVED_PAYMENT_GAP never appear
  in the net endpoint or its unallocated surface.
- **No explain-path provenance** (D4 deferred). `build_channel_month_revenue_explanation`
  supports only adjusted-gross today; net explanation is a separate follow-up.
- **No export layout/column changes.** Export net/deduction **totals** become correct;
  surfacing the breakdown columns/unallocated rows in workbook/PDF/slide templates is a later
  cosmetic PR.
- **No new permission or audit-event type.** Reuses `VIEW_REVENUE`, `VIEW_CONFIDENCE`,
  `VIEW_FINALIZED_PAYMENTS`, `REVENUE_VIEWED`, `PAYMENT_VIEWED`.
- **No map mutation** and none of the deferred Spec 2a follow-ups (PR #57 N2/N8/N9/N10/V8d).

---

## 4. The pure builder changes (`finance/net_revenue.py`)

### 4.1 New input

```python
def build_month_net_revenue_summary(
    *,
    month: str,
    facts: Iterable[RevenueFactEntry],
    manual_overrides: Iterable[RevenueManualOverrideEntry],
    deduction_components: Iterable[DeductionComponent] = (),
    account_allocations: Iterable[AllocationLine] = (),               # NEW (PR-1 dataclass)
    unallocated_account_issues: Iterable[UnallocatedIssue] | None = None,  # NEW (§4.5)
) -> MonthNetRevenueSummary: ...

def build_channel_net_revenue_summary(
    *,
    facts: Iterable[RevenueFactEntry],
    manual_overrides: Iterable[RevenueManualOverrideEntry],
    month: str | None = None,
    youtube_channel_id: str | None = None,
    deduction_components: Iterable[DeductionComponent] = (),
    account_allocations: Iterable[AllocationLine] = (),   # NEW
) -> ChannelNetRevenueSummary: ...
```

`AllocationLine` and `UnallocatedIssue` are imported from `finance.allocation` (PR-1). The
month builder groups allocations by `youtube_channel_id` (a new
`_account_allocations_by_channel`, mirroring `_deduction_components_by_channel`,
`net_revenue.py:447-457`) and passes each channel its slice. The channel builder needs only
`account_allocations` (the month-level `unallocated_account_issues` surface lives on
`MonthNetRevenueSummary`, not the channel summary). Both builders stay pure (no DB).

**Contract split (who owns what):** the **caller** (route/export) owns the *scope gate* — it
passes `unallocated_account_issues=result.unallocated` only for a global request and `None`
for any scoped request (§5.2). The **builder** owns the *finance filter* — it filters the
issues it is given to `component_kind in NET_APPLICABLE_COMPONENT_KINDS`, sums the total, and
serializes (single home for the net-applicable rule). `unallocated_account_issues=None` →
both month surface fields serialize as explicit JSON `null` (not zeroed, not omitted; §4.5,
finding #3). An empty iterable (global request, nothing unallocated) → total `Decimal("0")`,
issues `[]`.

### 4.2 Source-alignment gate (identical to channel-direct)

A new `_applicable_account_allocations` mirrors `_applicable_deduction_components` exactly:

```python
def _applicable_account_allocations(
    allocations: Iterable[AllocationLine],
    *,
    youtube_channel_id: str,
    primary_source_kind: str,
) -> list[AllocationLine]:
    return [
        line
        for line in allocations
        if line.youtube_channel_id == youtube_channel_id
        and line.net_applicable
        and SOURCE_SYSTEM_TO_SOURCE_KIND.get(line.source_system) == primary_source_kind
    ]
```

The gate is **`line.net_applicable` AND `SOURCE_SYSTEM_TO_SOURCE_KIND.get(line.source_system)
== primary.source_kind`** — byte-identical to the channel-direct rule. `line.basis_source_kind`
is **provenance only** (records the gross PR-1 weighted by) and is **deliberately not** used
as a second source-alignment contract. (The allocations passed in are already pre-filtered to
the month by the caller via the per-channel grouping, so no `month` field is required on the
line.)

### 4.3 Application — missing-net path only

In `build_channel_net_revenue_summary`, on the `primary.net_revenue_usd is None` branch
(`net_revenue.py:357`), compute both contributions and apply them together:

```python
channel_direct = _applicable_deduction_components(...)         # existing
account_allocated = _applicable_account_allocations(...)       # new
channel_direct_total = sum((c.amount_usd for c in channel_direct), Decimal("0"))
account_allocated_total = sum((l.allocated_amount_usd for l in account_allocated), Decimal("0"))
component_total = channel_direct_total + account_allocated_total
```

- If `component_total` has any contribution (either side non-empty) → `COMPONENT_DERIVED`
  via `_component_derived_channel_summary`, now also given `channel_direct_total` and
  `account_allocated_total` for the breakdown fields. `net = adjusted_gross − component_total`.
- If both empty → `NET_REVENUE_SOURCE_MISSING` (unchanged).

The **source-net path is untouched**: account allocations are never consulted when
`primary.net_revenue_usd` is present.

**Anti-double-count safety dedup (defensive, tested, should never fire):** because the two
sets are disjoint by construction (§1.1), an allocated line whose `component_key` already
appears among the applied channel-direct components is skipped (and would indicate an
ingestion-layer regression). The skip is covered by a test that asserts it does not fire on
real data and does fire if a duplicate `component_key` is injected.

### 4.4 Breakdown fields (D1 — applied-component semantics)

Add two fields to `ChannelNetRevenueSummary` (after `deduction_amount_usd`):

```python
    channel_direct_deduction_amount_usd: Decimal | None
    account_allocated_deduction_amount_usd: Decimal | None
```

These are **applied-component** fields (what was *applied* to derive net), **not** a global
re-decomposition of `deduction_amount_usd`. Semantics by status:

| status | `deduction_amount_usd` | `channel_direct_…` | `account_allocated_…` | identity |
|---|---|---|---|---|
| `CALCULATED` / `PENDING_OVERRIDE_REVIEW` (source-net) | `adjusted_gross − adjusted_net` (source-derived, **unchanged**) | `None` | `None` | **no** sum identity (deduction is source-derived, not component-applied) |
| `COMPONENT_DERIVED` (missing-net) | `channel_direct + account_allocated` | the applied channel-direct total | the applied account-allocated total | **`deduction_amount_usd == channel_direct + account_allocated`** (exact, tested) |
| `NET_REVENUE_SOURCE_MISSING` / empty | `None` | `None` | `None` | n/a |

`to_api()` (`net_revenue.py:51-80`) serializes both new fields via `decimal_to_api` (→ `null`
when `None`). Additive change; existing consumers tolerate new keys.

### 4.5 Month-level unallocated surface (D2 — net-applicable only)

`MonthNetRevenueSummary` gains:

```python
    unallocated_account_deduction_total_usd: Decimal | None
    unallocated_account_issues: list[dict[str, str]] | None
```

Populated from PR-1's `AccountAllocationResult.unallocated` (a list of `UnallocatedIssue`),
**filtered by the builder to net-applicable component kinds** (`component_kind in
NET_APPLICABLE_COMPONENT_KINDS`) — an account TAX/DEDUCTION that could not be allocated
(unmapped/unverified account, missing/incomplete/zero basis). The total is the summed
`amount_usd` of those filtered issues; each serialized issue dict carries `scope_id`
(account), `component_kind`, `issue_code`, `amount_usd`, `detail` (no secrets, no
`raw_payload`). Reconciliation-only kinds (FX/TRANSFER_FEE/UNRESOLVED_PAYMENT_GAP) are
**excluded** from this surface entirely.

Per the §4.1 contract split: the **caller** applies the scope gate by passing
`unallocated_account_issues=result.unallocated` for a global request or `None` for any scoped
request (§5.2); the **builder** applies the net-applicable filter and serializes.

**Serialization (explicit `null`, not key omission — finding #3):** to match the existing
`to_api()` style (`net_revenue.py:58-80,100-115`), which emits every key unconditionally, both
fields are **always present** in `MonthNetRevenueSummary.to_api()`:

- **Scoped request** (caller passed `None`) → `unallocated_account_deduction_total_usd: null`,
  `unallocated_account_issues: null` (explicit JSON `null`, not an absent key).
- **Global request, nothing unallocated** (caller passed the list, all filtered out or empty)
  → `unallocated_account_deduction_total_usd: "0"`, `unallocated_account_issues: []`.
- **Global request, with unallocated** → the summed total string + the issues array.

So `null` distinguishes "scope withheld this surface" from `0`/`[]` "global, nothing
unallocated." The dataclass fields are `Decimal | None` / `list[...] | None`; the route never
relies on key omission.

### 4.6 Import-cycle resolution (`finance/deduction_policy.py`)

`net_revenue.py` cannot top-level-import `AllocationLine`/`UnallocatedIssue` from
`allocation.py`, because `allocation.py:16-19` already imports `NET_APPLICABLE_COMPONENT_KINDS`
and `SOURCE_SYSTEM_TO_SOURCE_KIND` **from `net_revenue.py`** — a direct cycle.

**Resolution (Task 0, done first):** create a neutral leaf module `finance/deduction_policy.py`
holding the two shared net-policy constants (moved verbatim from `net_revenue.py:21-28`):

```python
# finance/deduction_policy.py
SOURCE_SYSTEM_TO_SOURCE_KIND: dict[str, str] = {
    "adsense_management": "ADSENSE",
    "youtube_reporting": "YOUTUBE_CMS",
    "youtube_analytics": "YOUTUBE_ANALYTICS",
}
NET_APPLICABLE_COMPONENT_KINDS: frozenset[str] = frozenset({"TAX", "DEDUCTION"})
```

`net_revenue.py`, `allocation.py`, and `api/revenue.py` import both names from
`finance.deduction_policy`. `deduction_policy` imports nothing from `net_revenue`/`allocation`,
so the cycle is gone and `net_revenue` can import `AllocationLine`/`UnallocatedIssue` from
`allocation` cleanly. **Leave `google_source_normalizer.py:74`'s identically-named
`SOURCE_SYSTEM_TO_SOURCE_KIND` (a `str → RevenueFactSourceKind` map, different type, own tests)
untouched.** `net_revenue.py` **MUST re-export** the two names (`from finance.deduction_policy
import NET_APPLICABLE_COMPONENT_KINDS, SOURCE_SYSTEM_TO_SOURCE_KIND`) so existing
`from net_revenue import …` sites keep working unchanged; new code imports from
`deduction_policy` directly. A test asserts the re-exported `net_revenue` names are the same
objects as the `deduction_policy` originals and that the values are unchanged (§9).

---

## 5. Authorization, audit, and scoped visibility

### 5.1 Net-revenue API auth + audit (D3)

`GET /revenue/months/{month}/net-revenue` (`revenue.py:1039-1118`) currently requires
`VIEW_REVENUE` + `VIEW_CONFIDENCE` on the target (org) scope and records `REVENUE_VIEWED`.
PR-2 **adds**, because net now embeds account-derived (finalized-payment) evidence:

- `_require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, target_scope, org_index)`
- a second audit event `PAYMENT_VIEWED` (entity `monthly_net_revenue_summary`, scope
  `finance_month(month)` as audit metadata), alongside the existing `REVENUE_VIEWED`.

`VIEW_REVENUE` + `VIEW_CONFIDENCE` are already checked on `target_scope` (the org scope
resolved from the `scope_type`/`scope_id` query params). The new `VIEW_FINALIZED_PAYMENTS`
gate is checked on the **same `target_scope`**, not `finance_month(month)`. Rationale (a
correction to the earlier draft, which mirrored the payment-match read): net-revenue is an
**org-scoped** endpoint, unlike the global+month payment-match view. `OrgAccessIndex.contains`
does not let a company/sector grant satisfy a `finance_month` target, so gating on
`finance_month(month)` would 403 every existing company/sector-scoped `finance_viewer` who
can read net-revenue today — an access regression on an already-shipped endpoint, beyond the
new account-derived portion. Gating on `target_scope` keeps the finalized-payment requirement
co-scoped with the revenue/confidence checks the caller already passes. (The `PAYMENT_VIEWED`
audit event still records `finance_month(month)` as its scope — that is audit-trail metadata,
not an authorization target.) Fail-closed throughout.

**Audit response-shape change (finding #4):** the net route today emits a singular
`summary_api["audit_event"]` (`revenue.py:1117`). With two events it switches to the **plural
`summary_api["audit_events"] = [revenue_record, payment_record]`** form — byte-for-byte the
payment-match precedent (`revenue.py:798-801`). This is a **deliberate response-shape change**
(`audit_event` → `audit_events`) for this endpoint; the plan updates the endpoint's existing
tests and the backlog documents it so any consumer keying on `audit_event` is on notice. (The
two other singular `audit_event` routes — `revenue.py:256,366` — are unrelated and untouched.)

### 5.2 Scoped-visibility pin (Correction 3 — global-only unallocated detail)

Allocation is computed **month-wide** (proportions require each account's full verified-channel
gross), then only **in-scope** channel lines are applied to that response's channels — so
net/deduction **totals are correct at every scope**. But the **unallocated-account surface**
(§4.5) describes *global* unmapped/unverified accounts that cannot be safely attributed to a
scoped channel subset. Therefore:

> The unallocated-account surface (`unallocated_account_deduction_total_usd` +
> `unallocated_account_issues`) is populated **only when `scope_type == "global"`**. For any
> scoped request (sector/company/channel), both fields serialize as explicit JSON **`null`**
> (caller passes `None`; §4.5 finding #3) — never zeroed, and never an absent key.

This is the **chosen** resolution (over "require a global permission for the detail"): one
rule, no auth-shape change, zero cross-scope leakage. It applies identically to the API and
exports.

### 5.3 Exports auth + audit (D5)

`api/exports.py` already gates finance-artifact exports with `VIEW_FINALIZED_PAYMENTS`
(`exports.py:1352-1358`), so no new permission is needed there. But
`_record_finance_export_artifact_audit` (`exports.py:1106-1128`) currently emits
`PAYMENT_VIEWED` **only when `scope_type == "global"`**. Since scoped finance exports will now
embed account-allocation-derived net totals, **`PAYMENT_VIEWED` must be recorded for scoped
finance-artifact exports too**:

- Move the `PAYMENT_VIEWED` record out of the `if export_job.scope_type == "global":` block
  so it is emitted for every finance-artifact export (scope `finance_month(month)`).
- **Leave `BANK_RECONCILIATION_VIEWED` global-only** — PR-2 exposes no bank/PAYMENT-grain
  rows, so actual bank exposure is unchanged (mirrors PR-1's "account evidence →
  `PAYMENT_VIEWED`, no bank audit" discipline and the deduction-components read's conditional
  bank audit).

### 5.4 Exports data inputs — close the pre-existing channel-direct gap (finding #2)

`_build_finance_source_summaries_for_export` (`exports.py:1035-1039`) currently calls
`build_month_net_revenue_summary` with **only `facts` + `manual_overrides`** — it passes **no**
`deduction_components` at all. So the export net **already** diverges from the API net (which
passes channel-direct net-applicable components, `revenue.py:1079-1083`) for any month with
channel-direct TAX/DEDUCTION on the missing-net path, *independent of* this PR. Passing only
`account_allocations` would leave that channel-direct gap open and the "no drift" claim false.

Therefore PR-2's export change fetches and passes **both** inputs, scoped to the export's
`channel_ids`:

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

A test asserts the export net for a missing-net channel with channel-direct components equals
the API net for the same month/scope (the regression this closes), and a second asserts parity
once account allocations are added. (`channel_ids is None` for a global export, matching how
the API's global path lists facts/components unscoped.)

---

## 6. Service orchestrator (DRY) — `finance/allocation_inputs.py`

Extract the input-gathering currently inline in `api/allocation.py:167-191` into one
reusable function so the allocation endpoint, the net-revenue route, and the exports route
share exactly one allocation path (no divergent re-implementations):

```python
def compute_month_account_allocation(
    *,
    month: str,
    deduction_repository: SqlAlchemyDeductionComponentRepository,
    revenue_repository: SqlAlchemyRevenueFactRepository,
    link_repository: SqlAlchemyChannelAccountLinkRepository,
    adsense_account_id: str | None = None,
) -> AccountAllocationResult:
    """Gather ACCOUNT components, source-aligned gross basis, and the verified
    channel map for a month, then run build_account_allocation. Pure orchestration
    over repositories; no auth, no audit, no writes."""
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
    return build_account_allocation(
        month=month,
        components=components,
        verified_channels=verified_channels,
        gross_basis=gross_basis,
    )
```

`api/allocation.py` is refactored to call this (its existing tests must stay green —
behavior-preserving). The net-revenue and exports routes call it with the **month only**
(no `adsense_account_id` filter — they need the full month), pass `result.lines` to the
builder as `account_allocations`, and — **only for a global-scope request** — pass
`result.unallocated` as `unallocated_account_issues` (the builder applies the net-applicable
filter; the caller passes `None` for any scoped request, per §4.1/§5.2).

This module imports repositories/types but, like other `finance/` service code, performs no
auth or audit. It does **not** live in `net_revenue.py` (which stays repository-free).

---

## 7. Result shape (API response deltas)

`ChannelNetRevenueSummary.to_api()` gains:

```json
"channel_direct_deduction_amount_usd": "12.500000" | null,
"account_allocated_deduction_amount_usd": "3.250000" | null
```

`MonthNetRevenueSummary.to_api()` gains (populated at global scope; explicit `null` on scoped
requests — §4.5/§5.2):

```json
"unallocated_account_deduction_total_usd": "40.000000" | null,
"unallocated_account_issues": [
  {"scope_id": "pub-7", "component_kind": "DEDUCTION", "issue_code": "ACCOUNT_UNMAPPED_OR_UNVERIFIED",
   "amount_usd": "40.000000", "detail": "no verified channels for account-month"}
] | null
```

All decimals via `decimal_to_api`; no secrets, no `raw_payload`. Both `unallocated_*` keys are
**always present**: explicit `null` on scoped responses, `"0"`/`[]` on a global response with
nothing unallocated, populated otherwise (§4.5, finding #3). Totals
(`total_net_revenue_usd`, `total_deduction_amount_usd`) already aggregate per-channel values
and now include account-allocated amounts on COMPONENT_DERIVED channels automatically. The
net-revenue endpoint's audit envelope changes from singular `audit_event` to plural
**`audit_events: [REVENUE_VIEWED, PAYMENT_VIEWED]`** (§5.1, finding #4).

---

## 8. Blast radius (database / graph / finance / auth)

- **Tables/ORM:** none written; no migration. Reads only: `deduction_components` (ACCOUNT
  via PR-1's `list_account_components`), the Spec 2a map, `monthly_channel_revenue_facts`.
- **PostgreSQL source of truth:** yes — unchanged.
- **Finance results:** **per-channel net changes only on the COMPONENT_DERIVED (missing-net)
  path**, by *adding* account-allocated net-applicable deductions that are disjoint from
  channel-direct ones (§1.1). Source-net (`B_RECONCILED`) channels are byte-for-byte
  unchanged. No double-count. Month totals shift only by the newly-applied account
  allocations.
- **API/export consistency:** both call the same builder with the same allocations → **no
  net drift** between the API and finance workbooks/PDF/slides.
- **Authorization:** net-revenue route becomes **more** restrictive (adds
  `VIEW_FINALIZED_PAYMENTS` on the route's `target_scope`, co-scoped with the existing
  revenue/confidence checks — §5.1); exports auth unchanged (already gated); audit becomes
  **more** complete (adds `PAYMENT_VIEWED` to net route and to scoped finance exports).
  Nothing weakened; fail-closed preserved. No access regression: roles that can read
  net-revenue today (e.g. `finance_viewer` at their org scope) already hold
  `VIEW_FINALIZED_PAYMENTS`, so the co-scoped gate does not lock them out.
- **Neo4j / graph projection:** **No graph projection impact detected** — pure relational
  compute; no projection code imported or invoked; cannot mutate any source-of-truth row.
- **Backward compatibility:** dataclasses gain optional fields (`None` defaults); the new
  builder param defaults to `()`, so any other caller (there are only two: revenue + exports)
  is unaffected if not updated. Response payloads gain keys (additive).
- **Migration/rollback/reset note:** none required (no DB writes).

Statement: **No graph projection impact detected.** (Backed by: no projection import/call;
read-only compute over relational data.)

---

## 9. Testing

Per `CLAUDE.md` finance-change requirements (source, formula, confidence, locks, overrides,
duplicates, missing data, rounding, export/API shape). **No Postgres tier required** (no
migration/lock); SQLite for the route/export tests, no DB for the pure-builder tests.

**Pure builder (`tests/finance/test_net_revenue.py`):**
- COMPONENT_DERIVED channel with account-allocated TAX/DEDUCTION → net =
  `adjusted_gross − (channel_direct + account_allocated)`; breakdown fields set; sum identity
  holds exactly.
- Source-net (`CALCULATED`) channel + a same-channel account allocation present in input →
  net and `deduction_amount_usd` **unchanged** (source-derived); breakdown fields `None`
  (account allocations never applied to source-net).
- Source alignment: an `adsense_management` allocated line does **not** reduce a
  `YOUTUBE_CMS`-primary channel's derived net; a defensive test that a **mismatched
  `basis_source_kind`** does not override the `source_system`→source-kind decision.
- Channel-direct and account-allocated share the same source-alignment rule (parity test).
- `net_applicable=false` allocated lines (reconciliation kinds) never reduce net and never
  appear in the unallocated surface.
- `component_key` safety-dedup: does not fire on disjoint real data; **does** skip when a
  duplicate `component_key` is injected across both sets.
- Month builder groups allocations per channel; aggregate `total_net_revenue_usd` /
  `total_deduction_amount_usd` include account-allocated amounts.
- Unallocated surface: net-applicable account issues populate the month total + list;
  reconciliation-kind unallocated issues are excluded.
- Empty `account_allocations` (default) → byte-identical behavior to PR-B.

**Service (`tests/finance/test_allocation_inputs.py`):**
- `compute_month_account_allocation` returns the same `AccountAllocationResult` the PR-1
  endpoint produced for equivalent fixtures (gross_basis keying, verified-channel map,
  account dedup).

**Constant move (`tests/finance/test_deduction_policy.py`):**
- `NET_APPLICABLE_COMPONENT_KINDS` and `SOURCE_SYSTEM_TO_SOURCE_KIND` in `deduction_policy`
  equal their former values; `net_revenue`'s (re-exported) names are the same objects;
  `google_source_normalizer.SOURCE_SYSTEM_TO_SOURCE_KIND` is unchanged (distinct map).
- Importing `net_revenue` and `allocation` together raises no `ImportError` (cycle gone).

**API (`tests/api/test_net_revenue_api.py` / net-revenue tests):**
- `finance_viewer` (VIEW_REVENUE + VIEW_CONFIDENCE + VIEW_FINALIZED_PAYMENTS) at **global** →
  200; response includes breakdown fields and (global) the unallocated surface; envelope is
  **`audit_events`** containing `REVENUE_VIEWED` + `PAYMENT_VIEWED`.
- Missing `VIEW_FINALIZED_PAYMENTS` (a custom principal holding VIEW_REVENUE + VIEW_CONFIDENCE
  but not VIEW_FINALIZED_PAYMENTS, injected via `dependency_overrides`) → **403** (fail-closed;
  new gate enforced) with detail `"Missing permission: finance.view_finalized_payments"`.
- Scoped request (company) with `finance_viewer` (which holds VIEW_FINALIZED_PAYMENTS at its
  org scope) → 200; net/deduction totals correct **but the unallocated-account surface is
  explicit `null`** (both fields), proving the §5.2 pin and the finding-#3 serialization rule.
  (This 200 is also the regression check that the `target_scope` gate does not lock out
  org-scoped finance viewers — see §5.1.)
- Existing net-revenue tests updated for: the new auth requirement, the `audit_event` →
  `audit_events` envelope change, and the additive keys.

**Exports (`tests/api/test_exports_api.py`):**
- **Channel-direct drift regression:** export net for a missing-net channel **with
  channel-direct TAX/DEDUCTION** equals the API net for the same month/scope (fails on today's
  code, which passes no `deduction_components` to the export builder).
- Account-allocation parity: export net equals API net once account allocations are added.
- `PAYMENT_VIEWED` is now recorded for a **scoped** finance-artifact export (previously only
  global); `BANK_RECONCILIATION_VIEWED` remains global-only.
- Global vs scoped export: unallocated surface present only at global scope (explicit `null`
  when scoped).

**Allocation endpoint regression (`tests/api/test_allocation_api.py`):** unchanged behavior
after the orchestrator extraction (all PR-1 tests stay green).

**Baseline gate:** `python -m ruff check backend tests scripts`, `pytest -q`,
`git diff --check`.

---

## 10. Affected files (principal)

- **Create** `backend/ums_smart_revenue/finance/deduction_policy.py` — neutral home for
  `NET_APPLICABLE_COMPONENT_KINDS` + the net-policy `SOURCE_SYSTEM_TO_SOURCE_KIND` (Task 0,
  breaks the import cycle, §4.6).
- **Create** `backend/ums_smart_revenue/finance/allocation_inputs.py` —
  `compute_month_account_allocation`.
- **Modify** `backend/ums_smart_revenue/finance/net_revenue.py` — import the two constants from
  `deduction_policy` and **MUST re-export** them for back-compat (§4.6); import `AllocationLine` +
  `UnallocatedIssue` from `allocation`; new builder params (`account_allocations`,
  `unallocated_account_issues`), breakdown fields, `_applicable_account_allocations`,
  `_account_allocations_by_channel`, the COMPONENT_DERIVED application + dedup, unallocated
  surface, `to_api()` deltas.
- **Modify** `backend/ums_smart_revenue/finance/allocation.py` — import the two constants from
  `deduction_policy` instead of `net_revenue` (removes the cycle edge).
- **Modify** `backend/ums_smart_revenue/api/allocation.py` — refactor to call the new service
  (behavior-preserving).
- **Modify** `backend/ums_smart_revenue/api/revenue.py` — net-revenue route: gather
  allocations, pass to builder, add a `VIEW_FINALIZED_PAYMENTS` gate (on the route's
  `target_scope`, co-scoped with the existing checks — see §5.1) + a `PAYMENT_VIEWED` audit
  (recording `finance_month(month)` as audit metadata), switch the envelope to plural
  `audit_events`, apply the scoped-visibility pin; import `NET_APPLICABLE_COMPONENT_KINDS`
  from `deduction_policy`.
- **Modify** `backend/ums_smart_revenue/api/exports.py` — fetch + pass the same scoped
  channel-direct net-applicable `deduction_components` **and** account allocations into the
  source-summary builder (§5.4); apply the scoped-visibility pin; emit `PAYMENT_VIEWED` for all
  finance-artifact exports (keep `BANK_RECONCILIATION_VIEWED` global-only).
- **Create/extend tests**: `tests/finance/test_deduction_policy.py` (constant-parity),
  `tests/finance/test_net_revenue.py`, `tests/finance/test_allocation_inputs.py`,
  `tests/api/test_net_revenue_api.py`, `tests/api/test_exports_api.py`; keep
  `tests/api/test_allocation_api.py` green.
- **Modify** `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md` — status
  (incl. the `audit_event` → `audit_events` net-route response-shape note).

Reused (no duplication): `AllocationLine`, `UnallocatedIssue`, `build_account_allocation`,
`AccountAllocationResult` (PR-1); `NET_APPLICABLE_COMPONENT_KINDS`,
`SOURCE_SYSTEM_TO_SOURCE_KIND` (now from `deduction_policy`); `list_account_components`,
`list_month_components`, `list_verified_adsense_account_channels`, `list_month_facts`;
`AccessScope`, `Permission`, `AuditEventType`, `record_audit_event`.

---

## 11. Decisions log (resolved during brainstorming)

1. **Slice = net-revenue integration, read/compute only.** Missing-net path only; ACCOUNT-grain
   `net_applicable` TAX/DEDUCTION only. PAYMENT-grain, persistence, committed writes, other
   methods deferred.
2. **Application rule (fork):** account allocations participate **only** in the
   component-derived (missing-net) path, never the trusted source-net path. Source-net
   channels are never downgraded.
3. **D1 breakdown — applied-component semantics.** `deduction_amount_usd` stays source-derived
   on source-net channels; breakdown fields are applied-component values; the sum identity
   holds only on COMPONENT_DERIVED.
4. **Source-align gate:** `net_applicable AND SOURCE_SYSTEM_TO_SOURCE_KIND[source_system] ==
   primary.source_kind` — identical to channel-direct; `basis_source_kind` stays
   provenance-only (with a defensive test).
5. **D2 — net-applicable-only unallocated surface;** reconciliation kinds excluded from the
   net endpoint.
6. **Scoped-visibility pin:** unallocated-account detail is **global-scope-only**; on scoped
   responses both fields serialize as explicit JSON `null` (not zeroed, not omitted); same
   rule for API and exports.
7. **D3 — auth/audit:** keep VIEW_REVENUE + VIEW_CONFIDENCE on `target_scope`; add
   `VIEW_FINALIZED_PAYMENTS` on the **same `target_scope`** (NOT `finance_month` — that would
   regress org-scoped viewers; see §5.1) + a `PAYMENT_VIEWED` audit (recording
   `finance_month(month)` as audit metadata) on the net route.
8. **D4 — explain provenance deferred.**
9. **D5 — exports in-scope:** feed allocations into the export builder (totals correct, no
   drift), render existing fields only; same scoped pin; `PAYMENT_VIEWED` now on **all**
   finance-artifact exports (`BANK_RECONCILIATION_VIEWED` stays global-only).
10. **DRY:** shared `compute_month_account_allocation` orchestrator; `net_revenue` stays
    repository-free.
11. **Import cycle (review #1):** move the two shared net-policy constants to a neutral
    `finance/deduction_policy.py` (Task 0) so `net_revenue` ↔ `allocation` don't form a cycle;
    leave the normalizer's different same-named constant alone.
12. **Export drift is pre-existing (review #2):** exports pass **no** `deduction_components`
    today, so they already drift on channel-direct components. PR-2 fixes both — exports fetch
    the same scoped channel-direct net-applicable components **and** account allocations — so
    "no drift" is actually true.
13. **Serialization (review #3):** scoped unallocated fields are explicit JSON `null` (not key
    omission); global-with-nothing is `"0"`/`[]`. Matches the unconditional `to_api()` style.
14. **Audit envelope (review #4):** the net route moves from singular `audit_event` to plural
    `audit_events` (payment-match precedent) — a deliberate, documented response-shape change.

---

## 12. Decomposition note (remaining Spec 2b after PR-2)

- **Explain-path provenance** — surface account-allocation lines + UNALLOCATED carryover in
  `build_channel_month_revenue_explanation` (needs a net-metric explanation builder).
- **Export layout** — breakdown columns / unallocated rows in workbook/PDF/slide templates.
- **PAYMENT-grain allocation** — once a verified payment→account hop exists.
- **Persistence + committed allocation** — `allocation_results` substrate + audited,
  locked-month-gated committed writes; remaining allocation methods.
