# Exports and Brand Reports

## Purpose
Generate management-ready Excel, PDF, and branded slide reports.

Current backend behavior: the foundation records queued export-job metadata, produces a read-only finance workbook preview for `FINANCE_EXCEL` export jobs, can generate an on-demand `.xlsx` workbook download from that preview, can generate an on-demand executive `.pdf` for `EXECUTIVE_PDF` export jobs, and can generate an on-demand `.pptx` branded slide pack for `BRANDED_SLIDE_PACK` export jobs. The workbook, PDF, and slide pack use SQL-backed finance summaries only. It does not generate CSV files yet, does not persist generated artifacts, and does not mark export jobs complete.

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

Planned CSV output for non-financial analytics exports that export operators can request without finance visibility.

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
- Finance workbook downloads add an `EXPORT_DOWNLOADED` audit event and return an XLSX file without storing it in `file_url`.
- Executive PDF downloads require the same revenue, finalized-payment, and bank-reconciliation permissions as finance workbook downloads.
- Executive PDF downloads add an `EXPORT_DOWNLOADED` audit event and return a PDF file without storing it in `file_url`.
- Branded slide pack downloads require the same revenue, finalized-payment, and bank-reconciliation permissions as finance workbook downloads.
- Branded slide pack downloads add an `EXPORT_DOWNLOADED` audit event and return a PPTX file without storing it in `file_url`.

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
month_lock_status
include_confidence_notes
include_manual_override_notes
created_at
completed_at
updated_at
```

Implementation note:
The backend foundation supports finance export requests for `FINANCE_EXCEL`, `EXECUTIVE_PDF`, and `BRANDED_SLIDE_PACK`, plus non-financial `ANALYTICS_SUMMARY_CSV` requests for export operators. Finance exports require both revenue export permission and revenue visibility for the requested scope. Group exports are checked against every member channel. Currency is currently restricted to USD until an exchange-rate source is implemented.

`GET /exports/{export_id}/finance-workbook-preview` is implemented for `FINANCE_EXCEL` only. It builds workbook-ready data from SQL source-of-truth rows: monthly revenue facts, approved/pending manual overrides, AdSense payment metadata, bank reconciliation receipt rows, finance month-close state, net-revenue summary, payment match summary, bank confirmation summary, and smart alerts. It does not use Neo4j as a financial source of truth.

`GET /exports/{export_id}/finance-workbook.xlsx` is implemented as an on-demand XLSX generator using pinned stable `openpyxl`. It writes the planned sheets from the same preview data and does not persist or upload the generated file in this phase.

`GET /exports/{export_id}/executive.pdf` is implemented as an on-demand executive PDF generator using pinned stable `ReportLab`. It writes the planned PDF sections from the same SQL-backed source summaries used by the finance workbook path. The PDF is guarded by the same finance export and sensitive finance read checks and does not persist or upload the generated file in this phase.

`GET /exports/{export_id}/branded-slide-pack.pptx` is implemented as an on-demand branded slide generator using pinned stable `python-pptx`. It writes the planned 10-slide management deck from the same SQL-backed source summaries used by the finance workbook and PDF paths. The slide pack is guarded by the same finance export and sensitive finance read checks and does not persist or upload the generated file in this phase.

## Acceptance checks

- User can export by holding, sector, company, group, or channel.
- Queued export metadata uses USD because currency selection is currently restricted to USD.
- Planned finance workbook preview includes the sheet manifest and source-backed explanations needed for XLSX generation.
- Finance workbook downloads produce a valid XLSX file with the planned sheet names.
- Executive PDF downloads produce a valid PDF with the planned management-summary sections.
- Branded slide downloads produce a valid PPTX file with the planned 10-slide management deck.
- Export log records who created each report.
