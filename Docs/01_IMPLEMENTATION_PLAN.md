# Implementation Plan

## Goal
Deliver a smart internal revenue engine for 300+ YouTube channels, focused first on monthly channel/company/sector numbers.

## Build rule
Do not start with a normal dashboard. Start with the **data and calculation engine**, then expose it through a dashboard.

---

## Phase 0 — Foundation decisions

### Scope
- UMS beta only.
- YouTube channels only.
- No title/show mapping in the first release.
- No Content ID fingerprint/claims module in the first release.
- No Neo4j or graph projection layer in the active roadmap.

### Outputs
- Final stack decision.
- OAuth/API access plan.
- Channel inventory file format.
- Finance month-close input format.

### Acceptance gate
- At least 300+ channels can be listed, classified, and mapped to dynamic groups.

---

## Phase 1 — Channel registry and hierarchy

### Build
- Dynamic organization hierarchy.
- Channel registry.
- CMS status: inside CMS / outside CMS / unknown.
- Revenue-required flag.
- Group builder.
- Role model.

### Outputs
- Channel master table.
- Company/sector/group mapping.
- Outside-CMS monitor.

### Acceptance gate
- Every active channel has a company/sector/group assignment or appears in an unmapped warning list.

---

## Phase 2 — YouTube ingestion

### Build
- YouTube Reporting API jobs.
- YouTube Analytics API targeted queries.
- YouTube Data API metadata sync.
- Raw report storage.
- Normalized monthly channel facts.
- Missing report alerts.

### Outputs
- Daily/monthly channel metrics.
- Monthly revenue facts where available.
- Revenue source labels.

### Acceptance gate
- Dashboard can show gross monthly revenue and performance for CMS-linked channels.

---

## Phase 3 — AdSense payment matching

### Build
- AdSense account connector.
- Monthly payment pull.
- Paid/unpaid status.
- Payment month matcher.
- Payment-vs-YouTube comparison.

### Outputs
- Monthly AdSense payment table.
- Payment match status.
- Payment gap value.

### Acceptance gate
- System can show whether YouTube revenue total matches the AdSense payment amount.

---

## Phase 4 — Reconciliation and allocation engine

### Build
- Finance month-close screen.
- Manual bank/payment input.
- Tax/deduction ingestion where available.
- Allocation rules.
- Net revenue by channel/company/sector.
- Manual override rules.

### Outputs
- Gross revenue.
- Deductions.
- Net revenue.
- Deduction percentage.
- Unresolved gap.
- Confidence rating.

### Acceptance gate
- Finance can generate a channel-level net revenue table for a selected month.

---

## Phase 5 — Smart dashboard

### Build
- Revenue command center.
- Explain-number drawer.
- Smart problem panel.
- Company/sector/channel ranking.
- Outside-CMS issue monitor.
- Month-close status.

### Outputs
- Internal decision dashboard.
- Smart alerts.
- Management-ready summaries.

### Acceptance gate
- A user can select month + group + currency and receive gross, deduction, net, and explanation.

---

## Phase 6 — Export center

### Build
- Excel export.
- PDF report.
- Branded slide export.
- Export templates.
- Export audit log.

### Outputs
- Monthly finance workbook.
- Executive PDF.
- Management slide pack.

### Acceptance gate
- Finance can generate a locked monthly report by company, sector, or all UMS.

---

## Phase 7 — Hardening

### Build
- Audit logs.
- Failure alerts.
- Data quality checks.
- Backup/export retention.
- OAuth token monitoring.
- Month locking.

### Acceptance gate
- The system detects missing channels, missing reports, unmatched payments, and manually overridden values.
