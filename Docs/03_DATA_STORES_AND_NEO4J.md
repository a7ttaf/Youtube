# Data Stores and Neo4j Read-Model

## Decision
Use Neo4j, but only as a **read-only graph projection**.

The primary data store remains SQL/warehouse. Neo4j is used for:

- visualizing UMS hierarchy;
- tracing channel → company → sector → payment/month;
- finding missing data relationships;
- exploring outside-CMS revenue problems;
- running graph algorithms later if useful.

## Store responsibilities

| Store | Responsibility | Writes allowed? |
|---|---|---:|
| PostgreSQL | Users, roles, configuration, channel registry, month close | Yes |
| Warehouse / reporting tables | Raw reports, normalized facts, historical numbers | Yes |
| Object storage | Raw CSVs, report files, export files | Yes |
| Neo4j | Read-only graph copy for visualization and relationship queries | Sync job only |

## Why not Neo4j as main DB?

Financial systems need:

- deterministic calculations;
- month locking;
- audit trails;
- exports;
- SQL-style aggregation;
- raw report storage;
- reconciliation tables.

A relational/warehouse model is better for this. Neo4j is better for relationships and graph exploration.

## Neo4j sync design

```text
SQL / warehouse source tables
    ↓
Graph projection job
    ↓
Upsert nodes and relationships into Neo4j
    ↓
Dashboard calls backend /graph/* APIs
    ↓
Backend queries Neo4j with a read-only credential
```

## Sync frequency

| Data type | Sync style |
|---|---|
| Organization/channel registry | On change + nightly |
| Monthly revenue facts | After revenue normalization |
| Month-close values | After close/recalculation |
| Alerts/issues | After data quality checks |
| Payments | After AdSense sync |

## Read-only enforcement

- Dashboard code must not connect to Neo4j directly; all graph reads go through backend `/graph/*` APIs.
- Backend graph APIs should use a read-only Neo4j user and enforce permissions, auditing, and scope filtering.
- Only the sync service should have write privileges.
- Users should not directly edit graph data.
- Graph changes must come from source tables.

## Better option to check

Neo4j is the right default for this project. The better setup is not replacing Neo4j; it is combining:

```text
Neo4j Bloom/Explore = internal graph exploration
Neo4j Visualization Library = embedded custom graph fed by backend /graph/* APIs
```

Use Bloom/Explore for analysts and admins. Use custom embedded graph views for management pages.
