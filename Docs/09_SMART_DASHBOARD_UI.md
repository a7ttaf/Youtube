# Smart Dashboard UI

## Product style
This is not a passive dashboard. It is a command center that answers finance and management questions.

## Main pages

### 1. Revenue Command Center

Filters:

```text
Month
Currency
Holding / sector / company / channel / group
Revenue type: gross / finalized / net / allocated
CMS status
Confidence
```

Cards:

```text
Gross revenue
AdSense/payment amount
Bank received amount
Total deductions
Net revenue
Unresolved gap
Month status
```

### 2. Channel Revenue Table

Columns:

```text
Channel
Company
Sector
CMS status
Gross revenue
Tax
Allocated deductions
Net revenue
Deduction %
Confidence
Issue flags
Explain button
```

### 3. Company / Sector Comparison

Views:

```text
Top companies by gross revenue
Top companies by net revenue
Top sectors by growth
Highest deduction percentage
Lowest confidence numbers
```

### 3.1 Smart Issue Panel

Foundation API:

```text
GET /channels/issues
```

The channel issue panel starts with scoped registry health checks. It shows only
channels visible to the caller and currently covers missing company, missing
sector, revenue-required outside-CMS channels, and revenue-required channels not
assigned to an active group. Finance reconciliation issues remain sourced from
month-specific revenue endpoints. Month smart alerts also surface source-backed
revenue movement anomalies from SQL monthly revenue facts so finance users can
see channels whose current gross revenue moved materially from the prior month
before exporting.

### 4. Outside-CMS Monitor

Shows:

```text
Outside-CMS channels
Revenue-required status
Revenue source status
Missing official revenue
Recommended action
```

Foundation API:

```text
GET /channels/outside-cms
```

The endpoint is scoped by `analytics.view` using the same global, sector,
company, and channel boundaries as the channel registry list. It returns no
money amounts; it exposes operational revenue-source status so analysts and
managers can see which outside-CMS channels need CMS linking or official manual
revenue import.

### 5. Monthly Close

Sections:

```text
YouTube gross summary
Tax/deduction summary
AdSense payment summary
Bank/manual finance input
Gap analysis
Allocation method
Lock month
```

### 6. Export Center

Exports:

```text
Excel finance workbook
PDF executive report
Branded slide pack
Company report
Sector report
Channel report
```

## User experience rules

- User should get answer in 3 clicks or less after selecting month.
- Every table row can be expanded.
- Every number can be explained.
- Alerts appear before exports.
- Exports include warnings and confidence notes when needed.

## Visual reference

Two static HTML mockups live in `mockups/` as visual targets for Phase 5
implementation. Both render the same product surfaces (Revenue Command
Center, Channel Revenue Table, Company / Sector Comparison, Smart Issue
Panel, Outside-CMS Monitor, Monthly Close, Export Center) and the same
role-restricted views.

| Mockup | Stack | License posture |
|---|---|---|
| `mockups/ums-smart-revenue-command-center.html` | Anthropic Sans/Serif/Mono from `mockups/FontsPP/` | Canonical Anthropic-licensed reference. |
| `mockups/ums-smart-revenue-command-center-soft-dark.html` | Mona Sans + Monaspace Neon + Newsreader from `mockups/FontsGH/` | OFL-1.1 sibling; redistributable variant. |

Each mockup has matching QA screenshots in `mockups/qa/` (one per role
and section) captured by `mockups/qa/generate-screenshots.py` (canonical)
and `mockups/qa/generate-screenshots-soft-dark.py` (soft dark). Both
generators follow the same `roleSelect` + page-hash drive contract so
they stay parallel.

The canonical mockup remains the authoritative visual reference per
`DESIGN.md`. The soft-dark variant exists so the design can be shared
and reviewed externally without bundling proprietary fonts; it is not a
new design direction.
