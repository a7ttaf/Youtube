# Source-Reported Currency Policy

> Status: **planned** - revised on 2026-05-23.
> Replaces the earlier FX-rate-led multi-currency design. Official finance
> values come from Google/YouTube/AdSense source reports, not from third-party
> exchange-rate reconstruction.

---

## Why this changed

UMS monthly finance must match the values that YouTube and AdSense report.
In markets where currency rates are unstable, especially Egypt-facing finance
workflows, deriving official monthly revenue from a public FX feed or a manual
rate table creates numbers that can disagree with Google settlement and bank
evidence.

The design rule is therefore:

1. **Google-reported money is authoritative.** Store the amount and currency
   exactly as the YouTube / AdSense report provides it.
2. **FX rates are not an official finance source.** Market rates can be used
   later for clearly labeled display conversion only.
3. **Month close freezes source evidence.** A locked month preserves imported
   source rows, selected revenue facts, AdSense payments, bank receipts, and
   reconciliation decisions. It does not freeze a derived FX rate in B1.
4. **No manual rate workflow in B1.** Do not add `Permission.MANAGE_FX_RATES`,
   `fx_rates`, `fx_locked_month_rates`, manual FX CSV upload, ECB sync, or
   exchangerate.host sync to the B1 source-of-truth path.

This page now describes the source-reported money model, the minimum currency
reference data still needed for validation, the Google ingestion boundary, and
the testing contract for the revised B1.

---

## Source-of-truth rules

Every official monetary row must preserve:

| Field | Meaning |
|---|---|
| `amount_native` | Amount exactly as reported by the source. |
| `currency_code` | ISO 4217 code reported by the source or requested from the Google report API. |
| `source_system` | `youtube_reporting`, `youtube_analytics`, or `adsense_management`. |
| `source_account_id` | Google account, AdSense account, content owner, or channel context. |
| `report_month` | Canonical `YYYY-MM` finance month. |
| `period_start` / `period_end` | Exact report date range. |
| `source_report_id` / `raw_file_id` | Link back to the downloaded report or API result. |
| `raw_payload` | Original row or source metadata needed for audit/debugging. |
| `ingested_at` / `imported_by` | Provenance for the connector run or operator-controlled import. |

Official calculations must not overwrite native source amounts with converted
values. Any later conversion envelope must remain separate from the official
source row and be labeled as display-only.

---

## Currency reference

The `currencies` table is still useful as reference data. It validates Google
report currency codes and user/tenant display preferences; it does not imply
that UMS manages official exchange rates.

```sql
CREATE TABLE currencies (
    code             text PRIMARY KEY,
    numeric_code     text NOT NULL,
    name             text NOT NULL,
    minor_unit       integer,
    is_supported     boolean NOT NULL DEFAULT false,
    activated_at     timestamptz,
    CONSTRAINT ck_currencies_code_format CHECK (
        length(code) = 3 AND code = upper(code)
    ),
    CONSTRAINT ck_currencies_numeric_code_format CHECK (
        length(numeric_code) = 3
    ),
    CONSTRAINT uq_currencies_numeric_code UNIQUE (numeric_code),
    CONSTRAINT ck_currencies_minor_unit_range
        CHECK (minor_unit IS NULL OR minor_unit BETWEEN 0 AND 6),
    CONSTRAINT ck_currencies_supported_minor
        CHECK (is_supported = false OR minor_unit IS NOT NULL),
    CONSTRAINT ck_currencies_supported_activated
        CHECK (is_supported = false OR activated_at IS NOT NULL)
);
```

Seed the full ISO 4217 snapshot as unsupported rows, then mark the v1 display
set as supported: `AED`, `USD`, `EUR`, `GBP`, `SAR`, `EGP`.

`tenants.primary_currency` and `users.preferred_currency` may reference this
table for display defaults. They must not drive official finance conversion in
B1.

---

## Planned Google source row storage

B1 should introduce a source-row table for monetary values returned by Google
reports. Exact implementation can split reports and rows into two physical
tables, but the contract below is the minimum storage shape.

