# Exports and Brand Reports

## Purpose
Generate management-ready Excel, PDF, and branded slide reports.

## Export types

### 1. Finance Excel workbook

Sheets:

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

### 2. Executive PDF

Sections:

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

### 3. Branded slide pack

Slides:

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

- Finance exports require finance or owner role.
- Every export is logged.
- Export should show month lock status.
- Export should include confidence notes.
- Export should include manual override notes.

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
created_at
completed_at
```

Implementation note:
The backend foundation currently records queued export-job metadata only. It supports finance export requests for `FINANCE_EXCEL`, `EXECUTIVE_PDF`, and `BRANDED_SLIDE_PACK`, plus non-financial `ANALYTICS_SUMMARY_CSV` requests for export operators. Finance exports require both revenue export permission and revenue visibility for the requested scope. Group exports are checked against every member channel. Currency is restricted to USD until an exchange-rate source is implemented.

## Acceptance checks

- User can export by holding, sector, company, group, or channel.
- Export output matches selected currency.
- Finance workbook includes formulas/explanations.
- Export log records who created each report.
