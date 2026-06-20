# Exports and Brand Reports

## Purpose
Generate management-ready Excel, PDF, CSV, and branded slide reports.

Current backend behavior: The foundation records queued export-job metadata and produces read-only finance workbook previews for `FINANCE_EXCEL` export jobs. It can generate and persist `.xlsx` workbook downloads from those previews, executive `.pdf` reports for `EXECUTIVE_PDF` jobs, `.pptx` branded slide packs for `BRANDED_SLIDE_PACK` jobs, and `.csv` analytics summaries for `ANALYTICS_SUMMARY_CSV` jobs. Workbook, PDF, and slide artifacts use SQL-backed finance summaries only. Analytics CSV artifacts use normalized `youtube_analytics` source rows aggregated by channel, metric, value kind, and currency; raw payloads, account IDs, content-owner IDs, and source report IDs are not exported. Generated artifacts are persisted through the configured export artifact store and recorded on the export job with `file_url` plus full artifact metadata: filename, content type, byte size, and SHA-256 checksum. External object storage uploads are not yet implemented.

## Planned export outputs

### 1. Finance Excel workbook (`FINANCE_EXCEL`)

Planned sheets:

```text
Executive Summary
Monthly Close
Company Breakdown
Sector Breakdown
Channel Breakdown
Deductions
Payment Gap
Confidence Notes
Raw Appendix
```

### 2. Executive PDF (`EXECUTIVE_PDF`)

Planned sections:

```text
Cover
Executive Summary
Gross vs Net Revenue
Deductions Explanation
Company Ranking
Channel Ranking
Problem Summary
Recommendations
```

### 3. Branded slide pack (`BRANDED_SLIDE_PACK`)

Planned slides:

```text
1. Cover
2. Monthly highlights
3. Holding revenue summary
4. Sector comparison
5. Company comparison
6. Channel ranking
7. Revenue deduction explanation
8. Payment gap analysis
9. Outside-CMS issues
10. Action items
```

### 4. Analytics summary CSV (`ANALYTICS_SUMMARY_CSV`)

Implemented CSV output for revenue-bearing analytics summaries. Requests require `exports.analytics`, `analytics.view`, and `finance.view_revenue` for the requested scope. The artifact is generated from normalized `google_revenue_source_rows` where `source_system='youtube_analytics'`, filtered by tenant, month, export currency, and the export's frozen channel scope. Downloads emit channel-scoped `REVENUE_VIEWED` audit records plus an export-scoped `EXPORT_DOWNLOADED` record.

## Template system

Templates should be configurable:

```text
logo
primary_color
secondary_color
font_family
cover_image
footer_text
slide_master
excel_header_style
pdf_header_footer
```

## Export controls

- Finance export requests require finance or owner role.
- Every export request is logged.
- Queued export metadata records month lock status.
- Queued export metadata records confidence-note inclusion.
- Queued export metadata records manual-override-note inclusion.
- Finance workbook previews require revenue export permission, revenue visibility for the export scope, and finance-month scoped finalized-payment and bank-reconciliation visibility.
- Finance workbook previews are audited as `REVENUE_VIEWED`, `PAYMENT_VIEWED`, and `BANK_RECONCILIATION_VIEWED`.
- Finance workbook downloads persist the XLSX artifact, update `file_url` and artifact metadata, mark the job `COMPLETED`, add an `EXPORT_DOWNLOADED` audit event, and return the file.
- Executive PDF downloads require the same revenue, finalized-payment, and bank-reconciliation permissions as finance workbook downloads.
- Executive PDF downloads persist the PDF artifact, update `file_url` and artifact metadata, mark the job `COMPLETED`, add an `EXPORT_DOWNLOADED` audit event, and return the file.
- Branded slide pack downloads require the same revenue, finalized-payment, and bank-reconciliation permissions as finance workbook downloads.
- Branded slide pack downloads persist the PPTX artifact, update `file_url` and artifact metadata, mark the job `COMPLETED`, add an `EXPORT_DOWNLOADED` audit event, and return the file.
- Analytics summary CSV downloads require analytics export permission plus analytics visibility for the export scope. The CSV uses the export's frozen channel set for scoped jobs, persists artifact metadata, marks the job `COMPLETED`, adds an `EXPORT_DOWNLOADED` audit event, and returns the file.
- Artifact storage failures for jobs that have not already completed leave the export job non-terminal and retryable, return `503`, and do not emit `EXPORT_DOWNLOADED`; if a completed job already has a persisted artifact, the existing artifact metadata is preserved and the retry returns an unavailable response without adding a download audit event.

