# Connectors — YouTube and AdSense

## Purpose
Collect official data needed for performance, revenue, payment matching, and reconciliation.

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
raw_youtube_reports
fact_channel_daily
fact_channel_monthly
fact_revenue_monthly
fact_tax_monthly
fact_shorts_revenue
```

Foundation note: official Shorts, longform, and subscription revenue components
are currently stored as optional USD component columns on
`monthly_channel_revenue_facts` when the source report provides them. Null means
the report did not provide that component; the backend must not infer a missing
component from gross revenue.

## YouTube Analytics API connector

### Responsibilities
- Targeted dashboard queries.
- Ad hoc checks.
- Month/group verification.

### Use carefully
Bulk warehouse ingestion should prefer Reporting API. Analytics API is better for targeted queries and validation.

## YouTube Data API connector

### Responsibilities
- Channel metadata.
- Video metadata if needed later.
- Public statistics where useful.

### Output tables

```text
youtube_channels
youtube_videos
channel_metadata_snapshots
```

## AdSense Management API connector

### Responsibilities
- Pull monthly payment objects.
- Match payment date/month.
- Store paid/unpaid amount.
- Feed reconciliation engine.

### Output table

```text
adsense_payments
```

## Connector health states

```text
OK
FAILED_AUTH
FAILED_QUOTA
FAILED_REPORT_UNAVAILABLE
FAILED_DOWNLOAD
FAILED_PARSE
MISSING_EXPECTED_REPORT
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
- Failed runs create dashboard alerts.
