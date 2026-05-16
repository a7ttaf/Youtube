# Archived Documentation

Files in this directory are no longer part of the active project scope. They are kept for historical reference only.

## Retired 2026-05-15 — Neo4j removal

The Neo4j graph database was removed from the project scope. Hierarchy and relationship queries are now served by PostgreSQL recursive CTEs and materialized views.

The following documents describe the dropped Neo4j layer and are archived here:

- `03_DATA_STORES_AND_NEO4J.md` — original two-store architecture (SQL + Neo4j).
- `04_NEO4J_GRAPH_MODEL.md` — node/relationship model intended for Neo4j.
- `NEO4J_READ_ONLY_GRAPH_SECURITY.md` — Neo4j-specific role/security model (`neo4j_sync_writer`, `neo4j_dashboard_reader`, `neo4j_admin`).

The retirement is recorded in [CHANGELOG.md](../../CHANGELOG.md).

If a future need for graph traversal arises, prefer **PostgreSQL recursive CTEs** for hierarchy questions and **materialized views** for payment-flow projections before reintroducing a graph database.
