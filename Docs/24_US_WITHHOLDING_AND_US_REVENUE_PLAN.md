# Docs/24 — US Revenue Visibility & US Withholding Tax

Status: PLAN (no code in this PR). Owner question, verbatim: *"The project can see the views
as per API right, US cuts some tax 16% i think check it, i need some think that show this
thing but i need you check first the percentage of US tax for US views, also add this
calculation"* and *"the project can see how much revinue from us per channel right?"*

This document records the verified facts first (tax rules and what the codebase actually
ingests today), then the program that adds per-channel US revenue and the withholding
calculation as display/evidence surfaces.

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

- **No valid form on file** → default withholding is **24% for individuals / 30% for
  business accounts**, applied to **total worldwide earnings, not just US earnings**.
- **Lapsed/expired form** falls back to the same default. The form has to be re-validated
  when Google asks.
- If a payment were classed under **motion picture/TV royalties**, Egypt's treaty gives NO
  reduction (30%). The AdSense form shows which categories were claimed and at what rate.

**Operator action (only Mahmoud can do this):** AdSense → Payments → Manage settings →
"United States tax info" shows the exact per-category withholding rate Google is applying
to this account right now. That number — not this document — is the rate the calculation
must be configured with. Expected value: **15%**.

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
| F5 | `source_row_key` hashes report_type/period/**dimensions**/currency — country-dimensioned rows get distinct keys and **cannot collide with or double-count** the existing channel-month totals. This is also why the EGP program sequencing matters (currency is in the hash). | `connectors/google_source_parsers/source_row_keys.py` |

**Direct answers to the operator's questions:**

- *"The project can see the views as per API right?"* — Yes; the Analytics lane ingests
  per-channel monthly metrics via the API today.
- *"Can the project see how much revenue from US per channel?"* — **Not yet.** The data is
  reachable two ways (F2: it's already inside the persisted CSV evidence; F4: a documented
  API query fetches it directly), but nothing ingests, stores, or displays a US split
  today. That is exactly what bands U1–U3 add.

---

## 2. The program — bands U1–U4

Sequenced after the current fleet (#211–#217) and independent of the admin program
(Docs/23). U2 must coordinate with the EGP program (Docs: EGP phases) because both touch
source-row identity.

### U1 — Probe: prove the numbers before building (2–4h, read-only script)

A standalone script (same shape as the EGP workbook reference script) that, for one month:
per channel, runs `filters=channel==UC…` + `dimensions=country` + `metrics=estimatedRevenue`,
extracts the `US` row, and prints channel → US revenue → US share % → estimated
withholding at the configured rate. Acceptance: the per-channel **worldwide totals** in the
same responses must equal the existing workbook/UMS numbers channel-by-channel (proves the
country cut decomposes the number we already trust, no drift). No writes, no UMS changes.

### U2 — Ingest the US slice (10–16h)

Extend the Analytics lane with a second, additive report shape per channel-month:
country-dimensioned revenue rows (at minimum the US row; storing the full country
breakdown is the same cost and future-proofs "top geographies"). New rows carry
`dimensions={channel, country}` so source_row_keys are distinct from the monthly totals
(F5). The existing channel-month lane is untouched — the US slice is evidence, never an
input to reconciliation or close math. Includes: parser + row-key tests, RLS-scoped
repository reads, full PG suite.

**Sequencing tripwire:** if the EGP flip (currency change, which re-keys source rows) is
scheduled, land U2 either clearly before or clearly after — never interleaved with — the
EGP re-key, so evidence rows don't split across two currency generations mid-month.

### U3 — Display + the calculation (6–10h)

Per-channel panel/column: **US revenue**, **US share of channel revenue**, and
**estimated US withholding = US revenue × rate**. The rate comes from a fail-fast setting
(`UMS_US_WITHHOLDING_RATE`, default `0.15`, validated 0 ≤ rate ≤ 0.30) — configured, not
hardcoded, because the true rate is whatever the AdSense tax-info page shows (§0). Every
figure is labeled "estimated"; the panel links the §0 explanation. Display only: nothing
feeds net revenue, allocation, close, or reconciliation.

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
- **T-U2** The rate is configuration with a validated range — never hardcoded, because the
  legal rate depends on the tax form Google has on file, which can change.
- **T-U3** Country-dimensioned rows never feed the monthly-totals lane (no double count);
  enforced by distinct source_row_key dimensions (F5) and a guard test in U2.
- **T-U4** No FX anywhere in this program (standing rule); US revenue is displayed in the
  source currency of the row, full stop.
- **T-U5** U2 respects EGP-program sequencing (currency is inside source_row_key).

## 4. Operator decisions

- **D-U1 (blocking U3's default):** Read the actual per-category rate from AdSense →
  Payments → "United States tax info" and confirm 15% (copyright royalties, Egypt treaty).
  If the page shows 24%/30%, stop and fix the tax form before building anything.
- **D-U2:** Where the US panel lives (Rankings view column vs. per-channel detail panel) —
  design is the operator's per the standing rule; the plan only commits the numbers.
- **D-U3:** Whether U4 (manual actual-withholding anchor) is worth building now or stays a
  monthly manual check in the AdSense UI.
- **D-U4:** Store full country breakdown in U2 (recommended, same cost) or the US row only.
