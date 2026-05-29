# Deduction-Evidence Substrate + Ingestion — Design Spec

**Date:** 2026-05-29
**Branch:** `spec/deduction-components`
**Status:** Design approved (brainstorming); pending spec review → implementation plan.
**Phase:** Phase 4 reconciliation — **Spec 1 of 2** (Spec 2 = allocation rules + committed
`/recalculate`; explicitly out of scope here).

---

## 0. Context

Phase 4's documented blockers to a fully-reconciled net figure are "tax/deduction
ingestion" and "allocation rules" (`Docs/01_IMPLEMENTATION_PLAN.md` Phase 4). This spec
covers the first, dependency-prior half: a substrate that **captures source-reported
deduction evidence**, so a later allocation spec has typed inputs to distribute.

Relevant existing substrate (verified):
- `finance/net_revenue.py` — `build_month_net_revenue_summary` / `build_channel_net_revenue_summary`.
  Today `net = primary fact.net_revenue_usd + Σ(approved overrides)`; deduction reported is the
  source fact's own `gross − net`. If `primary.net_revenue_usd` is missing it returns
  `NET_REVENUE_SOURCE_MISSING` (no derived deduction). USD-only.
- `db/source_models.py` — `GoogleRevenueSourceRowORM` (carries `source_account_id` NOT NULL,
  nullable `youtube_channel_id`, `value_kind ∈ {estimated,settled,adjustment,tax,deduction}`,
  `amount_native`, `currency_code`, unique `(tenant_id, source_system, source_row_key)`; already
  carries finite-NUMERIC + object-only `raw_payload` CHECKs we mirror below).
- `db/finance_models.py` — `BankReconciliationEntryORM` (keyed `(tenant_id, month, bank_reference)`;
  `transfer_fee_usd` with `≥0` CHECK; `fx_difference_usd` with **no** sign CHECK → signed-capable);
  `AdSensePaymentORM` (`source_account_id`, `payment_status`, USD per row).
- `finance/google_source_normalizer.py` — turns source rows into revenue facts but **drops**
  `value_kind ∈ {tax,deduction,adjustment}` (`_UNSUPPORTED_VALUE_KINDS`). This spec does **not**
  change the normalizer; it adds a separate consumer that reads those rows directly.

Governing rules:
- **`Docs/18`**: Google/AdSense source-reported money is the official finance source; public/
  provider FX is **barred** from official totals. Tax/deduction values ride as source-reported
  native currency on source rows.
