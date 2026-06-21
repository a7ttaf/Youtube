# Connectors — YouTube and AdSense

## Purpose
Collect official Google data needed for performance, revenue, payment
matching, and reconciliation. Monetary values reported by YouTube/Google and
AdSense are the official source for finance ingestion; connector code must
preserve the reported currency and source evidence instead of deriving official
amounts from market FX rates.

## Google credential contract

UMS must use only Google-issued credential material for live connectors. The
operator must not link a personal Gmail session directly, store Google account
passwords, reuse browser cookies, or automate Gmail login as a substitute for
official API authorization.

Credential scope by API:

- YouTube Data API public metadata can use an API key where Google allows
  key-only access. Private or owner-specific metadata still requires an
  authorized Google credential.
- YouTube Reporting API, YouTube Analytics API revenue/account queries, and
  AdSense Management API account or payment data require official Google
  OAuth 2.0 authorization tokens with the narrowest needed scopes. API-key-only
  access is not a valid contract for those private revenue surfaces.

UMS stores connector credentials as external secret references and refreshes
Google authorization tokens server-side. The credential owner decision remains
blocked until the operator supplies approved Google Cloud project credentials
and confirms the allowed API/scopes for each live connector.

References:

- <https://developers.google.com/youtube/v3/getting-started>
- <https://developers.google.com/youtube/reporting/guides/registering_an_application>
- <https://developers.google.com/youtube/analytics/reference/reports/query>
- <https://developers.google.com/adsense/management/getting_started>
- <https://developers.google.com/identity/protocols/oauth2>
- <https://developers.google.com/terms/api-services-user-data-policy>

## YouTube Reporting API connector

### Responsibilities
- Create/list reporting jobs.
- Download bulk reports.
- Store raw report files.
- Normalize report rows into warehouse tables.
- Detect missing reports.

### Data needed
- Channel daily/monthly metrics.
- Content-owner revenue reports where available.
- System-managed reports where available.
- Shorts financial reports if available.
- Tax/financial summaries if available.

### Output tables

```text
google_revenue_source_rows   (raw ingestion provenance — amount, currency, source row key)
monthly_channel_revenue_facts  (normalized per-channel per-month finance facts)
```

Downloaded Google reports are stored as raw files before parsing, then parsed
into tenant-scoped `google_revenue_source_rows` with the Google-reported amount,
currency, account/report identifiers, period, source row key, and raw payload
reference. After that provenance exists, selected values are normalized into
`monthly_channel_revenue_facts` by the post-run normalizer.

Official Shorts, longform, and subscription revenue components are stored as
optional USD component columns on `monthly_channel_revenue_facts` when the
source report provides them. Null means the report did not provide that
component; the backend must not infer a missing component from gross revenue.

When a configured YouTube Reporting job returns no report metadata for the
requested month, the runner records a typed per-report failure instead of
silently skipping the job. The live run finishes `FAILED` or `PARTIAL` based on
the rest of the run, records no raw file for the missing report, and emits a
terminal connector audit edge that can surface as the dashboard
`CONNECTOR_RUNS_FAILED` smart alert for users with `audit.view`.

## YouTube Analytics API connector

### Responsibilities
- Targeted dashboard queries.
- Ad hoc checks.
- Month/group verification.

### Use carefully
Bulk warehouse ingestion should prefer Reporting API. Analytics API is better
for targeted queries and validation. If an Analytics response includes monetary
metrics, the connector must preserve Google's reported currency and source
parameters; it must not convert the amount into another official currency.

## YouTube Data API connector

### Responsibilities
- Channel metadata.
- Video metadata if needed later.
- Public statistics where useful.

### Output tables

The YouTube Data API connector provides metadata queries only. No dedicated
warehouse tables are provisioned in this phase; channel identity and
content-owner details are stored on the core `channels` ORM model.

## AdSense Management API connector

### Responsibilities
- Pull monthly payment objects.
- Pull AdSense earnings/payment reports where available.
- Match payment date/month.
- Store paid/unpaid amount in the Google-reported payment currency.
- Preserve report/account identifiers and raw payload references.
- Feed reconciliation engine.

### Output table

```text
adsense_payments
google_revenue_source_rows
```

## Connector health states

**Connector run status** (`connector_runs.status`):

```text
RUNNING
SUCCEEDED
PARTIAL
FAILED
```

The latest terminal `CONNECTOR_JOB_RUN` audit edge per connector/account/month
is the operator-facing smart-alert signal. A later `SUCCEEDED` run clears an
older `FAILED` or `PARTIAL` state for the same connector/account/month.

**Credential health state** (derived at read time in `GET /connectors/credentials/health`):

```text
healthy          — last refresh succeeded, token not expiring
expiring         — token at or within 24h of expiry
auth_failed      — last refresh failed or an error class is recorded
missing          — no stored secret reference
unknown          — credential is not in active status (disabled, rotating, etc.)
```

## Required logs

```text
connector_name
account_id
run_id
started_at
finished_at
status
records_downloaded
files_downloaded
error_message
```

## Acceptance checks

- Connector can re-run safely without duplicating records.
- Raw files are stored before parsing.
- Every normalized row links back to source report/run.
- Every monetary source row preserves the amount and currency reported by
  YouTube/Google or AdSense.
- No connector path uses public FX rates to create official revenue,
  payment, tax, or deduction values.
- Failed runs create dashboard alerts.
