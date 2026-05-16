# Retired Neo4j Graph Model

## Status
The Neo4j graph model is retired from the active UMS Smart Revenue roadmap.

Do not implement nodes, relationships, Cypher queries, graph sync jobs, graph-specific permissions, or dashboard graph endpoints for the current foundation.

## Active Replacement
Use SQL/warehouse-backed read models for:

- holding, sector, company, channel, and group hierarchy;
- channel-to-company ownership tracing;
- month, payment, bank reconciliation, and issue summaries;
- outside-CMS problem lists;
- explanation chains for revenue, payment, deduction, and net values.

These views must use the same backend permission checks as the underlying finance and analytics APIs.
