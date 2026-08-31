# Docs/24 — US Revenue Visibility & US Withholding Tax

Status: PLAN (no code in this PR). Owner question, verbatim: *"The project can see the views
as per API right, US cuts some tax 16% i think check it, i need some think that show this
thing but i need you check first the percentage of US tax for US views, also add this
calculation"* and *"the project can see how much revinue from us per channel right?"*

This document records the verified facts first (tax rules and what the codebase actually
ingests today), then the program that adds per-channel US revenue and the withholding
calculation as display/evidence surfaces. Gap-patched 2026-08-31 (live-thread recertification).

### Related plans (program bundle)

| Doc / where | Role |
| --- | --- |
| [`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md) / [`21_BETA_IMPLEMENTATION_PLAN.md`](21_BETA_IMPLEMENTATION_PLAN.md) | Parent beta audit; P3 parked recon TAX (living status on P0 split PRs) |
| [`25_PROGRAM_DEPENDENCY_GRAPH.md`](25_PROGRAM_DEPENDENCY_GRAPH.md) | Execution DAG |
| P0 split PRs (P0-a…P0-e; #221–#225) on `main` | Living P0 implementation; supersedes historical #210 |
| [`23_ADMIN_ACCESS_AND_CONFIG_PLAN.md`](23_ADMIN_ACCESS_AND_CONFIG_PLAN.md) | Sibling admin program (independent) |

> ⚠️ **D-U1 is blocking.** No estimate surfaces until the operator confirms the live
> AdSense tax-info rate and records it in PostgreSQL-backed, effective-dated config.
> **No environment fallback and no default rate.**
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
scope. U3 reads only the PostgreSQL effective-dated rate record described below; an
environment scalar is not a historical configuration model. No matching row means
suppress all estimate UI. Arming recon / `UsViewShareProvider` requires a **separate**
finance ruling — not this plan.

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

**Required contract for pending PR #227 (final SHA not yet supplied):** U2 remains
**blocked**. The current PR is an implementation candidate, not shipped evidence.
At the 2026-08-31 recertification it still has an unresolved P1 and unpushed changes;
neither the remote intermediate head nor local-only bytes satisfy this gate.
Only after the operator supplies the final reviewed SHA may this paragraph be changed
to identify that exact SHA as satisfying the contract, and only if the focused tests
and alert behavior below are verified at those bytes. Do not infer completion from an
open PR title, intermediate head, or the old separate-source implementation.

The cleanup must keep country evidence on the existing allowlisted
`source_system="youtube_analytics"`; it must not invent a second source-system
value. The parser must own the dimension shape by emitting
`raw_payload.dimensions` with a `country` key (alongside `channel`). Before
`_source_row_buckets` groups rows, the normalizer must detect that exact
parser-owned shape, append
`SkippedSourceRow(..., SkipReason.NON_PROJECTING_EVIDENCE)` to `result.skipped`,
and not add the row to any canonical bucket. Existing `youtube_analytics` rows
without a country key (including worldwide rows) must remain in the normal bucket.

Country-dimensional rows must remain persisted in the source-row evidence table;
the explicit `NON_PROJECTING_EVIDENCE` skip must reach the normal `ROWS_SKIPPED`
audit summary **as non-actionable telemetry**. Existing alert code converts every
positive skipped reason into a HIGH `SOURCE_ROWS_SKIPPED` alert
(`finance/smart_alerts.py:_source_rows_skipped_alert`), and exports surface that alert.
The alert signal must therefore remove `non_projecting_evidence` from actionable
counts/reasons while retaining its raw audit count. A run containing only intentional
country evidence emits no `SOURCE_ROWS_SKIPPED` alert; a mixed run alerts only on the
remaining actionable reasons/count. This contract requires no new source-system
allowlist, source-row migration, or key namespace. Any future evidence discriminator
would need its full migration, parser/source allowlist, source-row-key, and audit-
contract changes before it could be proposed. The existing channel-month canonical
lane remains untouched.

The filter belongs in the shared dashboard/export signal reader **before** sensitive-
reason redaction. It subtracts only the explicit `non_projecting_evidence` count from
the raw total and removes that key from the returned reason map; any positive unlabeled
delta between `skipped_count` and the raw reason sum remains actionable fail-closed.
Thus callers without `audit.view_sensitive_payloads` still receive the correct
actionable total with an empty reason map, while the immutable audit row retains the
original total and intentional reason count.

Apply the same classification at the operator log boundary. An intentional-only run
emits bounded INFO telemetry, not the current "dropped rows" WARNING. A mixed run emits
a WARNING using only actionable counts/reasons plus separate bounded INFO telemetry for
the intentional count. The raw `ROWS_SKIPPED` audit payload remains unchanged.

**Acceptance criteria (U2):**
- [ ] Final PR #227 SHA is supplied and independently verified against this contract; until then this section remains pending, not shipped
- [ ] Parser emits country rows with `source_system="youtube_analytics"` and parser-owned `raw_payload.dimensions.country`
- [ ] Country rows persist as evidence with distinct keys (F5), then are skipped with `NON_PROJECTING_EVIDENCE`
- [ ] `ROWS_SKIPPED` audit telemetry includes the explicit non-projecting reason/count
- [ ] Intentional-only run produces no HIGH `SOURCE_ROWS_SKIPPED` dashboard/export alert; mixed run excludes intentional counts but retains actionable ones
- [ ] Intentional-only run produces no dropped-row WARNING; mixed-run WARNING counts
  only actionable skips while bounded INFO/audit telemetry retains intentional counts
- [ ] Redacted and sensitive dashboard/export reads agree on the actionable total; an
  unclassified positive delta remains HIGH rather than being subtracted accidentally
- [ ] `GoogleSourceNormalizer` never selects country-dimensional rows for canonical facts; removing the pre-bucket guard turns the guard test RED
- [ ] Full PG suite green; RLS-scoped reads

**Sequencing tripwire:** if the EGP flip (currency change, which re-keys source rows) is
scheduled, land U2 either clearly before or clearly after — never interleaved with — the
EGP re-key, so evidence rows don't split across two currency generations mid-month.

### U3 — Display + the calculation — **re-cost required; blocked until D-U1 + config service**

The former 6–10h estimate covered display/math only; it omitted the durable rate
migration, API, audit, overlap concurrency, and historical-correction work now required.

Per-channel panel/column: **US revenue**, **US share of channel revenue**, and
**estimated US withholding** — **only when** an effective-dated, operator-confirmed rate
exists in PostgreSQL. An environment variable is one mutable current scalar and cannot
reproduce an older month's estimate after the rate changes.

Add tenant-scoped `us_withholding_rates` records with: `id`, `tenant_id`, non-blank
`source_account_id`, allowlisted `income_category` (the initial value is
`youtube_copyright_royalty`), decimal `rate`, inclusive `effective_from_month`, exclusive
nullable `effective_to_month`, operator/source-report reference, confirmation timestamp,
`created_by`, creation reason, one-time interval-close actor/reason/time, `revoked_by`,
revoke reason, and created/revoked timestamps. Enforce 0 ≤ rate ≤ 0.30, valid `YYYY-MM`
intervals, and no overlapping active interval for the same tenant/account/category with
a database constraint or a same-key serialized transaction that is race-tested on
PostgreSQL. A forward-effective change locks the current open interval, sets its
previously-null exclusive end once with audit provenance, and inserts the next row in
one transaction; rate, source, account/category, and start are immutable, so older
months keep the same rate-record id. A retroactive correction revokes/supersedes the
affected interval and appends explicit replacement interval(s), preserving the old row
and its reason. Month lookup requires **exactly one** matching account/category
interval. Zero or multiple matches fail closed and suppress estimate fields.

Every figure is labeled "estimated"; the panel links the §0 explanation.

**Zero/missing semantics are typed, not inferred:**

- country evidence missing → `us_revenue=null`, `us_share=null`, estimate absent,
  `share_status="MISSING_COUNTRY_EVIDENCE"`;
- worldwide revenue missing → `us_share=null`, estimate absent,
  `share_status="MISSING_WORLDWIDE_REVENUE"`;
- worldwide revenue = 0 and US revenue = 0 → `us_share=null` (never `0%` and never a
  divide), `share_status="NOT_APPLICABLE_ZERO_WORLDWIDE"`; a zero estimate may be
  emitted only when the US zero is source-backed and an effective rate exists;
- worldwide revenue = 0 with positive US revenue, or US revenue greater than positive
  worldwide revenue → no share/estimate and `share_status="INCONSISTENT_EVIDENCE"`;
- only positive worldwide revenue with source-backed US evidence produces a decimal
  share. Absence is never coerced to zero.

**Backend-only estimate (AGENTS / finance rule):** the backend emits the labeled estimate
fields (US revenue, nullable share + status, estimated withholding, rate record id/rate,
effective interval, account/category, currency, confidence/source tokens). The SPA
**renders** those fields — it must **not** compute `US × rate` in the browser. Display
only: nothing feeds net revenue, allocation, close, or reconciliation (F6 fence).

**Acceptance criteria (U3):**
- [ ] PostgreSQL rate migration/repository/API are tenant-scoped, audited, non-overlapping, and append/revoke historical corrections
- [ ] No environment/default fallback; zero or multiple rate rows for the month suppress estimate fields and fail closed
- [ ] Recomputing an old month resolves the same persisted rate record and provenance
  after a later **forward-effective** rate change; a retroactive correction changes it
  only through an explicit revoke/replace audit trail
- [ ] Zero/missing/inconsistent revenue cases return the typed nullable semantics above; no divide-by-zero and no missing-as-zero
- [ ] With one confirmed effective rate, backend returns labeled estimate fields and rate-record provenance only
- [ ] SPA renders backend fields; no client-side withholding math
- [ ] Recon `DEFAULT_US_WITHHOLDING_RATE` (0.30) unchanged and dormant

### U4 — Actual-withholding anchor — **optional; re-cost required**

The former 4–8h estimate covered a small manual UI/CSV drop only; it omitted durable
revision persistence, tenant/payment constraints, idempotency, locks, and race tests.

The actual withheld amount is available only in the AdSense UI report (F3), so U4 adds
a durable manual-entry/CSV boundary plus a delta view: estimated (U3) vs actual. It is
not component state or an overwriteable setting.

Persist tenant-scoped `us_withholding_actuals` revisions with a composite
`(tenant_id, payment_id)` FK to the existing `adsense_payments` row (thereby fixing
account, month, payment name, amount, and currency) plus income category. Store a
non-negative finite actual withheld amount/currency, non-blank AdSense transaction-
report reference, optional durable artifact hash/reference, client idempotency key,
entered_by/reason/created_at, `supersedes_id`, and revoked_by/reason/at. Require the
anchor currency to match the payment; never convert it in U4. A PostgreSQL partial
unique index enforces one active revision per payment/category; a permanent tenant-
scoped unique idempotency key returns one result even after later revocation, while all
revision rows remain as history.

`adsense_payments.id` is currently the sole primary key; U4 must also add the redundant
parent uniqueness on `(tenant_id, id)` required by that composite tenant FK. This is a
constraint-only parent-table change, not a payment-row backfill.

`POST /adsense/payments/{payment_id}/withholding-actuals` and its CSV service use one
transaction to lock and validate the payment/month plus current active revision, mark
that prior revision inactive, insert the new row pointing to it, and append the audit
event. A replay with the same idempotency key returns the same committed revision; a key
reused with different content fails closed. There is no DELETE or in-place amount edit.
A locked finance month rejects new/corrected anchors; the operator must use the existing
audited unlock workflow, append the correction with a new reason/source reference, and
re-lock. Reads return revision provenance and a backend-computed estimated-vs-actual
delta **only when both values carry the same currency**; the SPA renders that value and
performs no finance math. Missing estimate data returns `delta=null` with a typed missing-
estimate status. A currency mismatch returns `delta=null` with
`CURRENCY_MISMATCH_NO_FX`, preserving both source amounts without conversion. The
comparison remains reproducible after restart. This is the only way to catch a silently
lapsed W-8 (actual jumps to 24/30% while the estimate stays 15%). Until U4, that check
is a monthly manual glance at the AdSense payments report.

**Acceptance criteria (U4):**
- [ ] Migration, tenant RLS, repository/service, typed API/CSV validation, and audit reason are present
- [ ] Anchor references an existing account/payment and source report; currency mismatch and unknown payment fail closed
- [ ] Retry is idempotent; correction appends/supersedes and preserves prior revision;
  concurrent writers leave one active revision
- [ ] Locked month rejects insert/correction until audited unlock; restart preserves the same actual-vs-estimated comparison
- [ ] Backend emits the actual-vs-estimated delta and provenance only for same-currency
  values; missing/mismatched inputs return typed null and the SPA performs no subtraction

---

## 3. Tripwires

- **T-U1** The withholding figure is an estimate and is always labeled as one; it never
  enters net finance math, close, allocation, or reconciliation without its own ruling.
  In particular: do **not** wire U2/U3 into `UsViewShareProvider` or change
  `DEFAULT_US_WITHHOLDING_RATE` (0.30) from this program (F6).
- **T-U2** The display-estimate rate is a **PostgreSQL effective-dated record with no
  default or environment fallback**. Missing/overlapping account-month configuration
  suppresses estimates. It is **not** a silent rename of the recon constant (0.30).
- **T-U3** Country-dimensioned rows never feed the monthly-totals lane (no double count);
  enforced by the U2 non-projecting guard. Distinct source_row_key dimensions (F5)
  prevent upsert collisions only; they are not a projection guard.
- **T-U3a** `NON_PROJECTING_EVIDENCE` remains audit telemetry but is excluded from
  actionable `SOURCE_ROWS_SKIPPED` counts. Intentional country evidence is not a HIGH
  defect; genuine skipped reasons remain HIGH and visible in dashboard/exports.
- **T-U4** No FX anywhere in this program (standing rule); US revenue is displayed in the
  source currency of the row, full stop.
- **T-U5** U2 respects EGP-program sequencing (currency is inside source_row_key).
- **T-U6** Official estimate math lives in the backend; the SPA does not calculate
  withholding locally.
- **T-U7** Missing/zero/inconsistent denominators use the typed nullable share states in
  U3; no divide-by-zero, missing-as-zero, or misleading 0%.
- **T-U8** U4 actuals are payment-linked append-only revisions; account/month lock and
  audit rules are never bypassed for a convenient manual scalar.
- **T-U9** U4 never computes a cross-currency delta. Currency mismatch is a typed null,
  not an invitation to add client-side or provider-derived FX.

## 4. Operator decisions

- **D-U1 (blocking U3):** Read the actual per-category rate from AdSense → Payments →
  "United States tax info"; record account type (business vs individual) and confirmed
  rate/source reference in the effective-dated PostgreSQL row for the exact AdSense
  account/category. If the page shows 24%/30% fallback, stop and fix the tax form before
  enabling estimates. **No estimate UI until D-U1 is recorded.**
- **D-U2:** Where the US panel lives (Rankings view column vs. per-channel detail panel) —
  design is the operator's per the standing rule; the plan only commits the numbers.
- **D-U3:** Whether U4 (manual actual-withholding anchor) is worth building now or stays a
  monthly manual check in the AdSense UI.
- **D-U4:** Store full country breakdown in U2 (recommended, same cost) or the US row only.

## 5. Migration and blast-radius statement

- **No migration/backfill required for the U2 projection fence itself.** Country rows
  stay in the existing source-row table/source-system/key namespace. The alert filter is
  read-model behavior; raw `ROWS_SKIPPED` audit telemetry is preserved.
- **Confirmed migration required for U3:** add `us_withholding_rates` with tenant RLS,
  account/category/effective-interval constraints, serialized overlap protection, and
  create/close/revoke audit provenance. No existing month is backfilled with a guessed
  rate; estimates remain absent until D-U1 rows exist.
- **Confirmed migration required for U4:** add `us_withholding_actuals` revisions linked
  to `adsense_payments` by tenant/payment FK, with tenant RLS, one-active-revision and
  idempotency constraints, source reference, and supersession history; add parent
  uniqueness on `adsense_payments(tenant_id, id)` for the composite FK. No historical
  actual is fabricated.
- PostgreSQL remains the sole source of truth. U3/U4 are display/evidence only: no
  allocation, close, net, reconciliation, or export finance result changes unless a
  later, separately reviewed ruling explicitly authorizes them.
