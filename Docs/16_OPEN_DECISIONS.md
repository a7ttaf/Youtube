# Open Decisions

## Status (2026-06-21)

Reconciled through PR #132 plus the Google credential setup/smoke runbook
branch. Decisions that are now made are listed under "Closed (with PR ref)".
Items in the topic sections below remain genuinely open.

## Closed (with PR ref)

- ✅ **Backend stack: FastAPI** — chosen in PR #1; reaffirmed across S0,
  S1, S2 PRs.
- ✅ **Database (operational): PostgreSQL-only first** — operational
  schema is PostgreSQL with Alembic migrations from PR #1 onward;
  BigQuery warehouse remains future scope but no longer blocks any
  shipped work.
- ✅ **Graph database: none** — Neo4j retired entirely in PR #12.
- ✅ **Multi-tenant model: row-level `tenant_id` on operational tables,
  header-based resolver with contextvar** — implemented via the S2 stack
  (PRs #18 – #21, #36). S3 storage-layer hardening shipped: Postgres RLS
  with `app_tenant`/`app_platform` roles and `FORCE ROW LEVEL SECURITY` on
  all 25 tenant-scoped tables (PRs #85, #106; see `Docs/17_MULTI_TENANT_ARCHITECTURE.md`).
- ✅ **Export engine: Python libraries** — `openpyxl` / `reportlab`-class
  approach via the export artifacts in PR #9 + tenant-scoped export jobs
  in PR #36.
- ✅ **Multi-currency model: source-reported money is authoritative** — official
  finance values come from Google/YouTube/AdSense source reports in their
  reported currency. The earlier FX-rate-led design (manual rate table, provider
  sync, locked-month FX freeze) is retired; market-rate conversion is deferred to
  a future display-only spec and must never drive official totals. Established in
  the B1 pivot (PR #42); see `Docs/18_MULTI_CURRENCY_ENGINE.md`.

## Stack decisions (closed)

- ✅ **Job runner: in-process ThreadPoolExecutor** — Celery/RQ, Temporal,
  Airflow, and cloud scheduler were all deferred. A bounded in-process
  `ThreadPoolExecutor` (off by default via `connector_job_executor_enabled`)
  ships with PR #95. Durable out-of-process queue remains a future upgrade.

## Data decisions (open — blocked on owner-approved Google connector credentials)

The blocker is not a direct Gmail account link. UMS needs approved Google Cloud
project credential material: API-key-only access for YouTube Data API public
metadata where Google permits it, and official Google authorization-token
credentials/scopes for private YouTube Reporting, YouTube Analytics, and
AdSense account or revenue data.

The setup/smoke process is documented in
`Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md`; it prepares credential registration,
credential-only CLI smoke, audited token-refresh probing, and ingestion dry-run
smoke, but it does not close the data questions below until real
owner-approved Google credentials and reports are available.

- Exact list of YouTube report types available in UMS account.
- Which reports cover outside-CMS channels.
- Whether 70 outside-CMS channels can be linked to CMS later.
- Required historical period per report type.
- Whether old manual finance exports are available.

## Finance decisions

- ✅ **Default allocation method: gross_revenue_proportional** — shipped as the
  first committable method (PR #58/62); post_tax also available (PR #67);
  company_level, manual, and no_allocation shipped (PR #74/76).
- ✅ **Who can approve manual overrides** — Finance Admin, Finance Approver,
  or Super Owner (established by the auth policy and
  `Docs/security/ROLE_PERMISSION_MODEL.md`; `APPROVE_MANUAL_OVERRIDE` permission).
- Should unresolved payment gap appear as holding-level only or allocated to channels?
- What happens if bank amount is not available?
- Which Google/YouTube/AdSense report currency is authoritative when a source
  offers account currency, payment currency, and/or metric-level `currencyCode`?
- Which Google source wins when YouTube estimated revenue and AdSense
  settled/payment reports disagree for the same month?
- How should bank-side local-currency variance be explained when the official
  Google/AdSense source value is in a different currency?
- How to treat UAE USD account after activation?

## UI decisions

- First dashboard language: English only or bilingual labels?
- Required currency display format.
- Required management report structure.
- Brand identity template source.
- Which users can see the internal performance league?

## Suggested default decisions

```text
Backend: FastAPI                              [decided — PR #1]
Database: PostgreSQL + optional BigQuery       [PostgreSQL chosen; BigQuery deferred]
Graph database: none in active roadmap         [decided — PR #12]
Export engine: Python libraries                [decided — PR #9 + PR #36]
Allocation method: gross_revenue_proportional default; post_tax also available [shipped — PRs #58–#76]
Outside-CMS labels: show confidence clearly                   [open]
Bank input: manual first, automation later                    [foundation shipped — PR #29; automation open]
```
