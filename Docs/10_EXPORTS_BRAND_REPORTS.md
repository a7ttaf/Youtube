# Exports and Brand Reports

## Purpose
Generate management-ready Excel, PDF, and branded slide reports.

Current backend behavior: the foundation records queued export-job metadata only. It does not generate workbook, PDF, CSV, or slide files yet.

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

## Acceptance checks

- User can export by holding, sector, company, group, or channel.
- Queued export metadata uses USD because currency selection is currently restricted to USD.
- Planned finance workbook output includes formulas/explanations.
- Export log records who created each report.
