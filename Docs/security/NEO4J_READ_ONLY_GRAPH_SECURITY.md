# Retired Neo4j Security Note

## Status
Neo4j is retired from the active UMS Smart Revenue Control Center architecture.

Do not add dashboard graph credentials, graph-read scopes, graph-specific permissions, Cypher allowlists, graph sync jobs, or `/graph/*` APIs for the current foundation.

## Active Security Rule
Hierarchy, ownership, issue-tracing, and revenue-flow style views must be served from SQL/warehouse-backed APIs and must reuse the same authorization checks as the underlying finance, analytics, registry, and export data.
