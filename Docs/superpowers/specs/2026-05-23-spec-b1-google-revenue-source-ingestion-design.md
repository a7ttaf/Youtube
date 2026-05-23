# Spec B1 - Google Revenue Source Ingestion Foundation - Design Spec

**Date:** 2026-05-23
**Owner:** Director Software Architect / Operator
**Status:** Design revision - replaces the FX-rate-led B1 plan
**Primary docs:** `Docs/18_MULTI_CURRENCY_ENGINE.md`,
`Docs/05_CONNECTORS_YOUTUBE_ADSENSE.md`

---

## 1. Problem statement

The earlier Spec B decomposition treated FX-rate storage as the next
multi-currency foundation. That is the wrong first cut for UMS finance.
YouTube and AdSense report money per month, and currency instability can make
public/provider FX rates unsuitable as the official source for revenue,
deduction, tax, payment, or reconciliation values.

B1 therefore shifts from "add/manage rates" to "ingest and preserve Google
source-reported money." The system must store what Google/YouTube/AdSense
reported, with currency, report identity, period, raw evidence, tenant scope,
and idempotent source row keys. Later display conversion may exist, but it is
not official finance.

## 2. Goals

- Store a platform-wide `currencies` reference table for ISO 4217 validation
  and display defaults.
- Add tenant-scoped `google_revenue_source_rows` for Google/YouTube/AdSense
  reported monetary source rows.
- Preserve `amount_native`, `currency_code`, `source_system`,
  `source_account_id`, `report_type`, `report_month`, period boundaries,
  `source_report_id` or `raw_file_id`, `raw_payload`, and ingestion metadata.
- Make source-row ingestion idempotent by
  `(tenant_id, source_system, source_row_key)`.
- Keep finance services reading SQL source-of-truth rows only.
- Keep existing USD fact tables unchanged until a later paired-column migration
  is designed around real source evidence.

## 3. Non-goals

- No `fx_rates` table in B1.
- No `fx_locked_month_rates` table in B1.
- No `tenants.fx_provider_settings` in B1.
- No `Permission.MANAGE_FX_RATES`.
- No `/fx/sync`, `/fx/rates/manual-upload`, ECB provider, exchangerate.host
  provider, manual CSV provider, or provider priority chain.
- No official finance calculation from public/provider FX rates.
- No locked-month FX freeze behavior.
- No frontend currency switcher.
- No paired-column migration of every existing `_usd` table in this PR.

## 4. Data model

### `currencies`

```sql
currencies (
  code text primary key,
  numeric_code text not null unique,
  name text not null,
  minor_unit integer null,
  is_supported boolean not null default false,
  activated_at timestamptz null
);
```

Seed the full ISO 4217 list as unsupported reference rows. Mark the v1 display
set (`AED`, `USD`, `EUR`, `GBP`, `SAR`, `EGP`) as supported. Supported rows
must have a known `minor_unit`.

### `google_revenue_source_rows`

```sql
google_revenue_source_rows (
  id uuid primary key default gen_random_uuid(),
  tenant_id uuid not null references tenants(id) on delete restrict,
  source_system text not null,
  source_row_key text not null,
  source_account_id text not null,
  content_owner_id text null,
  youtube_channel_id text null,
  report_type text not null,
  report_month text not null,
  period_start date not null,
  period_end date not null,
  metric_key text not null,
  value_kind text not null,
  amount_native numeric(20, 6) not null,
  currency_code text not null references currencies(code),
  source_report_id text null,
  raw_file_id uuid null,
  raw_payload jsonb not null default '{}'::jsonb,
  imported_by uuid null,
  ingested_at timestamptz not null default now(),
  unique (tenant_id, source_system, source_row_key)
);
```

Recommended constraints:

- `report_month` uses the existing `YYYY-MM` format.
- `amount_native >= 0`.
- `source_system in ('youtube_reporting', 'youtube_analytics',
  'adsense_management')`.
- `value_kind in ('estimated', 'settled', 'adjustment', 'tax', 'deduction')`.
- `raw_payload` is a JSON object.

Recommended indexes:

- `(tenant_id, report_month, source_system)`.
- Partial `(tenant_id, youtube_channel_id, report_month)` where
  `youtube_channel_id is not null`.

## 5. Connector flow

1. Resolve tenant and connector credential metadata.
2. Fetch or download the Google report using approved connector code.
3. Store raw file metadata before parsing.
4. Parse source rows and compute deterministic `source_row_key` values from
   stable Google dimensions/metrics/period/report identifiers.
5. Upsert `google_revenue_source_rows`.
6. Only after source rows exist, normalize selected values into existing
   finance fact tables.

Official Google reference surfaces:

- YouTube Analytics and Reporting APIs:
  https://developers.google.com/youtube/analytics/
- YouTube Analytics `reports.query`:
  https://developers.google.com/youtube/analytics/reference/reports/query