- **PostgreSQL is the finance source of truth.** Neo4j is retired (PR #12) — no graph impact.

---

## 1. Core principle (non-negotiable)

**A deduction is source-reported evidence, never a figure UMS computes.** The system ingests
and *labels* what Google/AdSense/the bank already report; it never invents tax, withholding, or
fees. Every component carries a **strict source label** reflecting its proven cause:

- `TAX` / `DEDUCTION` — an explicit tax/deduction line from a source report (`value_kind`).
- `TRANSFER_FEE` — a bank-reported transfer fee (true deduction evidence).
- `FX_VARIANCE` — a **signed** bank-reported FX difference; variance evidence, **not** a blind
  deduction.
- `UNRESOLVED_PAYMENT_GAP` — the shortfall between finalized AdSense earnings and the actual
  AdSense payment; **reconciliation evidence only**, never labeled tax/withholding/fee unless a
  source proves the cause.

AdSense reporting is **account-scoped** (no `youtube_channel_id`); account/payment-level evidence
is captured and **left unallocated** here. Distributing it to channels is Spec 2.

---

## 2. Goal & deliverables

1. **`deduction_components` table** (+ Alembic migration + Postgres round-trip test) — the
   substrate.
2. **Three ingestion adapters** (pure mapping + idempotent upsert) that turn existing
   source-of-truth rows into typed, labeled components.
3. **An ingestion runner** (service + operator CLI, mirroring `run_adsense_payment_sync.py`) that
   executes the adapters for a tenant+month under the month-lock gate, with audit.
4. **Channel-direct `net_revenue` wiring** with strict anti-double-count + anti-cross-source
   guards (§6).
5. **A read-only endpoint** `GET /revenue/months/{month}/deduction-components` for finance/
   month-close visibility and as a stable contract for Spec 2 (§7).
6. Tests at every layer (§11).

**PR split:** delivered as two PRs. **PR-A** = substrate + migration + repository/service + the
three ingestion adapters + CLI + idempotency/month-lock/audit tests (deliverables 1–3). **PR-B** =
`net_revenue` wiring + read endpoint + auth/audit/API tests (deliverables 4–5). PR-A lands the
migration + ingestion substrate before PR-B touches official finance-number behavior and the public
API surface. **The implementation plan that follows this spec covers PR-A first.**

---

## 3. Non-goals (explicit)

- **No allocation** of ACCOUNT/PAYMENT-scoped components to channels.
- **No committed `/recalculate`** writes; that endpoint stays preview-only.
- **No manual deduction entry** (deferred until authoritative external tax documents exist and we
  accept the added auth/approval/month-lock surface).
- **No parser change to *emit* `tax`/`deduction`** source rows; this spec builds the *consumer*
  only (the path is ready but dormant until an authoritative emitter exists).
- **No FX/currency conversion.** USD-only; non-USD evidence is skipped + flagged (per `Docs/18`).
- **No change to the official net path when `primary.net_revenue_usd` exists** (see §6).
- **No `google_revenue_source_rows` reuse** as the substrate (Google-specific, source-system
  CHECK, non-negative amounts — wrong fit for bank/computed evidence).

---

## 4. Data model — `deduction_components`

New table on `FinanceBase`, tenant-scoped, additive (backward-compatible; no change to existing
tables).

| Column | Type | Notes |
|---|---|---|
| `id` | uuid pk | `gen_random_uuid()` |
| `tenant_id` | uuid | NOT NULL, UMS-tenant default (matches siblings) |
| `month` | text | `YYYY-MM` CHECK (same format CHECK as sibling tables) |
| `component_kind` | text | CHECK ∈ `TAX, DEDUCTION, TRANSFER_FEE, FX_VARIANCE, UNRESOLVED_PAYMENT_GAP` |
| `scope_kind` | text | CHECK ∈ `CHANNEL, ACCOUNT, PAYMENT` |
| `scope_id` | text | NOT NULL — `youtube_channel_id` (CHANNEL) / `source_account_id` (ACCOUNT) / `bank_reference` (PAYMENT) |
| `amount_usd` | Numeric(18,6) | **signed** (FX_VARIANCE / gap may be negative); finite CHECK (below) |
| `amount_native` | Numeric(20,6) | nullable; finite CHECK when not null |
| `currency_code` | text | 3-char upper CHECK; the source-reported currency (USD for fee/FX/gap) |
| `source_system` | text | e.g. `adsense_management`, `youtube_reporting`, `bank_reconciliation`, `adsense_payment_gap` |
| `source_table` | text | origin table name |
| `source_id` | text nullable | origin row id (uuid as text) where applicable |
| `source_key` | text nullable | origin natural key (e.g. `source_row_key`, `bank_reference`) |
| `source_report_id` | text nullable | provenance |
| `raw_payload` | JSONB | **NOT NULL DEFAULT `'{}'`**, object-only CHECK; provenance/audit; **not** in the default read response |
| `component_key` | text | **stable idempotency key** (see below) |
| `created_at` / `updated_at` | timestamptz | `now()` / `onupdate now()` |

- **Unique:** `(tenant_id, component_key)` — idempotent upsert target.
- **Indexes:** `(tenant_id, month)`, `(tenant_id, scope_kind, scope_id)`, `(tenant_id, month, component_kind)`.
- **Hardening CHECKs** (Postgres `NUMERIC` can store `NaN`, and a direct-SQL/backfill/future-service
  writer must be fenced — mirror the `google_revenue_source_rows` guards):
  - `amount_usd` finite: `amount_usd > '-Infinity'::numeric AND amount_usd < 'Infinity'::numeric`
    (rejects `NaN` and ±`Infinity`; signed so both bounds), Postgres-only via
    `.ddl_if(dialect="postgresql")`.
  - `amount_native` finite when present: `amount_native IS NULL OR (amount_native > '-Infinity'::numeric
    AND amount_native < 'Infinity'::numeric)`, Postgres-only.
  - `raw_payload` object-only: `jsonb_typeof(raw_payload) = 'object'`, Postgres-only; column is
    `NOT NULL DEFAULT '{}'`.
  - `month` `YYYY-MM` format CHECK + `currency_code` 3-char-upper CHECK + `component_kind` /
    `scope_kind` enum CHECKs (as in the table above).
- **`component_key` formats** (deterministic, stable across re-runs):
  - source-row tax/deduction → `srcrow:{source_system}:{source_row_key}`
  - bank fee → `bank:{month}:{bank_reference}:transfer_fee`
  - bank FX → `bank:{month}:{bank_reference}:fx_variance`
  - AdSense gap → `adsense_gap:{source_account_id}:{month}`
- **Migration:** Alembic revision adding the table; a Postgres round-trip test on disposable
  `postgres:18-alpine` (constraints + indexes + idempotent upsert), matching the repo's
  `*_postgres.py` pattern.

---

## 5. Ingestion adapters

Layering (keeps pure logic separate from I/O, per existing patterns):
- `finance/deduction_components.py` — `DeductionComponent` frozen dataclass + **pure mapping
  functions** (`map_source_rows_to_components`, `map_bank_entries_to_components`,
  `map_adsense_gap_to_components`) + `component_key` builders + `.to_api()`. No DB/I/O.
- `finance/deduction_ingestion.py` — `SqlAlchemyDeductionComponentRepository`
  (`upsert_components` idempotent by `component_key`; `list_month_components`) + an
  `DeductionIngestionService` that reads the sources, calls the pure mappers, and upserts under
  the month-lock gate. Typed `DeductionIngestionValidationError`.
- `scripts/run_deduction_ingestion.py` — operator CLI (`--tenant`, `--month`, `--reason`,
  optional `--source`), mirroring `run_adsense_payment_sync.py`. **Actor:** a fail-closed
  `RUN_CONNECTOR_JOBS` service principal via `build_connector_service_principal(tenant_id=...)`
  (no human-header path). **Audit:** one new `AuditEventType.DEDUCTION_COMPONENTS_INGESTED`
  event per run (sensitive; finance), scoped to `finance_month(month)`, carrying only summary
  counts (components upserted per kind, non-USD skipped) — never amounts/payloads. **Month-lock:**
  refuses `LOCKED` months.

All adapters: **idempotent** (re-run replaces by `component_key`), **USD-only** (non-USD evidence
skipped + counted in the run summary, never converted), **month-lock-gated** writes (acquire the
same advisory lock / `_require_month_open` path other finance writers use; refuse on `LOCKED`),
and **audited** per run.

**5.1 `value_kind` consumer** (`source_system` carried from the row)
- Source: `google_revenue_source_rows` WHERE `value_kind ∈ {tax, deduction}` for the tenant+month.
- Kind: `TAX` or `DEDUCTION` (1:1 with `value_kind`).
- Scope: `CHANNEL` (`youtube_channel_id`) when present, else `ACCOUNT` (`source_account_id`).
- Amount: `amount_native`; if `currency_code != USD`, **skip + flag** (no conversion). When USD,
  `amount_usd = amount_native`.
- Key: `srcrow:{source_system}:{source_row_key}`.
- **Dormant today** (no emitter writes `tax`/`deduction` rows yet) — exercised with synthetic
  source rows in tests; ready for a future authoritative emitter.

**5.2 Bank fee / FX adapter**
- Source: `bank_reconciliation_entries` for the tenant+month.
- `TRANSFER_FEE` when `transfer_fee_usd > 0`: scope `PAYMENT` (`bank_reference`),
  `amount_usd = transfer_fee_usd` (≥0), `currency_code = USD`.
- `FX_VARIANCE` when `fx_difference_usd != 0`: scope `PAYMENT`, `amount_usd = fx_difference_usd`
  (**signed**), `currency_code = USD`.
- Keys: `bank:{month}:{bank_reference}:transfer_fee` / `bank:{month}:{bank_reference}:fx_variance`
  (month is included because bank uniqueness is `(tenant_id, month, bank_reference)` — a reference
  reused in a later month must not collide under `unique (tenant_id, component_key)`).

**5.3 AdSense earnings→payment gap adapter**
- Earnings: sum `amount_native` of `google_revenue_source_rows` WHERE
  `source_system='adsense_management'` AND `value_kind='settled'` (the finalized figure), grouped
  by `source_account_id` + month, USD only. (If no `settled` rows exist for an account+month, emit
  **no** gap component — we never derive a gap from preliminary `estimated` figures.)
- Paid: sum `payment_amount` of `adsense_payments` WHERE `payment_status='PAID'`, USD, grouped by
  `source_account_id` + month.
- Emit `UNRESOLVED_PAYMENT_GAP` only when **both** a settled-earnings total and a PAID total exist
  for `(source_account_id, month)` and they differ: `amount_usd = settled_earnings_usd − paid_usd`
  (**signed**), scope `ACCOUNT` (`source_account_id`), `source_system='adsense_payment_gap'`.
- Key: `adsense_gap:{source_account_id}:{month}`. **Labeled only as an unresolved gap.**

---

## 6. `net_revenue` wiring (channel-direct, guarded)

`build_channel_net_revenue_summary` gains a `deduction_components` input (the API passes the
month's components). The rule **prevents double-counting and cross-source mixing**:

- **If `primary.net_revenue_usd` exists:** the official-net path is **unchanged**. Deduction
  components are **not** subtracted (the source's own `gross − net` already reflects them). Channel
  `TAX`/`DEDUCTION` components may be surfaced as *evidence/reconciliation context* only.
- **If `primary.net_revenue_usd` is missing** (today → `NET_REVENUE_SOURCE_MISSING`):
  `component_derived_net_revenue_usd = adjusted_gross_revenue_usd − Σ(applicable channel TAX/DEDUCTION components)`,
  where `adjusted_gross_revenue_usd = primary.gross_revenue_usd + Σ(approved manual overrides)`
  (the same adjusted-gross the existing builder computes, so override handling is unchanged), and
  "applicable" = `scope_kind == CHANNEL`, `scope_id == channel`, **and** the component's
  `source_system` maps to the chosen primary `source_kind` via an explicit
  `SOURCE_SYSTEM_TO_SOURCE_KIND` map (no mixing YouTube Reporting / Analytics / AdSense evidence).
  The reported `deduction_amount_usd` for such a channel therefore equals the applied component sum.
- **ACCOUNT and PAYMENT** components, and **`TRANSFER_FEE` / `FX_VARIANCE` / `UNRESOLVED_PAYMENT_GAP`**,
  **never** affect channel net in Spec 1.
- Component-derived net is marked with a **distinct** `status = COMPONENT_DERIVED` and
  `confidence = D_ESTIMATED` (never `B_RECONCILED`).

This path is **dormant today** (no channel-scoped emitter), but fully wired and tested with
synthetic channel-scoped components — the smallest correct end-to-end deduction path, with no
allocation and no invented tax.

`SOURCE_SYSTEM_TO_SOURCE_KIND` (initial, conservative; refine if a real emitter lands):
`adsense_management → ADSENSE`, `youtube_reporting → YOUTUBE_CMS`,
`youtube_analytics → YOUTUBE_ANALYTICS` (exact `RevenueFactSourceKind` values:
`YOUTUBE_CMS, YOUTUBE_ANALYTICS, ADSENSE, MANUAL_UPLOAD, ALLOCATION`).

---

## 7. Read endpoint

`GET /revenue/months/{month}/deduction-components` — **read-only**; never triggers ingestion,
allocation, recalculation, or any write.

- **Response:** grouped totals + per-component rows, with `scope_kind` (CHANNEL/ACCOUNT/PAYMENT)
  clearly distinguished so no consumer mistakes account/payment evidence for allocated channel
  deductions. Exposes `source_system`/`source_table`/`source_id`/`source_key`/`source_report_id`
  provenance but **never `raw_payload`** in the default response.
- **Filters / pagination:** `scope_kind`, `component_kind`, and a channel/account filter;
  deterministic ordering; bounded page size (mirror the AdSense payments listing conventions).
- **Auth (mirror `/months/{month}/smart-alerts` exactly — scopes differ by permission):**
  `VIEW_REVENUE` (`finance.view_revenue`) and `VIEW_CONFIDENCE` (`analytics.view_confidence`) on
  **`AccessScope.global_scope()`**; `VIEW_FINALIZED_PAYMENTS` (`finance.view_finalized_payments`)
  and `VIEW_BANK_RECONCILIATION` (`finance.view_bank_reconciliation`) on
  **`AccessScope.finance_month(month)`**. All four required, fail-closed.
- **Audit (sensitive views):** record `REVENUE_VIEWED`, plus `PAYMENT_VIEWED` when ACCOUNT/gap
  evidence is included and `BANK_RECONCILIATION_VIEWED` when PAYMENT (fee/FX) evidence is included.

---

## 8. Error handling, currency, fail-closed

- Typed domain errors (`DeductionIngestionValidationError`) translated at the route/CLI boundary;
  no bare excepts, no secret/SQL leakage in messages or audit details.
- **USD-only:** non-USD evidence is skipped and counted in the ingestion run summary; never
  converted (public FX is barred per `Docs/18`).
- **Fail-closed:** ingestion and the read endpoint enforce permissions before any read/return;
  ingestion refuses `LOCKED` months; storage errors propagate (no partial-success masking).
- **Idempotency:** re-running ingestion for a tenant+month upserts by `component_key` (stable
  output; no duplicate rows).

---

## 9. Validation gate

- `python -m ruff check backend tests scripts`
- `python -m pytest -q`
- `git diff --check`
- **Alembic Postgres round-trip is REQUIRED** (this spec adds a migration): run the new
  `*_postgres.py` migration/round-trip test with `UMS_TEST_DATABASE_URL` set against disposable
  `postgres:18-alpine`. SQLite is not a substitute for the migration round-trip.

---

## 10. Blast-radius review

- **Tables/ORM affected:** **new** `deduction_components` (additive); new `DeductionComponentORM`.
  No change to existing tables/columns. **Backward-compatible additive migration** — no
  destructive change, no backfill required.
- **PostgreSQL remains the source of truth.** No graph projection (Neo4j retired) — *No graph
  projection impact detected.*
- **Finance results:** `net_revenue` behavior changes **only** in the previously-missing-net case,
  and only via source-aligned CHANNEL components → distinct `COMPONENT_DERIVED`/`D_ESTIMATED`
  marking. The official-net path (source net present) is untouched → no double-count, no silent
  change to existing totals.
- **Authorization:** the read endpoint **reuses** existing permissions (no new permission, no
  weakening); requires all four, fail-closed. Ingestion reuses connector-job/finance write
  permissions + month-lock.
- **Audit:** the read endpoint reuses `REVENUE_VIEWED` / `PAYMENT_VIEWED` /
  `BANK_RECONCILIATION_VIEWED`; ingestion adds **one new** `AuditEventType.DEDUCTION_COMPONENTS_INGESTED`
  (sensitive, finance; summary counts only — no secrets). This is the only new AuditEventType.
- **Month locks / overrides:** ingestion respects the `LOCKED` gate like every other finance
  writer; manual overrides are unaffected.

---

## 11. Testing

- **Pure mapping** (`finance/deduction_components.py`): per-adapter mapping correctness; scope
  assignment (CHANNEL vs ACCOUNT vs PAYMENT); **signed** FX variance; `TRANSFER_FEE` ≥0; gap =
  settled − paid only when both present; `component_key` stability; USD-only skip+flag; `.to_api()`
  shape (no `raw_payload`).
- **Repository / ingestion** (`finance/deduction_ingestion.py`): idempotent upsert (re-run yields
  identical rows); `LOCKED`-month refusal; audit emitted; non-USD skip counted.
- **`net_revenue` wiring:** net-present → unchanged (components ignored for net, surfaced as
  context); net-missing → `COMPONENT_DERIVED`/`D_ESTIMATED` = gross − Σ(source-aligned channel
  components); cross-source components excluded; ACCOUNT/PAYMENT/fee/FX/gap never affect net.
- **Read endpoint:** auth (each of the four permissions missing → 403, fail-closed); scope-
  distinguished shape; no `raw_payload`; audit rows (`REVENUE_VIEWED` + conditional
  `PAYMENT_VIEWED`/`BANK_RECONCILIATION_VIEWED`); filters/pagination; malformed month → 422.
- **Migration:** Postgres round-trip (constraints, indexes, unique `(tenant_id, component_key)`).

---

## 12. Decomposition note

**Spec 2 (later):** allocation rules — distribute ACCOUNT/PAYMENT components (and any holding/
company-level deductions) down to channels, persist results (a `channel_net_revenue` table or
`ALLOCATION`-kind facts), and turn `/recalculate` into a committed, month-lock-guarded write.
Spec 2 consumes this spec's components as typed inputs. It additionally depends on the
still-missing channel↔account map (`Docs/01` Phase 3 note) and is intentionally excluded here.
