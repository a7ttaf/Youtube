# Open Decisions

## Status (2026-05-22)

Reconciled through PR #36 (S2 multi-tenant integration). Decisions that
are now made are listed under "Closed (with PR ref)". Items in the topic
sections below remain genuinely open.

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
  (PRs #18 – #21, #36). Storage-layer hardening (RLS + GUC + composite
  FKs + tenant-scoped unique keys per `Docs/17`) is a separate S3 spec,
  not yet written.
- ✅ **Export engine: Python libraries** — `openpyxl` / `reportlab`-class
  approach via the export artifacts in PR #9 + tenant-scoped export jobs
  in PR #36.

## Stack decisions

- Job runner: Airflow, Temporal, Celery/RQ, or cloud scheduler?

## Data decisions

- Exact list of YouTube report types available in UMS account.
- Which reports cover outside-CMS channels.
- Whether 70 outside-CMS channels can be linked to CMS later.
- Required historical period per report type.
- Whether old manual finance exports are available.

## Finance decisions

- Default allocation method: gross revenue or post-tax revenue?
- Should unresolved payment gap appear as holding-level only or allocated to channels?
- Who can approve manual overrides?
- What happens if bank amount is not available?
- Which FX rate should be used before UAE USD account is active?
- Which FX provider is authoritative for automated `currency_exchange_rates`
  imports, and what fallback policy applies when a requested rate is missing?
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
Allocation method: post-tax revenue proportional allocation   [target; not yet implemented]
Outside-CMS labels: show confidence clearly                   [open]
Bank input: manual first, automation later                    [foundation shipped — PR #29; automation open]
```
