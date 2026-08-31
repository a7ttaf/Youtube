# Docs/24 — US Revenue Visibility & US Withholding Tax

Status: PLAN (no code in this PR). Owner question, verbatim: *"The project can see the views
as per API right, US cuts some tax 16% i think check it, i need some think that show this
thing but i need you check first the percentage of US tax for US views, also add this
calculation"* and *"the project can see how much revinue from us per channel right?"*

This document records the verified facts first (tax rules and what the codebase actually
ingests today), then the program that adds per-channel US revenue and the withholding
calculation as display/evidence surfaces. Gap/status-patched 2026-08-31.

### Related plans (program bundle)

| Doc / where | Role |
| --- | --- |
| [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md) / [`21_BETA_IMPLEMENTATION_PLAN.md`](21_BETA_IMPLEMENTATION_PLAN.md) | Parent audit + frozen costing snapshot; P3 parks recon TAX |
| [`25_PROGRAM_DEPENDENCY_GRAPH.md`](25_PROGRAM_DEPENDENCY_GRAPH.md) | Execution DAG |
| P0-a…P0-e / #221–#225 | Open drafts, not merged; only current main-targeted P0 successors |
| [`23_ADMIN_ACCESS_AND_CONFIG_PLAN.md`](23_ADMIN_ACCESS_AND_CONFIG_PLAN.md) | Sibling admin program (independent) |