- AdSense Management API:
  https://developers.google.com/adsense/management/
- AdSense report generation:
  https://developers.google.com/adsense/management/reference/rest/v2/accounts.reports/generate

## 6. Legacy exchange-rate scaffold

`currency_exchange_rates`, `POST /exchange-rates/sync`, and
`GET /exchange-rates/latest` are pre-S2 scaffolding. B1 does not expand them.
They are not the official finance source. The implementation PR may leave them
in place if deleting them would increase blast radius, but new B1 code must not
depend on them for official revenue or payment values.

## 7. Authorization

No new FX permission is added. Google source ingestion uses existing connector
and finance boundaries:

- Connector runs: `connectors.run_jobs` on the relevant connector scope.
- Raw-file metadata: existing raw-file/connector permissions.
- Revenue reads: existing `finance.view_revenue` and confidence permissions.
- Payment reads: existing finalized-payment permissions.

If a future admin API allows editing supported currencies or source-ingestion
settings, that API needs its own permission review. It is not B1.

## 8. Blast radius

PostgreSQL remains the financial source of truth. Neo4j has been retired from
the active roadmap, so no graph projection update is required for this design.

Expected implementation blast radius:

- New SQLAlchemy model and Alembic migration for `currencies` and
  `google_revenue_source_rows`.
- New repository tests for idempotent source-row upserts and tenant isolation.
- Connector ingestion tests for YouTube/AdSense source preservation.
- Finance service tests proving existing USD facts are still consumed as they
  are today until a later paired-column migration.

No graph projection impact detected.

## 9. Section 5 - Testing

### 9.1 Test inventory

Layer: ISO snapshot
File: `tests/db/test_iso_4217_snapshot.py`
Checks: supported v1 set present; uppercase codes; unique codes; unique
numeric codes; `minor_unit` is `0..6` or `None`; supported rows have non-null
minor units; row count smoke test.

Layer: ORM and migration
File: `tests/db/test_google_revenue_source_models.py` and
`tests/db/test_google_revenue_source_migration.py`
Checks: `currencies` and `google_revenue_source_rows` columns, nullability,
constraints, unique key, JSONB object check, FK to tenants, FK to currencies,
tenant/month/source indexes, partial channel/month index. Migration round trip
runs on disposable PostgreSQL, not SQLite.

Layer: Source-row repository
File: `tests/connectors/test_google_revenue_source_repository.py`
Checks: insert/upsert idempotency by `(tenant_id, source_system,
source_row_key)`; update-on-rerun behavior; tenant isolation; month/source
listing; channel/month listing; rejects unsupported source system, invalid
month, negative amount, unknown currency, malformed raw payload.

Layer: YouTube ingestion
File: `tests/connectors/test_youtube_source_ingestion.py`
Checks: raw file saved before parse; parsed monetary rows preserve
Google-reported amount and currency; deterministic row key; rerun does not
duplicate rows; connector failure records failed job/alert without partial
finance normalization.

Layer: AdSense ingestion
File: `tests/connectors/test_adsense_source_ingestion.py`
Checks: payment/earnings report rows preserve Google-reported amount and
currency; payment-month matching uses source evidence; rerun is idempotent;
locked finance month blocks downstream fact/payment mutation while source
evidence rules remain explicit.

Layer: Finance integration
File: existing finance API/service tests plus focused new source tests
Checks: current USD endpoints continue to use existing SQL facts; non-USD
source rows are surfaced as source coverage or unsupported-currency issues
until paired-column/display work lands; no endpoint uses `currency_exchange_rates`
to produce official values.

Layer: Authorization
File: connector and revenue API permission tests
Checks: source ingestion requires connector job permission; source reads use
existing finance visibility; no `MANAGE_FX_RATES` permission exists; connector
admins can run connector jobs but cannot grant finance permissions beyond the
existing policy.

### 9.2 Explicit B1 non-tests

- No `FxConversionService` tests.
- No Hypothesis conversion property tests.
- No ECB/exchangerate/manual CSV provider tests.
- No `/fx/sync` or `/fx/rates/manual-upload` tests.
- No `MANAGE_FX_RATES` direct-grant tests.
- No locked-month FX freeze tests.
- No frontend currency switcher tests.
- No paired-column migration tests for all existing `_usd` tables.

### 9.3 Validation commands

Focused local loop:

```powershell
pytest -q `
  tests/db/test_iso_4217_snapshot.py `
  tests/db/test_google_revenue_source_models.py `
  tests/connectors/test_google_revenue_source_repository.py `
  tests/connectors/test_youtube_source_ingestion.py `
  tests/connectors/test_adsense_source_ingestion.py
```

Full implementation gate:

```powershell
python scripts/run_validation_gate.py
git diff --check
```

Migration tests require disposable PostgreSQL through
`UMS_TEST_DATABASE_URL`. SQLite is valid for repository unit tests only, not
for JSONB/FK/index/migration correctness.
