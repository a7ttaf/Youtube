# Track F — Smart Revenue Reconciliation Workflow (Design)

> Status: **approved for planning** — 2026-06-09.
> One combined backend effort. Covers the four "hard problems" reframed around
> the app's actual purpose: **automatically calculate and explain the shrinkage
> from YouTube's estimated revenue down to the money each channel actually
> receives**, across three hops (US tax → YouTube/AdSense transfer fee →
> AdSense/bank transfer fee + FX), attributed per channel.
> Builds on Track E (RLS, merged `6fea319`). References:
> `Docs/18_MULTI_CURRENCY_ENGINE.md`, `Docs/13`/finance specs.

---

## 1. Purpose and domain context

UMS is a corporate holding CMS: many companies → many channels. Each channel
earns revenue; UMS pays each out monthly. The problem: **no system (YouTube,
CMS, or AdSense) shows the breakdown of what is lost between YouTube's estimate
and the cash that lands in the bank.** Three reductions happen and are invisible:

1. **US tax** withheld on the US-views portion of earnings.
2. **YouTube → AdSense** transfer shrinkage (CMS estimated vs what reaches AdSense).
3. **AdSense → bank** transfer fee + FX (AdSense paid vs bank received).

AdSense reports only a **total** (channels only — no websites). So the aggregate
deltas must be **attributed back to each channel**, and the defensible basis is
each channel's **CMS estimated gross** (proportional) — exactly what the
committed-allocation engine already does.

**This spec builds a smart month-level workflow that derives all three
reductions from the actual figures (not operator-entered), attributes them per
channel, computes each channel's net received, and explains every number.**

The four originally-listed problems map in:
- **#3 + #4** (payment-gap + bank-variance narrative) = the core reconciliation
  workflow below.
- **#1** (outside-CMS revenue source) folds into the same engine (same API key
  serves CMS + outside-CMS + AdSense).
- **#2** (report retention) = manual delete-by-request (retain forever
  otherwise) — independent, §6.

---

## 2. What already exists (reuse — do NOT rebuild)

Verified against the merged tree (2026-06-09):
- `monthly_channel_revenue_facts` with `RevenueFactSourceKind` incl. `YOUTUBE_CMS`,
  `ADSENSE`, `ALLOCATION` (finance/revenue_facts.py). Holds per-channel gross.
- `deduction_components` table + `component_kind` (`TAX`, `DEDUCTION`,
  `TRANSFER_FEE`, `FX_VARIANCE`, `UNRESOLVED_PAYMENT_GAP`) + `scope_kind`
  (`CHANNEL`, `ACCOUNT`, `PAYMENT`) (finance/deduction_components.py).
- `number_explanations` with extensible metric + components JSON + confidence
  (finance/explanations.py; `SUPPORTED_METRICS` currently
  `adjusted_gross_revenue_usd`, `net_revenue_usd`).
- Committed-allocation engine: proportional account→channel distribution
  (finance/committed_allocation.py, `resolve_month_account_allocation`).
- `MonthlyPaymentMatchSummary` (`payment_gap_usd = youtube_total − adsense_paid`)
  and `MonthBankReconciliationSummary` (`bank_gap_usd`, `transfer_fee_usd`,
  `fx_difference_usd`) (finance/payment_matching.py, finance/bank_reconciliation.py).
- `youtube_channels.cms_status` (INSIDE_CMS/OUTSIDE_CMS/UNKNOWN) +
  `revenue_source_status` (OFFICIAL_CMS_REVENUE / OFFICIAL_MANUAL_IMPORT /
  ALLOCATED_FROM_PAYMENT_POOL / PERFORMANCE_ONLY / MISSING_REVENUE_SOURCE),
  read for UI flagging only (org_models.py, api/channels.py).
- `raw_report_files` with `parse_status` (DOWNLOADED/PARSED/FAILED/QUARANTINED);
  register/list/get only — no delete (report_models.py, reports/raw_files.py).

