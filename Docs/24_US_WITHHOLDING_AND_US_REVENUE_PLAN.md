# Docs/24 — US Revenue Visibility & US Withholding Tax

Status: PLAN (no code in this PR). Owner question, verbatim: *"The project can see the views
as per API right, US cuts some tax 16% i think check it, i need some think that show this
thing but i need you check first the percentage of US tax for US views, also add this
calculation"* and *"the project can see how much revinue from us per channel right?"*

This document records the verified facts first (tax rules and what the codebase actually
ingests today), then the program that adds per-channel US revenue and the withholding
calculation as display/evidence surfaces. Gap-patched 2026-08-30 (triad/status review).

### Related plans (program bundle)

| Doc / where | Role |
| --- | --- |
| [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md) / [`21_BETA_IMPLEMENTATION_PLAN.md`](21_BETA_IMPLEMENTATION_PLAN.md) | Parent beta audit; P3 parked recon TAX (living status on P0 split PRs) |
| [`25_PROGRAM_DEPENDENCY_GRAPH.md`](25_PROGRAM_DEPENDENCY_GRAPH.md) | Execution DAG |
| P0 split PRs (P0-a…P0-e; #221–#225) on `main` | Living P0 implementation; supersedes historical #210 |
| [`23_ADMIN_ACCESS_AND_CONFIG_PLAN.md`](23_ADMIN_ACCESS_AND_CONFIG_PLAN.md) | Sibling admin program (independent) |

> ⚠️ **D-U1 is blocking.** No estimate surfaces until the operator confirms the live
> AdSense tax-info rate and records it in effective-dated config. **No default rate.**
>
> **Consolidation:** Docs/20/21/23/24/25 ship together in `docs/program-plans-consolidated`
> (supersedes closed drafts #209 / #218 / #219).

---

## 0. The rate ruling — it is 15%, not 16%

**The verified chain:**

1. Google withholds US tax **only on earnings from US viewers**, and only applies a reduced
   rate when a valid tax form (W-8BEN-E for a business) is on file in AdSense. Withheld
   amounts surface in the AdSense **payments transactions report**, usually the month after
   the payment. (Google: "US tax requirements for YouTube earnings",
   support.google.com/youtube/answer/10391362)
2. The Egypt–United States income tax treaty caps royalty withholding at source. The IRS
   treaty table row for Egypt reads **NA / 30 / 15 / NA / 15** across the five royalty
   categories: industrial equipment NA, know-how **30%**, patent **15%**, motion
   picture/TV **NA (no treaty reduction → default 30%)**, **copyright 15%**.
   (irs.gov/pub/irs-trty/egypt.pdf; PwC treaty withholding tables.)
3. YouTube/AdSense payments claim treaty benefit under the **copyright royalties** category
   in the AdSense tax-info form. For a properly filed Egyptian entity the applicable rate is
   therefore **15% of US-viewer earnings**.

**The traps (why "check first" was the right instinct):**

- **Business account, no valid form on file** → default withholding is **30% on
  US-source earnings only** (not worldwide).
- **Individual account, no valid form** → backup withholding is **24% on total
  worldwide earnings**.
- **Lapsed/expired form** falls back to the same account-type default. The form has
  to be re-validated when Google asks.
- If a payment were classed under **motion picture/TV royalties**, Egypt's treaty gives NO
  reduction (30%). The AdSense form shows which categories were claimed and at what rate.

**Operator action (only Mahmoud can do this):** AdSense → Payments → Manage settings →
"United States tax info" shows the exact per-category withholding rate Google is applying
to this account right now. That number — not this document — is the rate the calculation
must be configured with. For a properly filed Egyptian **business** entity under copyright
royalties, the **expected** treaty rate is **15% of US-viewer earnings** — but only after
D-U1 confirms the live page matches.

**Honesty rule for everything below:** YouTube Analytics revenue metrics
(`estimatedRevenue`) are **pre-withholding**. Any withholding figure UMS computes from
Analytics data is an **estimate** (`US revenue × configured rate`). The actual withheld
amount exists only in the AdSense payments transactions report. Every UMS surface built by
this program labels the computed number "estimated withholding" and never mixes it into
net finance math.

---

## 1. Verified inventory — what exists today (checked in code, 2026-08-28)

| # | Fact | Where verified |
|---|------|----------------|
| F1 | The YouTube Analytics lane queries one channel-month at a time: `ids=contentOwner==…`, `filters=channel==<id>`, `dimensions=month` — deliberately month-only on the wire. **No country dimension is fetched today.** | `connectors/google/youtube_analytics_client.py` (`_DIMENSIONS = "month"`, `_build_query_request`) |
| F2 | The YouTube Reporting (CSV) lane whitelists `content_owner_estimated_revenue_a1`, whose **daily rows already include `country_code`**. The adapter **deliberately drops** country (and video/day) when folding rows into monthly channel totals, to keep one source_row_key per channel-month. Raw CSV evidence is persisted before parsing. | `connectors/google/report_type_whitelist.py`; `connectors/runs/orchestrator.py` `_accumulate_csv_row` ("Lower-level official dimensions (video_id, country_code, etc.) are deliberately NOT forwarded") |
| F3 | The AdSense payments lane is `accounts.payments.list` only — unpaid/scheduled **payment totals, no transaction line items**. **No withholding amount is ingested anywhere in UMS.** The Management API v2 does not expose the transactions detail; the withheld line lives in the AdSense UI report. | `connectors/google/adsense_payments_client.py`; `finance/adsense_payments.py` |
| F4 | The YouTube Analytics API **does** support the needed shape: content-owner "user activity by country" report = `dimensions=country` + revenue metrics (`estimatedRevenue`) + exactly one `filters=channel==UC…`. Same per-channel loop as the 54-channel workbook (contentOwner queries still don't support `dimensions=channel`). | developers.google.com/youtube/analytics/content_owner_reports |
| F5 | `source_row_key` hashes report_type/period/**dimensions**/currency, so country-dimensioned rows get distinct keys and cannot collide at upsert. That key distinction alone does **not** prevent double projection: canonical selection still groups by channel/source and could choose a country row. The U2 non-projecting guard is what keeps country evidence out of channel-month totals. This is also why EGP sequencing matters (currency is in the hash). | `connectors/google_source_parsers/source_row_keys.py`; `finance/google_source_normalizer.py` |
| F6 | **Existing recon already models a US-tax hop at 0.30.** `DEFAULT_US_WITHHOLDING_RATE = 0.30` in `finance/reconciliation_workflow.py` feeds a `us_view_share × gross × rate` component into net math. Today it is **dormant** (`NullUsViewShareProvider` → tax ≈ 0). Docs/21 P3 already says do not arm recon-derived TAX until the rate is known. | `finance/reconciliation_workflow.py`; Docs/21 P3; Docs/15 refine-later “withholding-rate calibration” |

**Direct answers to the operator's questions:**

- *"The project can see the views as per API right?"* — Yes; the Analytics lane ingests
  per-channel monthly metrics via the API today.
- *"Can the project see how much revenue from US per channel?"* — **Not yet.** The data is
  reachable two ways (F2: it's already inside the persisted CSV evidence; F4: a documented
  API query fetches it directly), but nothing ingests, stores, or displays a US split
  today. That is exactly what bands U1–U3 add.

**Fence on F6 (non-negotiable for this program):** U1–U4 do **not** rename, replace, or
silently sync with `DEFAULT_US_WITHHOLDING_RATE`. The recon path stays dormant and out of
scope. U3’s **`UMS_US_WITHHOLDING_RATE` has no default** — unset means suppress all
estimate UI. Arming recon / `UsViewShareProvider` requires a **separate** finance
ruling — not this plan.

---

## 2. The program — bands U1–U4

Sequenced after the current fleet (#211–#217) and independent of the admin program
([`23_ADMIN_ACCESS_AND_CONFIG_PLAN.md`](23_ADMIN_ACCESS_AND_CONFIG_PLAN.md)). Parent
context: Docs/21 (this PR snapshot; living status on P0 split PRs) P3
“Reconciliation-derived TAX” pause. U2 must coordinate with the EGP program because both
touch source-row identity.

### U1 — Probe: prove the numbers before building (2–4h, read-only script)

A standalone script (same shape as the EGP workbook reference script) that, for one month:
per channel, runs `filters=channel==UC…` + `dimensions=country` + `metrics=estimatedRevenue`,
extracts the `US` row, and prints channel → US revenue → US share % → estimated
withholding **only if** an operator-supplied rate is passed on the CLI (no baked-in
default). Acceptance: the per-channel **worldwide totals** in the same responses must
equal the existing workbook/UMS numbers channel-by-channel (proves the country cut
decomposes the number we already trust, no drift). No writes, no UMS changes.

**Acceptance criteria (U1):**
- [ ] Script runs read-only against live or fixture Analytics responses
- [ ] Per-channel worldwide totals match existing UMS/workbook numbers
- [ ] US row extracted; no UMS database writes

### U2 — Ingest the US slice (10–16h) — **blocked until normalization fence**

The PR227 contract keeps country evidence on the existing allowlisted
`source_system="youtube_analytics"`; do not invent a second source-system value.
The parser owns the dimension shape by emitting `raw_payload.dimensions` with a
`country` key (alongside `channel`). Before `_source_row_buckets` groups rows, the
normalizer detects that exact parser-owned shape, appends
`SkippedSourceRow(..., SkipReason.NON_PROJECTING_EVIDENCE)` to `result.skipped`, and
does not add the row to any canonical bucket. The existing `youtube_analytics` rows
without a country key (including worldwide rows) remain in the normal bucket.

Country-dimensional rows remain persisted in the source-row evidence table; the
explicit `NON_PROJECTING_EVIDENCE` skip reaches the normal `ROWS_SKIPPED` audit
summary. No new source-system allowlist, migration, or key namespace is required.
Any future evidence discriminator would need its full migration, parser/source
allowlist, source-row-key, and audit-contract changes before it could be proposed.
The existing channel-month canonical lane is untouched.

**Acceptance criteria (U2):**
- [ ] Parser emits country rows with `source_system="youtube_analytics"` and parser-owned `raw_payload.dimensions.country`
- [ ] Country rows persist as evidence with distinct keys (F5), then are skipped with `NON_PROJECTING_EVIDENCE`
- [ ] `ROWS_SKIPPED` audit telemetry includes the explicit non-projecting reason
- [ ] `GoogleSourceNormalizer` never selects country-dimensional rows for canonical facts; removing the pre-bucket guard turns the guard test RED
- [ ] Full PG suite green; RLS-scoped reads

**Sequencing tripwire:** if the EGP flip (currency change, which re-keys source rows) is
scheduled, land U2 either clearly before or clearly after — never interleaved with — the
EGP re-key, so evidence rows don't split across two currency generations mid-month.

### U3 — Display + the calculation (6–10h) — **blocked until D-U1 + config service**

Per-channel panel/column: **US revenue**, **US share of channel revenue**, and
**estimated US withholding** — **only when** an effective-dated, operator-confirmed rate
exists in config. **`UMS_US_WITHHOLDING_RATE` unset → suppress estimate UI entirely**
(fail-closed; no silent 15% default). Rate validated 0 ≤ rate ≤ 0.30 when set.

Every figure is labeled "estimated"; the panel links the §0 explanation.

**Backend-only estimate (AGENTS / finance rule):** the backend emits the labeled estimate
fields (US revenue, share, estimated withholding, rate used, confidence/source tokens).
The SPA **renders** those fields — it must **not** compute `US × rate` in the browser.
Display only: nothing feeds net revenue, allocation, close, or reconciliation (F6 fence).

**Acceptance criteria (U3):**
- [ ] With no confirmed rate in config, estimate fields absent from API (not zero)
- [ ] With confirmed rate, backend returns labeled estimate fields only
- [ ] SPA renders backend fields; no client-side withholding math
- [ ] Recon `DEFAULT_US_WITHHOLDING_RATE` (0.30) unchanged and dormant

### U4 — Actual-withholding anchor (4–8h, optional, operator decision)

The actual withheld amount is UI-only (F3), so U4 adds a small manual-entry surface (or
CSV drop) for the per-payment withheld line from the AdSense payments transactions report,
plus a delta view: estimated (U3) vs actual. This is the only way to catch a silently
lapsed W-8 (actual jumps to 24/30% while the estimate stays 15%). Until U4, that check is
a monthly manual glance at the AdSense payments report.

---

## 3. Tripwires

- **T-U1** The withholding figure is an estimate and is always labeled as one; it never
  enters net finance math, close, allocation, or reconciliation without its own ruling.
  In particular: do **not** wire U2/U3 into `UsViewShareProvider` or change
  `DEFAULT_US_WITHHOLDING_RATE` (0.30) from this program (F6).
- **T-U2** The display-estimate rate is **effective-dated configuration with no default**.
  `UMS_US_WITHHOLDING_RATE` unset → no estimate surfaces. It is **not** a silent rename of
  the recon constant (0.30).
- **T-U3** Country-dimensioned rows never feed the monthly-totals lane (no double count);
  enforced by the U2 non-projecting guard. Distinct source_row_key dimensions (F5)
  prevent upsert collisions only; they are not a projection guard.
- **T-U4** No FX anywhere in this program (standing rule); US revenue is displayed in the
  source currency of the row, full stop.
- **T-U5** U2 respects EGP-program sequencing (currency is inside source_row_key).
- **T-U6** Official estimate math lives in the backend; the SPA does not calculate
  withholding locally.

## 4. Operator decisions

- **D-U1 (blocking U3):** Read the actual per-category rate from AdSense → Payments →
  "United States tax info"; record account type (business vs individual) and confirmed
  rate in effective-dated config. If the page shows 24%/30% fallback, stop and fix the
  tax form before enabling estimates. **No estimate UI until D-U1 is recorded.**
- **D-U2:** Where the US panel lives (Rankings view column vs. per-channel detail panel) —
  design is the operator's per the standing rule; the plan only commits the numbers.
- **D-U3:** Whether U4 (manual actual-withholding anchor) is worth building now or stays a
  monthly manual check in the AdSense UI.
- **D-U4:** Store full country breakdown in U2 (recommended, same cost) or the US row only.
