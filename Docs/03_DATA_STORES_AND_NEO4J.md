# Data Stores and Retired Neo4j Decision

## Active Decision
Do not use Neo4j or a graph database in the active UMS Smart Revenue Control Center roadmap.

The primary data store remains SQL/PostgreSQL or warehouse tables. Relationship, hierarchy, ownership, issue tracing, explanation, and export views must be served from SQL-backed read models or warehouse projections.

## Store Responsibilities

| Store | Responsibility | Writes allowed? |
|---|---|---:|
| PostgreSQL | Users, roles, configuration, channel registry, month close, reconciliation controls | Yes |
| Warehouse / reporting tables | Raw reports, normalized facts, historical numbers, query read models | Yes |
| Object storage | Raw CSVs, report files, export files | Yes |

## Why Neo4j Is Retired
The first foundation is a finance/revenue numbers engine. Adding a graph projection creates extra synchronization, security, audit, and review surface before the core SQL-backed reconciliation system is complete.

Financial workflows need:

- deterministic calculations;
- month locking;
- audit trails;
- permission-filtered exports;
- SQL-style aggregation;
- raw report storage;
- reconciliation tables.

Those requirements are handled directly by SQL/warehouse models in the active roadmap.

## Replacement Pattern

```text
SQL / warehouse source tables
    ->
bounded read-model queries
    ->
backend permission guards
    ->
dashboard hierarchy, issue, explanation, and report views
```

No dashboard user or backend API should depend on a graph database for first-beta behavior.