**No new tenant-scoped table is required.** (So the Track E RLS drift guard is
not touched.) The only schema change is additive columns/CHECK on
`raw_report_files` for §6.

---

## 3. The Smart Reconciliation Workflow (core)

A month-scoped engine `finance/reconciliation_workflow.py` that gathers inputs,
derives the three reductions, attributes them per channel, persists typed
components + explanations, and returns each channel's net received.

### 3.1 Inputs (all via the existing single-API ingestion)

| Input | Source today | Grain |
|---|---|---|
| CMS estimated gross | `monthly_channel_revenue_facts` source_kind=`YOUTUBE_CMS` | per channel |
| US-view revenue share | **new provider** (YouTube Analytics geography) — see §3.5 | per channel (fraction 0..1) |
| AdSense total received | `adsense_payments` (PAID) and/or account-level `google_revenue_source_rows` | account / month total |
| Bank received (+ currency) | `bank_reconciliation_entries` | month total |

### 3.2 Derivation (smart — back-calculated from real money)

For month `M`, tenant `T`, let `gross[c]` be each channel's CMS estimated gross,
`G = Σ gross[c]`.

**Hop 1 — US tax (per channel, separable):**
```
us_tax[c] = us_view_share[c] × gross[c] × withholding_rate
```
`withholding_rate` is the one configurable input (default documented in §3.5).
If `us_view_share[c]` is unavailable, `us_tax[c] = 0` and the component is
flagged `confidence=LOW, basis=MISSING_US_VIEW_DATA` (refine-later, §7).
`T_tax = Σ us_tax[c]`.

**Hop 2 — YouTube→AdSense transfer fee (residual, then attributed):**
```
adsense_received = AdSense total received for M
yt_adsense_fee_total = max(0, (G − T_tax) − adsense_received)
fee_pct = yt_adsense_fee_total / (G − T_tax)        # recorded for transparency
yt_adsense_fee[c] = yt_adsense_fee_total × (gross[c] / G)   # ∝ CMS gross
```
The engine **infers** the fee from the gap rather than taking a hand-entered
rate. The derived `fee_pct` is stored on each component for transparency.

**Hop 3 — AdSense→bank transfer fee + FX (residual, split):**
```
bank_received_usd = Σ bank_received_amount_usd for M
adsense_bank_delta = max(0, adsense_received − bank_received_usd)
fx_part   = Σ fx_difference_usd (from bank entries)         # currency movement
fee_part  = max(0, adsense_bank_delta − fx_part)            # remainder = fee
# attribute both ∝ gross
adsense_bank_fee[c] = fee_part × (gross[c] / G)
fx_variance[c]      = fx_part  × (gross[c] / G)
```

**Net received per channel:**
```
net_received[c] = gross[c] − us_tax[c] − yt_adsense_fee[c]
                  − adsense_bank_fee[c] − fx_variance[c]
```

All money is `Decimal`; rounding uses the existing finance quantization helper;
the residual that cannot be cleanly attributed (rounding remainder) lands on the
largest-gross channel so `Σ net_received[c]` reconciles exactly to
`bank_received_usd` (documented invariant + test).

### 3.3 Persistence

- **`deduction_components`** (idempotent upsert, month-scoped): one row per
  (channel, component_kind) for `TAX`, `TRANSFER_FEE` (hop 2), `TRANSFER_FEE`
  (hop 3 — distinguished by `component_key`), `FX_VARIANCE`. Each row carries
  `amount_usd`, the derived percentage in its metadata/`component_key`,
  `source_table="reconciliation_workflow"`, `source_system`, provenance. Written
  on the `app_tenant` lane (existing write surface).
- **`number_explanations`** new metric `revenue_reconciliation` per
  (channel, month): components array chaining estimated → −tax → −yt/adsense fee
  → −adsense/bank fee+fx → net_received, each with value + derived pct + source,
  a `confidence` block, warnings for missing inputs, and a **deterministic
  prose** `narrative` string (template over the components — NO LLM). Persisted
  via the existing `record_explanation` path; add `revenue_reconciliation` to
  `SUPPORTED_METRICS`.

