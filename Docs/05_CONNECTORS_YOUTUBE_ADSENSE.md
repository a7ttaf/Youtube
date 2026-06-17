# Connectors — YouTube and AdSense

## Purpose
Collect official Google data needed for performance, revenue, payment
matching, and reconciliation. Monetary values reported by YouTube/Google and
AdSense are the official source for finance ingestion; connector code must
preserve the reported currency and source evidence instead of deriving official
amounts from market FX rates.

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