> ⚠️ **D-U1 is blocking.** No estimate surfaces until the operator confirms the live
> AdSense tax-info rate and records it in effective-dated config. **No default rate.**
>
> **Consolidation:** Docs/20/21/23/24/25 ship together in PR #220 / `docs/program-plans-consolidated`
> (supersedes closed drafts #209 / #218 / #219).

---

## 0. The classification/rate ruling — the repository must not choose one

**The verified chain:**

1. [Google's U.S. tax requirements](https://support.google.com/youtube/answer/10391362)
   say withholding is determined from submitted tax information and that the applied rate
   is visible under AdSense **Manage tax info**. The repository does not determine that
   account-specific category or rate.
2. [IRS Treaty Table 1](https://www.irs.gov/pub/irs-lbi/tax-treaty-table-1.pdf)
   reports a rate for qualifying Egypt copyright royalties but marks film/TV `n/a`.
   `n/a` is not an automatic 30% treaty rate and must not be translated into one.
3. The [Egypt treaty](https://www.irs.gov/pub/irs-trty/egypt.pdf) and its
   [IRS technical explanation](https://www.irs.gov/pub/irs-trty/egypttech.pdf) exclude
   motion-picture films and radio/television films or tapes from the treaty's royalty
   definition and route that classification to the business-profits article. The actual
   treatment therefore depends on the account's facts and classification, not a hardcoded
   film/TV fallback.
4. [Google's tax-information guidance](https://support.google.com/youtube/answer/10390801)
   directs the payee through the account-specific tax tool and recommends professional tax
   advice. UMS records the resulting AdSense category/rate; it does not issue the ruling.

**The traps (why "check first" was the right instinct):**

- Google's guidance describes generic no-valid-form/default-withholding outcomes that can
  reach 30% of U.S.-source business earnings or 24% of worldwide individual earnings.
  Those defaults are not an interpretation of the treaty table's film/TV `n/a` entry and
  are never the repository's configured treaty rate.
- Lapsed, incomplete, or changed tax information can change the displayed result. The
  effective-dated record must be refreshed from AdSense when Google requests re-validation.

**Operator action (only Mahmoud can do this):** AdSense → Payments → Manage settings →
"United States tax info" shows the exact per-category withholding rate Google is applying
to this account right now. That displayed category and rate — not this document and not an
interpretation of `n/a` — are what D-U1 records for the calculation. If the classification
is unclear, consult a qualified tax professional before enabling the estimate.

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
| F5 | `source_row_key` hashes report_type/period/**dimensions**/currency, so country-dimensioned rows get distinct keys and cannot collide at upsert. That distinction alone does **not** prevent double projection: canonical selection still groups by channel/source and could choose a country row. The U2 non-projecting guard is what keeps country evidence out of channel-month totals. This is also why EGP sequencing matters (currency is in the hash). | `connectors/google_source_parsers/source_row_keys.py`; `finance/google_source_normalizer.py` |
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
scope. U3 reads an effective-dated PostgreSQL configuration row; **no matching row means
suppress all estimate UI**. An environment scalar is not the configuration model and
no treaty rate is hardcoded. Arming recon / `UsViewShareProvider` requires a
**separate** finance ruling — not this plan.

---

## 2. The program — bands U1–U4

Sequenced after the current P1 fleet (#211 merged; #212 open/non-draft/BLOCKED;
#213–#216 open drafts) and coordinated with separate EGP Phase 1 draft/BLOCKED #217.
The finance program is independent
of the admin program
([`23_ADMIN_ACCESS_AND_CONFIG_PLAN.md`](23_ADMIN_ACCESS_AND_CONFIG_PLAN.md)). Parent
context: Docs/21 (frozen costing snapshot; Docs/25 + live GitHub own execution status) P3
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

**Required contract for pending PR #227:** U2 remains **blocked**. The captured #227
head `e174c51f` requires redesign: it does **not** satisfy the typed non-projecting fence
while preserving the allowlisted source-system contract. A redesigned successor must
implement and test that behavior before merge. Even then, the fence is not U2 country
ingestion and must not be marked as that feature.

Country evidence stays on the allowlisted `source_system="youtube_analytics"`; do not
invent a second source-system value without the full migration, parser/source allowlist,
source-row-key, and audit-contract changes. The parser owns the dimension shape by
emitting `raw_payload.dimensions` with a `country` key (alongside `channel`). Before
[`GoogleSourceNormalizer._source_row_buckets`](../backend/ums_smart_revenue/finance/google_source_normalizer.py)
groups by `(channel_id, source_system)`, the normalizer must detect that exact
parser-owned shape, append a typed `NON_PROJECTING_EVIDENCE` skip, and not add the row
to a canonical bucket. Existing worldwide `youtube_analytics` rows without a country
key stay in the normal bucket and remain eligible for
[`select_canonical_row`](../backend/ums_smart_revenue/finance/google_source_normalizer.py).

Country rows remain persisted in source-row evidence with their distinct keys. The
intentional skip remains auditable, but alert aggregation must exclude it from the
generic defect count (or emit a separate informational lifecycle); a healthy U2 run
must not produce a HIGH `SOURCE_ROWS_SKIPPED` alert merely because evidence was fenced.

**Acceptance criteria (U2):**
- [ ] A redesigned #227 successor satisfying the typed-fence/source constraint is
  reviewed and merged before U2; current head `e174c51f` does not satisfy this gate
- [ ] Parser emits country rows with `source_system="youtube_analytics"` and
  parser-owned `raw_payload.dimensions.country`
- [ ] Country rows persist as evidence with distinct keys (F5), then receive the typed
  non-projecting skip before canonical bucketing
- [ ] Intentional evidence skips remain in audit telemetry but do not generate HIGH
  missing-data alerts; true malformed/failed skips still alert
- [ ] `GoogleSourceNormalizer` never selects country-dimensional rows for canonical facts;
  removing the pre-bucket guard turns a test RED
- [ ] Full PG suite green; RLS-scoped reads

**Sequencing tripwire:** if the EGP flip (currency change, which re-keys source rows) is
scheduled, land U2 either clearly before or clearly after — never interleaved with — the
EGP re-key, so evidence rows don't split across two currency generations mid-month.

### U3 — Display + the calculation (6–10h) — **blocked until D-U1 + config service**

Per-channel panel/column: **US revenue**, **US share of channel revenue**, and
**estimated US withholding** — **only when** an effective-dated, operator-confirmed rate
exists in PostgreSQL for the revenue month (and payment account when multiple accounts
can differ). **No matching row → suppress estimate fields entirely** (fail-closed; no
silent treaty-rate default). Rate validated 0 ≤ rate ≤ 0.30 when recorded. PR #228 currently
provides only an open ORM/repository scaffold and has an Alembic-head collision with
#223; it must be restacked after #223 and integrated behind an audited service/API before
U3 can be called implemented.

Every figure is labeled "estimated"; the panel links the §0 explanation.

**Backend-only estimate (AGENTS / finance rule):** the backend emits the labeled estimate
fields (US revenue, share, estimated withholding, rate used, confidence/source tokens).
The SPA **renders** those fields — it must **not** compute `US × rate` in the browser.
Display only: nothing feeds net revenue, allocation, close, or reconciliation (F6 fence).

**Acceptance criteria (U3):**
- [ ] PostgreSQL stores append-only/effective-dated operator confirmations with actor,
  source/account context, and month-based lookup; no environment scalar is authoritative
- [ ] With no confirmed rate row for the month/account, estimate fields absent from API
- [ ] With confirmed rate, backend returns labeled estimate fields only
- [ ] Zero worldwide revenue returns `share=null` with typed reason
  `ZERO_WORLDWIDE_REVENUE`; missing worldwide evidence returns `share=null` with a
  distinct missing-data reason—neither path divides by zero or reports 0% as fact
- [ ] SPA renders backend fields; no client-side withholding math
- [ ] Recon `DEFAULT_US_WITHHOLDING_RATE` (0.30) unchanged and dormant

### U4 — Actual-withholding anchor (4–8h, optional, operator decision)

The actual withheld amount is UI-only (F3), so U4 adds a typed PostgreSQL record and a
small manual-entry surface (or CSV drop) for the per-payment withheld line from the
AdSense payments transactions report, plus a delta view: estimated (U3) vs actual. The
record carries tenant, AdSense/payment account, payment identifier/month, amount,
currency, source-report reference, actor, required reason, and timestamps. Corrections
are append-only and link to the superseded record; no silent overwrite. Record + audit
commit atomically, and locked/closed-period handling is explicit. Until U4, this remains
a monthly manual glance at the AdSense report—no durable anchor exists yet.

---

## 3. Tripwires

- **T-U1** The withholding figure is an estimate and is always labeled as one; it never
  enters net finance math, close, allocation, or reconciliation without its own ruling.
  In particular: do **not** wire U2/U3 into `UsViewShareProvider` or change
  `DEFAULT_US_WITHHOLDING_RATE` (0.30) from this program (F6).
- **T-U2** The display-estimate rate is **effective-dated configuration with no default**.
  No matching PostgreSQL row → no estimate surfaces. It is **not** an environment scalar
  or a silent rename of the recon constant (0.30).
- **T-U3** Country-dimensioned rows never feed the monthly-totals lane (no double count);
  enforced by the U2 non-projecting guard. Distinct source-row keys (F5) prevent upsert
  collisions only; they are not a projection guard.
- **T-U4** No FX anywhere in this program (standing rule); US revenue is displayed in the
  source currency of the row, full stop.
- **T-U5** U2 respects EGP-program sequencing (currency is inside source_row_key).
- **T-U6** Official estimate math lives in the backend; the SPA does not calculate
  withholding locally.

## 4. Operator decisions

- **D-U1 (blocking U3):** Read the actual category/rate from AdSense → Payments →
  "United States tax info"; record account type and confirmed result in effective-dated
  config. If the page shows a fallback or the classification is unclear, review the tax
  information with Google and a qualified adviser before enabling estimates. **No
  estimate UI until D-U1 is recorded.**
- **D-U2:** Where the US panel lives (Rankings view column vs. per-channel detail panel) —
  design is the operator's per the standing rule; the plan only commits the numbers.
- **D-U3:** Whether U4 (manual actual-withholding anchor) is worth building now or stays a
  monthly manual check in the AdSense UI.
- **D-U4:** Store full country breakdown in U2 (recommended, same cost) or the US row only.
