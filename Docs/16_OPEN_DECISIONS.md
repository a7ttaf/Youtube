# Open Decisions

## Stack decisions

- Backend: FastAPI or NestJS?
- Warehouse: BigQuery or PostgreSQL-only first?
- Neo4j: AuraDB, Enterprise, or self-hosted?
- Job runner: Airflow, Temporal, Celery/RQ, or cloud scheduler?
- Export engine: Python libraries or dedicated reporting service?

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
- How to treat UAE USD account after activation?

## UI decisions

- First dashboard language: English only or bilingual labels?
- Required currency display format.
- Required management report structure.
- Brand identity template source.
- Which users can see the internal performance league?

## Neo4j decisions

- Use Bloom/Explore only, custom embedded graph only, or both?
- Which graph views are needed for first beta?
- Should graph show finance values to everyone or finance-only?
- Should graph be refreshed after every sync or only after month close?

## Suggested default decisions

```text
Backend: FastAPI
Database: PostgreSQL + optional BigQuery for warehouse
Neo4j: AuraDB or self-hosted Enterprise depending on data governance
Graph UI: Bloom/Explore for internal analysts + embedded graph for dashboard
Allocation method: post-tax revenue proportional allocation
Outside-CMS labels: show confidence clearly
Bank input: manual first, automation later
```