## Export job fields

```text
export_id
export_type
scope_type
scope_id
month
currency
requested_by
status
file_url
artifact_filename
artifact_content_type
artifact_byte_size
artifact_checksum_sha256
failure_reason
month_lock_status
include_confidence_notes
include_manual_override_notes
created_at
completed_at
updated_at
```

Implementation note:
The backend foundation supports finance export requests and downloads for `FINANCE_EXCEL`, `EXECUTIVE_PDF`, and `BRANDED_SLIDE_PACK`, plus revenue-bearing `ANALYTICS_SUMMARY_CSV` requests and CSV downloads. Finance exports require both revenue export permission and revenue visibility for the requested scope. Analytics CSV exports require analytics export permission, analytics visibility, and revenue visibility for the requested scope. Group exports are checked against every member channel. Currency is currently restricted to USD until source-reported non-USD handling is implemented. Official exports must not use public FX rates as the source for revenue, payment, tax, deduction, or reconciliation values.

`GET /exports/{export_id}/finance-workbook-preview` is implemented for `FINANCE_EXCEL` only. It builds workbook-ready data from SQL source-of-truth rows: monthly revenue facts, approved/pending manual overrides, AdSense payment metadata, bank reconciliation receipt rows, finance month-close state, net-revenue summary, payment match summary, bank confirmation summary, and smart alerts. Month-wide AdSense payment and bank receipt rows are included only for global exports because phase 1 does not attribute cash receipts to sector, company, group, or channel scopes. It does not depend on a graph database.

`GET /exports/{export_id}/finance-workbook.xlsx` is implemented as an on-demand XLSX generator using pinned stable `openpyxl`. It writes the planned sheets from the same preview data, persists the generated artifact through the export artifact store, records `file_url`, filename, content type, byte size, and SHA-256 checksum on the export job, and marks the job `COMPLETED`.

`GET /exports/{export_id}/executive.pdf` is implemented as an on-demand executive PDF generator using pinned stable `ReportLab`. It writes the planned PDF sections from the same SQL-backed source summaries used by the finance workbook path. The PDF is guarded by the same finance export and sensitive finance read checks, persists artifact metadata, and marks the job `COMPLETED`.

`GET /exports/{export_id}/branded-slide-pack.pptx` is implemented as an on-demand branded slide generator using pinned stable `python-pptx`. It writes the planned 10-slide management deck from the same SQL-backed source summaries used by the finance workbook and PDF paths. The slide pack is guarded by the same finance export and sensitive finance read checks, persists artifact metadata, and marks the job `COMPLETED`.

The default artifact store writes to a local filesystem path and exposes object-storage-like `file-store://exports/{export_id}/{filename}` URIs. Set `UMS_EXPORT_ARTIFACT_DIR` to control the storage root in local and deployment environments; a future object-storage adapter can keep the same job metadata contract.

## Acceptance checks

- User can export by holding, sector, company, group, or channel.
- Queued export metadata uses USD because currency selection is currently restricted to USD; future non-USD exports must be backed by source-reported currency evidence or clearly labeled display conversion.
- Planned finance workbook preview includes the sheet manifest and source-backed explanations needed for XLSX generation.
- Finance workbook downloads produce a valid XLSX file with the planned sheet names.
- Executive PDF downloads produce a valid PDF with the planned management-summary sections.
- Branded slide downloads produce a valid PPTX file with the planned 10-slide management deck.
- Generated XLSX, PDF, and PPTX downloads persist artifact metadata, mark export jobs complete, and record SHA-256 checksums.
- Artifact storage failure marks an incomplete export job failed without recording a download audit, while completed jobs keep their existing artifact metadata.
- Export log records who created each report.