### 3.4 Workflow API

`POST /revenue/months/{month}/reconcile` — permission-gated (finance compute
permission; confirm exact `Permission` against existing finance-write routes),
audited (`REVENUE_RECONCILED` audit event, reusing the audit sink), tenant-scoped.
- Idempotent: recomputing a non-locked month replaces that month's
  reconciliation components + explanations.
- Lock-aware: if `finance_month_close` for `M` is LOCKED, reject recompute
  (409) — read endpoints still serve the committed result.
- Returns `{month, channels:[{channel_id, gross, us_tax, yt_adsense_fee,
  adsense_bank_fee, fx_variance, net_received, confidence}], totals, warnings}`.

`GET /revenue/channels/{channel_id}/months/{month}/reconciliation` — read the
persisted explanation (finance read permission; raw provenance redaction
consistent with existing explain reads).

### 3.5 Refine-later inputs (explicitly deferred per operator)

- **US-view share provider**: structure a `UsViewShareProvider` interface now;
  the initial impl reads YouTube Analytics geography if present, else returns
  `None` → tax flagged LOW/MISSING. The real geography feed + exact
  `withholding_rate` (US treaty rate) are tuned with real statements.
- **Multi-API-key / 100-channel ingestion scaling** — out of scope now.
- Precise fee/FX split rules refined when real AdSense/bank statements arrive.

---

## 4. Outside-CMS channels (#1) — folds into the engine

- A channel with `cms_status=OUTSIDE_CMS` that **has** CMS gross is treated
  identically (gross is the basis).
- A channel with no CMS gross whose AdSense account maps **1:1** to it (verified
  links): the account total is its gross basis → a `monthly_channel_revenue_facts`
  row is written with `source_kind=ALLOCATION`; its per-month source is
  `ALLOCATED_FROM_PAYMENT_POOL`.
- Account mapping to **many** no-CMS-gross channels with no per-channel basis:
  **fail closed** — no fabricated number; channel stays `MISSING_REVENUE_SOURCE`
  and is surfaced in warnings. (Refine-later: a performance-based split basis.)
- **Per-month revenue source status is derived** from the fact's `source_kind`
  (no new table; `youtube_channels.revenue_source_status` stays the channel-level
  default/UI flag).

---

## 5. Data flow

```text
POST /revenue/months/{M}/reconcile
  → permission gate + tenant context (RLS active)
  → gather: CMS gross facts, us-view shares, AdSense total, bank received
  → derive hop1 (tax), hop2 (yt→adsense residual), hop3 (adsense→bank fee+fx)
  → attribute aggregates ∝ CMS gross; reconcile rounding remainder
  → upsert deduction_components (per channel, typed, derived pct)
  → record number_explanations 'revenue_reconciliation' (+ prose) per channel
  → audit REVENUE_RECONCILED
  → return per-channel net_received + totals + warnings
```

---

## 6. Report retention = manual delete only (#2)

- **Retain forever.** No expiry, no scheduler, no policy table.
- Migration (additive, on existing `raw_report_files`): add `PURGED` to the
  `parse_status` CHECK; add `purged_at timestamptz NULL`, `purged_by uuid NULL`.
  (Existing tenant table — no drift-guard impact.)
- `DELETE /reports/raw-files/{id}` — authorized (admin/connector permission;
  confirm exact Permission), **reason required**, audited (`REPORT_PURGED`).
  Clears the stored blob/`file_url` reference, sets `parse_status=PURGED`,
  `purged_at`/`purged_by`. **Keeps the metadata row** (month, source, checksum,
  who/when) for audit. Idempotent (purging an already-PURGED row is a no-op 200
  or 409 — pick 409 conflict; documented).
- Repository gains `purge_file(...)`; reads exclude/annotate PURGED as needed.

---

## 7. Error handling, confidence, fail-closed

- Missing AdSense total or bank data → compute the hops that are possible, set
  the rest to 0 with `warnings` + LOW confidence; never fabricate.
