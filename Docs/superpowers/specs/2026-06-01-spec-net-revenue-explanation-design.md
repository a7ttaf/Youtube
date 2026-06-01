# Spec 2b PR-3 — Net-Revenue Explanation Extension (Design)

**Date:** 2026-06-01
**Phase:** 4 — Reconciliation / Allocation (Spec 2b)
**Status:** Draft for review
**Depends on:** Spec 2b PR-1 (account-level allocation compute+read, merged #58) and PR-2
(net-revenue integration, PR #59). PR-3 branches off `main` **after #59 merges**, so it
inherits `build_channel_net_revenue_summary(..., account_allocations=...)`,
`compute_month_account_allocation`, and `finance/deduction_policy.py`.

---

## 1. Goal

Add `net_revenue_usd` as a second supported metric on the existing
`POST /revenue/channels/{channel_id}/months/{month}/explain` endpoint, so finance users
can see **why** a channel's net revenue is what it is — including the channel-direct and
account-allocated deduction provenance produced by PR-1/PR-2 — with the same fail-closed
authorization, audit, and persistence guarantees as the existing gross-revenue explanation.

The existing `adjusted_gross_revenue_usd` metric stays **byte-identical**: same auth, same
singular `audit_event`, same components, same response shape.

## 2. Scope

In scope (one backend PR, read+persist, **no migration**):

- New supported explain metric `net_revenue_usd` on the existing endpoint and service.
- Reuse of PR-2's `build_channel_net_revenue_summary` + `compute_month_account_allocation`
  to derive the channel's net, deduction breakdown, and account-allocation provenance.
- A net-confidence → explain-confidence mapping helper in the finance service (tested).
- A net-metric-only `VIEW_FINALIZED_PAYMENTS@finance_month(month)` authorization gate.
- A net-metric-only plural `audit_events = [REVENUE_VIEWED, PAYMENT_VIEWED]` envelope.
- Deduction provenance recorded in the existing free-form `components` JSONB.
- `422` rejection when the net is indeterminate (no fabricated value).
- `Docs/01` + `Docs/15` status updates.

Out of scope (future Spec 2b PRs):

- PAYMENT-grain allocation, persisted/committed allocation state, other allocation methods.
- Export breakdown columns (separate read-only PR).
- Company/sector/holding-level net explanations (this PR is channel-grain only, matching the
  existing explain endpoint's `entity_type="channel"`).

## 3. Current state (verified anchors)

- **Endpoint** `backend/ums_smart_revenue/api/revenue.py:1328-1393` —
  `explain_channel_month_revenue_metric`. `metric` is a query param defaulting to
  `"adjusted_gross_revenue_usd"`; auth is `VIEW_REVENUE` + `VIEW_CONFIDENCE` at
  `AccessScope.channel(channel_id)` (`:1350-1352`); audit is a singular `audit_event` =
  `REVENUE_VIEWED`, `entity_type="number_explanation"`,
  `entity_id=f"{channel_id}:{month}:{metric}"` (`:1382-1392`); persistence is
  `explanation_repository.record_explanation(explanation)` (`:1369`). It does **not** inject
  the deduction or channel↔account-link repositories today.
- **Service** `backend/ums_smart_revenue/finance/explanations.py` —
  `build_channel_month_revenue_explanation(*, facts, manual_overrides, month,
  youtube_channel_id, metric)` (`:110-117`) rejects any metric other than
  `adjusted_gross_revenue_usd` (`:119`). `NumberExplanationEntry` (`:22-50`) carries
  `value, currency, formula, confidence: dict, components: list[dict], warnings: list[dict]`.
  Confidence is derived from the primary fact's `confidence_score` (`_confidence`, `:198-215`).
  Repository `record_explanation` (`:73-107`) upserts on
  `(tenant_id, month, entity_type, entity_id, metric)`.
- **Persistence** `backend/ums_smart_revenue/db/explanation_models.py:23-91` —
  `components` and `warnings` are `JSON().with_variant(JSONB)`, `nullable=False`,
  default `[]`, **no CHECK constraint** (migration `20260510_0007_number_explanations.py`
  + `20260517_0001` confirm only month/entity_type/currency CHECKs). `value` is
  `Numeric(18,6) NOT NULL`. Unique key includes `metric`, so a new metric upserts into its
  own row without colliding with the gross row.
- **Net-revenue reuse** `backend/ums_smart_revenue/finance/net_revenue.py` —
  `build_channel_net_revenue_summary(*, facts, manual_overrides, month=None,
  youtube_channel_id=None, deduction_components=(), account_allocations=())` (`:333-341`)
  returns `ChannelNetRevenueSummary` (`:24-44`) with `net_revenue_usd`,
  `deduction_amount_usd`, `channel_direct_deduction_amount_usd`,
  `account_allocated_deduction_amount_usd`, `confidence`. The breakdown fields are populated
  only on the COMPONENT_DERIVED path and are `None` on the source-net path.
  `_applicable_account_allocations(allocations, *, youtube_channel_id, primary_source_kind)`
  (`:196-213`) is the channel + net-applicable + source-aligned filter;
  `component_key` dedup is the defensive net-builder behavior (`:401`).
- **Confidence labels** — `build_channel_net_revenue_summary` emits exactly three
  `confidence` values: `B_RECONCILED` (source-net, no pending override, `:324`),
  `D_ESTIMATED` (COMPONENT_DERIVED `:246`, or source-net with pending override `:324`),
  `E_MISSING` (NET_REVENUE_SOURCE_MISSING `:280`, and the NO_FACTS empty summary `:698`).
  **Invariant:** `net_revenue_usd is None` exactly when `confidence == "E_MISSING"`
  (`:275`, `:280`, `:693`, `:698`). There is **no** `"NO_FACTS"` confidence label.
- **Auth model** — `backend/ums_smart_revenue/api/revenue.py:1098-1102` shows the PR-2 net
  route's gate: `VIEW_REVENUE` + `VIEW_CONFIDENCE` on the route `target_scope`, then
  `VIEW_FINALIZED_PAYMENTS` on `AccessScope.finance_month(month)` (called **without**
  `org_index` — finance_month is not an org-hierarchy scope).
  `backend/ums_smart_revenue/auth/user_permissions.py:41` restricts
  `VIEW_FINALIZED_PAYMENTS` to `{GLOBAL, FINANCE_MONTH}` scope-types, so a `@channel`
  gate for it would be structurally unsatisfiable. `auth/seed.py` confirms FINANCE_ADMIN,
  FINANCE_APPROVER, and FINANCE_VIEWER each hold both `VIEW_REVENUE` and
  `VIEW_FINALIZED_PAYMENTS`.

## 4. Architecture & data flow

```txt
POST /revenue/channels/{channel_id}/months/{month}/explain?metric=net_revenue_usd
  -> route: resolve target_scope = AccessScope.channel(channel_id)
  -> route: VIEW_REVENUE@channel + VIEW_CONFIDENCE@channel           (existing checks)
  -> route: VIEW_FINALIZED_PAYMENTS@finance_month(month)             (NET-METRIC-ONLY, new)
  -> route: gather facts, overrides (existing) +
            channel-direct net-applicable deduction_components (this channel) +
            compute_month_account_allocation(month, ...).lines       (month-wide, PR-2 reuse)
  -> service: build_channel_month_revenue_explanation(metric=net_revenue_usd, ...)
       -> build_channel_net_revenue_summary(...)                     (PR-2 reuse)
       -> if summary.net_revenue_usd is None: raise -> 422           (indeterminate net)
       -> components = baseline gross, approved overrides,
                       deduction provenance (path-dependent),
                       confidence = map_net_confidence(summary.confidence)
  -> route: explanation_repository.record_explanation(entry)         (idempotent upsert)
  -> route: audit_events = [REVENUE_VIEWED, PAYMENT_VIEWED]          (NET-METRIC-ONLY)
```

The gross metric path is unchanged end-to-end.

## 5. Detailed design

### 5.1 Endpoint contract

- Request: unchanged route; `metric` query param now accepts `net_revenue_usd` in addition
  to the default `adjusted_gross_revenue_usd`. No request body.
- Response (gross metric): unchanged — singular `audit_event`.
- Response (net metric): `NumberExplanationEntry.to_api()` plus `audit_events`
  (plural list of API-serialized audit records). The net response does **not** carry a
  singular `audit_event` key.
- An unsupported metric continues to raise `NumberExplanationValidationError` →
  HTTP `422` (existing behavior; net is now supported).

### 5.2 Authorization (net metric only)

The net metric performs, in order, fail-closed before any data access:

```py
target_scope = AccessScope.channel(channel_id)
_require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
_require_permission(user, Permission.VIEW_CONFIDENCE, target_scope, org_index)
_require_permission(
    user, Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month(month)
)
```

The third check is **net-metric-only** and mirrors the PR-2 net route exactly
(`revenue.py:1100-1102`): scope `AccessScope.finance_month(month)`, no `org_index`.
The gross metric keeps only the first two checks. Rationale: net explanations expose
payment/deduction-derived provenance, so `VIEW_REVENUE` + `VIEW_CONFIDENCE` alone is too
weak; `VIEW_FINALIZED_PAYMENTS` is the correct permission and `{GLOBAL, FINANCE_MONTH}` is
its only valid *direct-grant* scope-type set per `user_permissions.py:41`.

**Access boundary — intentional, NOT access-transparent.** This gate is identical to the
already-merged PR-2 net-revenue route, so it adds no *new* boundary; it makes *explaining*
net consistent with *reading* net. But it is **not** transparent to every principal who can
read gross. `has_permission` authorizes a role assignment via
`index.contains(assignment.scope, target_scope)` (`policy.py:36-41`) and does **not** clamp
role-derived grants by `PERMISSION_SCOPE_TYPES` (that allowlist only validates *direct*
grants, `user_permissions.py:399`). `FINANCE_VIEWER.allowed_scope_types=_ORG_SCOPES`
(`roles.py:72`), so a company/sector/channel-scoped `FINANCE_VIEWER` holds
`VIEW_FINALIZED_PAYMENTS` only at that **org** scope, and `OrgAccessIndex.contains` has no
cross-type rule mapping an org grant to a `finance_month` target (`scopes.py:57-87`).
Consequence: such a principal passes `VIEW_REVENUE@channel` (gross → `200`) but is
**`403`'d on the net metric**. Net-explanation access therefore requires finalized-payment
visibility at `GLOBAL` (role/grant assigned globally → `contains(GLOBAL, finance_month)`
True) or a direct `FINANCE_MONTH(month)` grant — exactly as on the PR-2 net-revenue route.
This is deliberate: the net number and its explanation share one access boundary. §8 pins
both sides of it.

### 5.3 Audit envelope (net metric only)

The net metric emits a plural `audit_events` list (mirroring the PR-2 net route precedent):

- `REVENUE_VIEWED` — `entity_type="number_explanation"`,
  `entity_id=f"{channel_id}:{month}:net_revenue_usd"`, `scope=target_scope` (channel),
  `details={"metric": "net_revenue_usd", "warning_count": <n>}`.
- `PAYMENT_VIEWED` — `scope=AccessScope.finance_month(month)`,
  `entity_type="finance_month"`, `entity_id=month`,
  `details={"metric": "net_revenue_usd"}` — recording finalized-payment data access at the
  finance-month grain, consistent with PR-2.

The gross metric keeps its singular `audit_event = REVENUE_VIEWED`.

### 5.4 Service layer

`build_channel_month_revenue_explanation` gains two optional, default-empty params so the
gross path is unchanged:

```py
def build_channel_month_revenue_explanation(
    *,
    facts: list[RevenueFactEntry],
    manual_overrides: list[RevenueManualOverrideEntry],
    month: str,
    youtube_channel_id: str,
    metric: str,
    deduction_components: Iterable[DeductionComponent] = (),
    account_allocations: Iterable[AllocationLine] = (),
) -> NumberExplanationEntry:
```

- `metric == "adjusted_gross_revenue_usd"` → existing code path, untouched.
- `metric == "net_revenue_usd"` → new internal builder
  `_build_net_revenue_explanation(...)` that:
  1. Calls `build_channel_net_revenue_summary(facts=..., manual_overrides=..., month=...,
     youtube_channel_id=..., deduction_components=..., account_allocations=...)`.
  2. If `summary.net_revenue_usd is None` → raise `NumberExplanationValidationError`
     (→ 422; see §5.7). This is exactly the `E_MISSING` set.
  3. Otherwise build the `NumberExplanationEntry` with
     `metric="net_revenue_usd"`, `value=summary.net_revenue_usd`, `currency="USD"`,
     a path-dependent `formula`, the components in §5.5, the warnings in §5.6, and
     `confidence=map_net_confidence(summary.confidence)` (§5.6).
- `metric` not in the supported set → `NumberExplanationValidationError` (unchanged).

The route gathers `account_allocations` from `compute_month_account_allocation(month=month,
deduction_repository=..., revenue_repository=..., link_repository=...).lines` (month-wide
basis, as PR-2 requires — single-channel allocation is not computable in isolation).

**Shared provenance helpers (no copied logic — required).** The channel/source/net-applicable
filter and `component_key` dedup the net builder uses today live in private functions
(`net_revenue.py:_applicable_deduction_components:176`, `_applicable_account_allocations:196`,
and the dedup at `:401`). This PR **extracts those into shared, importable helpers** that
return the channel's applicable, deduped per-channel lines, and has BOTH
`build_channel_net_revenue_summary` (to compute its totals) AND
`_build_net_revenue_explanation` (to enumerate the provenance arrays) call the **same**
helper. This is a behavior-preserving refactor of the net builder — its computed totals,
statuses, and confidence are unchanged. The explanation builder MUST NOT re-implement or
copy the filter/dedup; deriving provenance and total from one shared helper **guarantees no
drift**: the explanation's `account_allocated_deduction_usd.value` is the sum of exactly the
`allocations[]` it lists, which equals the summary's `account_allocated_deduction_amount_usd`.
The same shared-helper rule applies to the channel-direct lines vs
`channel_direct_deduction_amount_usd`. Provenance arrays are derived only from these shared
helpers — never the raw month-wide lines, never a re-implemented copy. A test asserts the
sum-identity on the COMPONENT_DERIVED path to lock the no-drift contract.

`formula` strings:

- COMPONENT_DERIVED:
  `"net_revenue_usd = adjusted_gross_revenue_usd - channel_direct_deduction_amount_usd - account_allocated_deduction_amount_usd"`
- source-net (CALCULATED / PENDING_OVERRIDE_REVIEW):
  `"net_revenue_usd = source-reported net (deduction_amount_usd = adjusted_gross_revenue_usd - net_revenue_usd)"`

### 5.5 Components JSON (provenance)

All `value`/amount fields are `decimal_to_api`-formatted strings; `basis_share` is the
`decimal_to_api` of the `AllocationLine.basis_share`. Component order is deterministic and
stable for tests.

Common (both paths):

```jsonc
{ "key": "baseline_gross_revenue_usd", "label": "Baseline gross revenue",
  "value": "<gross>", "source_kind": "<primary>", "source_report_id": "<id|null>" }
{ "key": "approved_manual_override_total_usd", "label": "Approved manual overrides",
  "value": "<total>", "count": <int> }
```

Source-net path adds one source-derived deduction component (breakdown fields are `None`,
so no split, no allocation provenance):

```jsonc
{ "key": "source_reported_deduction_usd", "label": "Source-reported deductions",
  "value": "<deduction_amount_usd>", "source_kind": "<primary_source_kind>" }
```

COMPONENT_DERIVED path adds two deduction components:

```jsonc
{ "key": "channel_direct_deduction_usd", "label": "Channel-direct deductions",
  "value": "<channel_direct_deduction_amount_usd>", "count": <int>,
  "components": [
    { "component_kind": "TAX|DEDUCTION", "source_system": "<system>",
      "component_key": "<key>", "amount_usd": "<amount>" }
  ] }
{ "key": "account_allocated_deduction_usd", "label": "Account-allocated deductions",
  "value": "<account_allocated_deduction_amount_usd>", "count": <int>,
  "allocations": [
    { "adsense_account_id": "<acct>", "component_kind": "TAX|DEDUCTION",
      "source_system": "<system>", "component_key": "<key>",
      "basis_source_kind": "<kind>", "basis_share": "<decimal>",
      "allocated_amount_usd": "<amount>" }
  ] }
```

- The `allocations` array is the channel's applicable lines from the **shared helper**
  (§5.4) — source-aligned, net-applicable, `component_key`-deduped `AllocationLine`s —
  verbatim provenance from the dataclass (`allocation.py:97-110`), nothing more. Sorted
  deterministically by `(adsense_account_id, component_key)`.
- The `channel_direct_deduction_usd.components` array is the channel's applicable lines from
  the **shared helper** (§5.4), using the `DeductionComponent` fields the net builder
  already consumes (exact field names verified at plan time: `component_kind`,
  `source_system`, `component_key`, amount). Sorted deterministically by
  `(source_system, component_key)`.
- No raw payloads, no unrelated account/channel data, no month-wide rows.

### 5.6 Confidence & warnings (net metric)

A small tested helper maps the net summary's confidence label into the explain HIGH/MEDIUM/LOW
shape. Located in the finance service (not inline in the route):

```py
def map_net_confidence(label: str) -> dict[str, str]:
    return {
        "B_RECONCILED": {"label": "HIGH", "score": "0.95"},
        "D_ESTIMATED":  {"label": "MEDIUM", "score": "0.80"},
        "E_MISSING":    {"label": "LOW", "score": "0"},
    }.get(label, {"label": "LOW", "score": "0"})
```

`E_MISSING` is mapped defensively (and unit-tested) even though §5.7 rejects those before
persistence; the `.get(..., LOW/0)` default fails safe for any future label. The gross
metric's `_confidence` (fact-score) logic is unchanged.

Warnings (net metric): carry forward the relevant net-summary `issues` (e.g.
`NET_REVENUE_SOURCE_MISSING`, `NO_REVENUE_FACTS`) as warning dicts `{ "code", "message" }`,
plus existing pending-override warnings where applicable. (Indeterminate-net issues lead to
422, not a persisted warning.)

### 5.7 Indeterminate net → 422 (no fabricated value)

When `summary.net_revenue_usd is None` (exactly the `E_MISSING` cases:
`NET_REVENUE_SOURCE_MISSING` and `NO_FACTS`), the service raises
`NumberExplanationValidationError`; the route translates it to HTTP `422` and **nothing is
persisted**. We do not write a placeholder `0` into the `NOT NULL` `value` column — this
honors the repo's "without inventing values" rule
(`tests/finance/test_net_revenue.py:89`) and the `net_revenue_usd=None` contract
(`net_revenue.py:275`, `:693`).

### 5.8 Persistence

Unchanged mechanism. The net explanation upserts via `record_explanation` into its own row
keyed by `(tenant_id, month, "channel", channel_id, "net_revenue_usd")`. `components` and
`warnings` accept the new nested shapes (free-form JSONB, no CHECK). **No migration.**

## 6. Error handling

- Auth failures → `403` via `_require_permission` (fail-closed, before any data access).
- Indeterminate net → `422` (§5.7).
- Unsupported metric → `422` (existing).
- Malformed month → existing validation path (unchanged).
- Typed domain errors only (`NumberExplanationValidationError`); no bare `except`, no leaked
  internals.

## 7. Blast radius

- **Tables/ORM affected:** `number_explanations` (write path; existing table; **no schema
  change** — free-form `components`/`warnings` JSONB). Reads `revenue_facts`,
  `revenue_manual_overrides`, `deduction_components`, channel↔account link tables via
  existing repositories.
- **PostgreSQL remains source of truth.** USD-only preserved.
- **Authorization:** strictly *more* restrictive for the new metric
  (`+VIEW_FINALIZED_PAYMENTS@finance_month`); gross metric unchanged; no permission
  weakened. **Not access-transparent (see §5.2):** an org-scoped (company/sector/channel)
  `FINANCE_VIEWER` holds `VIEW_FINALIZED_PAYMENTS` only at that org scope, which cannot
  contain a `finance_month` target — so they read gross but are intentionally `403`'d on
  net, exactly as on the merged PR-2 net-revenue route (no *new* boundary). Net access
  requires finalized-payment visibility at `GLOBAL` or a direct `FINANCE_MONTH(month)`
  grant. Fail-closed throughout; both sides of the boundary are pinned by §8 tests.
- **Audit:** net metric adds `PAYMENT_VIEWED` (more coverage, not less); response shape
  change is additive and net-metric-only.
- **Finance results:** no change to computed numbers — this reuses PR-2's net builder
  verbatim and only *explains* the existing figures.
- **Neo4j / graph projection:** `No graph projection impact detected.` (explain is a
  Postgres read+persist path with no graph writes or reads).
- **Migration / reset:** none required.

## 8. Testing requirements

Finance service (`tests/finance/`):

- `map_net_confidence` pins each of `B_RECONCILED→HIGH/0.95`, `D_ESTIMATED→MEDIUM/0.80`,
  `E_MISSING→LOW/0`, and an unknown label → `LOW/0`.
- Net explanation builder: source-net path (single source-derived deduction component, no
  split); COMPONENT_DERIVED path (channel-direct + account-allocated components with exact
  nested provenance, deterministic order, `component_key` dedup); indeterminate net
  (`E_MISSING`) raises `NumberExplanationValidationError`; sum identity
  `value == adjusted_gross - channel_direct - account_allocated` on COMPONENT_DERIVED.
- Gross path regression: builder output for `adjusted_gross_revenue_usd` byte-identical.

API (`tests/api/test_net_revenue_api.py` or the explain test module):

- Net metric happy path returns `net_revenue_usd` value + provenance components + plural
  `audit_events = [REVENUE_VIEWED, PAYMENT_VIEWED]`, and persists one upserted row.
- Auth — boundary pinned on BOTH sides:
  (a) a principal with only `VIEW_REVENUE`+`VIEW_CONFIDENCE@channel` (no finalized-payment)
  → `403` on net, `200` on gross;
  (b) an **org-scoped (company/sector) `FINANCE_VIEWER`** — holds `VIEW_FINALIZED_PAYMENTS`
  only at the org scope — → `403` on net, `200` on gross (documents the intentional
  boundary from §5.2);
  (c) a viewer holding `VIEW_FINALIZED_PAYMENTS` at `GLOBAL` or via a direct
  `FINANCE_MONTH(month)` grant → `200` on net with full provenance + plural `audit_events`;
  (d) missing `VIEW_REVENUE` → `403`; (e) disabled user → `403`. Fail-closed throughout.
- Indeterminate net (no facts / missing source net) → `422`, nothing persisted.
- Gross metric unchanged: singular `audit_event`, no `VIEW_FINALIZED_PAYMENTS` required.
- Idempotency: two net-metric calls upsert one row; gross + net coexist as two rows.

Run with the Postgres test container (`UMS_TEST_DATABASE_URL`) for the PG-tier assertions.

## 9. Files changed / not changed

Changed:

- `backend/ums_smart_revenue/finance/explanations.py` — net metric branch, optional
  `deduction_components`/`account_allocations` params, `_build_net_revenue_explanation`,
  `map_net_confidence`, supported-metric set.
- `backend/ums_smart_revenue/finance/net_revenue.py` — behavior-preserving extraction of the
  applicable-filter + `component_key`-dedup logic into shared importable helpers consumed by
  both the net builder and the explanation builder (§5.4). No change to computed totals,
  statuses, or confidence.
- `backend/ums_smart_revenue/api/revenue.py` — explain handler: inject deduction +
  channel↔account-link repositories (reuse PR-2's local providers), net-metric auth gate,
  gather inputs, plural `audit_events`, metric-conditional response.
- `tests/finance/...` + `tests/api/...` — per §8.
- `Docs/01_IMPLEMENTATION_PLAN.md` + `Docs/15_DELIVERY_BACKLOG.md` — mark PR-3 shipped.

Not changed:

- Any Alembic migration / ORM schema (no migration).
- The gross-revenue explanation path (auth, audit, components, response).
- PR-2 net-revenue / allocation *behavior* — totals, statuses, and confidence unchanged
  (`net_revenue.py` gains only a behavior-preserving extraction of its existing
  filter/dedup helpers for shared reuse; see §5.4). `allocation.py` /
  `allocation_inputs.py` / `deduction_policy.py` reused unmodified.
- CODEOWNERS / branch protection.

## 10. Validation gate

- `python -m ruff check backend tests`
- `pytest -q` (with `UMS_TEST_DATABASE_URL` Postgres container)
- targeted: `tests/finance/test_*explanation*` (new), `tests/api/test_net_revenue_api.py`,
  explain endpoint tests
- `git diff --check`
- every commit trailer-free (no Co-Authored-By / Claude footer)

## 11. Non-goals / future

- PAYMENT-grain allocation, persisted/committed allocation, other allocation methods.
- Export breakdown columns.
- Non-channel (company/sector/holding) net explanations.