```sql
CREATE TABLE google_revenue_source_rows (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id          uuid NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    source_system      text NOT NULL,
    source_row_key     text NOT NULL,
    source_account_id  text NOT NULL,
    content_owner_id   text,
    youtube_channel_id text,
    report_type        text NOT NULL,
    report_month       text NOT NULL,
    period_start       date NOT NULL,
    period_end         date NOT NULL,
    metric_key         text NOT NULL,
    value_kind         text NOT NULL,
    amount_native      numeric(20, 6) NOT NULL,
    currency_code      text NOT NULL REFERENCES currencies(code),
    source_report_id   text,
    raw_file_id        uuid REFERENCES raw_report_files(id) ON DELETE RESTRICT,
    raw_payload        jsonb NOT NULL DEFAULT '{}'::jsonb,
    imported_by        uuid,
    ingested_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_google_revenue_source_rows_key
        UNIQUE (tenant_id, source_system, source_row_key),
    CONSTRAINT ck_google_revenue_source_rows_month_format CHECK (
        length(report_month) = 7
        AND substr(report_month, 5, 1) = '-'
        AND substr(report_month, 1, 1) BETWEEN '0' AND '9'
        AND substr(report_month, 2, 1) BETWEEN '0' AND '9'
        AND substr(report_month, 3, 1) BETWEEN '0' AND '9'
        AND substr(report_month, 4, 1) BETWEEN '0' AND '9'
        AND substr(report_month, 6, 2) BETWEEN '01' AND '12'
    ),
    CONSTRAINT ck_google_revenue_source_rows_period_order
        CHECK (period_end >= period_start),
    CONSTRAINT ck_google_revenue_source_rows_amount_non_negative
        CHECK (amount_native >= 0),
    CONSTRAINT ck_google_revenue_source_rows_source_system CHECK (
        source_system IN (
            'youtube_reporting',
            'youtube_analytics',
            'adsense_management'
        )
    ),
    CONSTRAINT ck_google_revenue_source_rows_value_kind CHECK (
        value_kind IN ('estimated', 'settled', 'adjustment', 'tax', 'deduction')
    )
);

CREATE INDEX ix_google_revenue_source_rows_month
    ON google_revenue_source_rows (tenant_id, report_month, source_system);
CREATE INDEX ix_google_revenue_source_rows_channel
    ON google_revenue_source_rows (tenant_id, youtube_channel_id, report_month)
    WHERE youtube_channel_id IS NOT NULL;
```

`source_row_key` is a deterministic connector-built key from Google source
identifiers, report type, date range, metric, channel/content-owner context,
and row dimensions. It avoids nullable-column uniqueness traps and makes
re-ingestion idempotent.

`monthly_channel_revenue_facts` can remain the normalized serving table for
current APIs, but selected values should be copied from Google source rows with
their source references preserved. Later paired-column migration can replace
the existing `*_usd` fields; B1 does not need market FX to start ingestion.

---

## Google connector boundaries

Primary source APIs:

- YouTube Reporting API: bulk reports for channel/content-owner reporting.
- YouTube Analytics API: targeted queries and validation, including monetary
  metrics when the account has the required access.
- AdSense Management API: earnings and payment reports. When report APIs expose
  currency controls such as `currencyCode`, B1 must make the request policy
  explicit and still store the currency returned by Google with each monetary
  row.

B1 connector behavior:

1. Fetch or download the Google report.
2. Store raw file/API metadata before parsing.
3. Parse rows into `google_revenue_source_rows`.
4. Upsert by `(tenant_id, source_system, source_row_key)`.
5. Normalize selected source rows into existing revenue/payment tables only
   after source provenance is persisted.

The connector layer owns OAuth credentials, retry, quota/auth failures, report
availability, and parse errors. Finance services consume stored SQL rows; they
do not call Google APIs directly and do not calculate official FX.

Official docs used for this design direction:

- YouTube Analytics and Reporting APIs:
  <https://developers.google.com/youtube/analytics/>
- YouTube Analytics `reports.query`:
  <https://developers.google.com/youtube/analytics/reference/reports/query>
- AdSense Management API:
  <https://developers.google.com/adsense/management/>
- AdSense `accounts.reports.generate`:
  <https://developers.google.com/adsense/management/reference/rest/v2/accounts.reports/generate>

---

## What happens to exchange rates

The existing `currency_exchange_rates` scaffold is legacy infrastructure. It
can remain inert until a cleanup PR retires it, but B1 must not expand it into
an official finance workflow.

Deferred / removed from B1:

- `fx_rates`
- `fx_locked_month_rates`
- `tenants.fx_provider_settings`
- `Permission.MANAGE_FX_RATES`
- `/fx/rates/manual-upload`
- `/fx/sync`
- ECB / exchangerate.host / manual CSV provider priority
- locked-month FX freeze tests
- conversion-service property tests