- Negative residuals (e.g., AdSense received > CMS estimated) → clamp to 0 and
  emit a `RECONCILIATION_ANOMALY` warning (do not produce negative fees).
- Locked month → reject recompute (409); serve committed read.
- All typed domain errors translate to HTTPException at the route boundary; no
  secrets/PII in messages; fail-closed auth (RLS + permission gate).
- Confidence: tax HIGH only when real US-view data present (else LOW); derived
  transfer fees MEDIUM (residual-inferred); FX from recorded bank deltas.

---

## 8. Blast radius (CLAUDE.md required answers)

- **Tables/ORM:** writes `deduction_components`, `number_explanations`,
  possibly `monthly_channel_revenue_facts` (source_kind=ALLOCATION for 1:1
  outside-CMS); additive columns on `raw_report_files`. No new table.
- **PostgreSQL source of truth:** yes. RLS (Track E) applies to all writes.
- **Migrations/tests/seed break?** Only additive `raw_report_files` change +
  new metric in `SUPPORTED_METRICS`; existing explanations unaffected.
- **Neo4j:** No graph projection impact detected (Neo4j retired).
- **Authz/audit more permissive?** No — new gated compute + delete routes, both
  audited; strictly additive permissions.
- **Finance results change?** Yes by design — this *computes* net received +
  deductions where none existed; all derived numbers carry source/formula/
  confidence and are reproducible from inputs.
- **Backward compatible / destructive?** Additive. Purge is the only destructive
  action and is manual, audited, reason-required, metadata-preserving.
- **Rollback/reset note?** Reconciliation is recomputable (idempotent) for
  unlocked months; the `raw_report_files` migration is additive (reversible).

Statement: **`No graph projection impact detected.`**

---

## 9. Testing contract

- **Derivation math:** per-channel tax/fee/fx with known inputs; residual
  inference of YT→AdSense fee; `Σ net_received` reconciles exactly to bank
  received (rounding-remainder invariant).
- **Attribution:** aggregate deltas split ∝ CMS gross; zero-gross channels get
  zero; single-channel month = full passthrough.
- **Fail-closed:** missing AdSense/bank/us-view → warnings + LOW confidence, no
  fabrication; negative residual clamped + anomaly warning; locked month → 409.
- **Persistence:** deduction_components typed rows + derived pct; explanation
  metric + deterministic prose; idempotent recompute replaces prior month rows.
- **Outside-CMS:** 1:1 passthrough writes ALLOCATION fact; 1:many no-basis fails
  closed to MISSING_REVENUE_SOURCE with warning.
- **Permissions/tenant:** reconcile + read + purge gated; cross-tenant denied
  (RLS); purge cross-tenant id → 404.
- **Retention:** purge marks PURGED + clears blob + keeps metadata + audits +
  requires reason; re-purge → 409.
- **Full gate** against the Postgres container (RLS active): ruff, diff-check,
  full pytest.

---

## 10. Docs to update with implementation

- `Docs/18` — record the smart reconciliation workflow + the three derived
  reductions; note US-view-data + withholding-rate as refinement inputs.
- `Docs/12_BACKEND_API_SPEC.md` — `POST /revenue/months/{month}/reconcile`,
  `GET .../reconciliation`, `DELETE /reports/raw-files/{id}`.
- `Docs/01_IMPLEMENTATION_PLAN.md` + `Docs/15_DELIVERY_BACKLOG.md` — Track F
  status: reconciliation workflow + outside-CMS attribution + manual report
  delete DONE; US-view feed + rate + ingestion scaling = documented refine-later.

---

## 11. Out of scope (explicit)

- Multi-API-key / >100-channel ingestion scaling.
- Real US-view geography feed + final withholding-rate calibration (interface
  built; data deferred).
- Any new tenant-scoped table; any FX-rate provider workflow (Docs/18 forbids).
- Performance-based split basis for 1:many outside-CMS with no CMS gross.
