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

## Decommissioning Neo4j / graph projection

Concrete rollout steps (run in order, one environment at a time):

1. Disable any `/graph/*` routes at the API gateway or router (return 410 Gone
   or remove the routes entirely). Confirm dashboard pages no longer link to
   graph views.
2. Stop and remove any scheduled `GraphSyncJob` / `SyncService` workers and
   cron entries that backfilled the graph projection.
3. Apply Alembic migration `20260513_0002_retire_graph_permissions` so the
   retired `graph-read` scope and `graph.*` permissions are removed from the
   SQL source of truth.
4. Revoke and rotate any Neo4j credentials stored in the secret manager;
   remove `graph-scope` configuration from all deployment manifests.
5. Run a smoke test on the SQL/warehouse read models for relationship,
   hierarchy, ownership, and issue-tracing views before tearing down the
   Neo4j instances themselves.
6. Decommission the Neo4j containers / clusters only after the smoke test
   passes and metrics confirm zero traffic to the retired routes.