Future display-only conversion may return as a separate spec, likely with a
name such as `display_fx_rates`, and must be visibly non-official in API
responses, explain drawers, exports, and audit notes.

---

## API surface direction

B1 should build on existing connector and report surfaces instead of creating
rate-management APIs.

Use / extend:

```http
GET  /connectors/credentials
POST /connectors/credentials
POST /connectors/jobs
POST /reports/raw-files
GET  /reports/raw-files
POST /revenue/facts
POST /adsense/sync-payments
```

Future source-row endpoints, if needed, should expose Google source evidence,
not FX rates:

```http
GET /revenue/source-rows?month=2026-03&source_system=adsense_management
GET /revenue/source-rows/{id}
```

These reads require finance revenue visibility and must redact raw payload
fields that could expose credentials, private account metadata, or other
sensitive Google data.

---

## Section 5 - Testing for revised B1

Replace the old FX Section 5 inventory with source-ingestion tests.

### Source model and migration tests

- `currencies` seed includes the full ISO snapshot and the v1 supported display
  set.
- Google source-row storage preserves source system, source account/channel
  identifiers, report month/date range, native amount, currency code, source
  reference, ingestion timestamp, and raw payload.
- Money uses `numeric`; currency codes use `text` plus ISO-format checks.
- B1 migration does not create `fx_rates`, `fx_locked_month_rates`,
  `tenants.fx_provider_settings`, or `MANAGE_FX_RATES`.
- Migration round-trip runs against PostgreSQL for JSONB, numeric, FK, and
  index behavior.

### Repository tests

- Insert and upsert Google source rows by deterministic source key.
- Re-ingesting the same Google row is idempotent and does not duplicate
  revenue.
- Tenant isolation is enforced on every read and write.
- Raw payload and source provenance survive round-trip.
- Missing amount, currency code, source system, source account, report month,
  or source key fails closed with a typed validation error.

### Connector job tests

- YouTube Reporting ingestion stores raw metadata before normalized source
  rows.
- YouTube Analytics monetary query ingestion stores source rows without FX
  conversion.
- AdSense report ingestion stores source rows without FX conversion.
- API/client failures create typed connector errors and do not create partial
  finance rows.
- Retrying an already-ingested report updates ingestion metadata safely without
  duplicating source revenue.

### Finance integration tests

- Revenue reports, exports, payment matching, and reconciliation consume
  Google-reported native amounts or existing selected source facts.
- No official total depends on `currency_exchange_rates`, `fx_rates`, or a
  market-rate provider.
- Month close locks selected source values and reconciliation inputs, not an
  FX-rate cache.
- If both estimated YouTube revenue and settled AdSense revenue exist, tests
  assert that the API labels them distinctly and does not silently replace one
  with the other.

### Permission tests

- Connector admins can configure or run Google ingestion through connector
  permissions.
- Finance admins can review and reconcile imported finance data.
- No role can add manual FX rates because B1 exposes no manual FX-rate route
  and no `MANAGE_FX_RATES` permission.

### Explicit B1 non-tests

- No `FxConversionService` tests.
- No Hypothesis FX round-trip, identity, sum-invariance, pivot, or locked-rate
  tests.
- No ECB / exchangerate.host / manual CSV provider tests.
- No `/fx/*` API tests.
- No paired-column migration tests for every existing `*_usd` field unless a
  separate paired-column migration spec is explicitly approved.

---

## Blast radius

Design-only change in this docs update. The implementation PR for revised B1
will touch PostgreSQL models/migrations, connector ingestion services,
repository tests, and finance reader tests.

PostgreSQL remains the source of truth. Neo4j is retired from the active
architecture and must not be reintroduced as a financial source.

Potential graph projection impact: none in the active architecture. If a future
read model projects `monthly_channel_revenue_facts` from Google source rows, it
must preserve source provenance and must not calculate official finance values
from display FX.

---

## Open decisions before implementation

- Exact YouTube report types available to the UMS account.
- Which monetary metrics are authoritative for estimated YouTube revenue.
- Whether AdSense settled earnings or paid payment reports win when both are
  present for the same month.
- Whether Google reports should be requested in account currency or with an
  explicit `currencyCode`.
- How outside-CMS channels obtain official source rows.
- How bank receipt variance should be explained when it reflects bank timing,
  fees, or local currency settlement rather than a Google-reported revenue
  difference.
