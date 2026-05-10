# Neo4j Read-Only Graph Security

## Decision
Neo4j is a read-only projection layer for dashboard users. It is not the financial source of truth and cannot grant access independently from the application permission model.

## Access Pattern
Dashboard users must call backend `/graph/*` APIs. The backend:
1. resolves the authenticated user into a `UserPrincipal`;
2. builds the requested `AccessScope`;
3. checks `graph.view`;
4. checks `graph.view_finance` plus `finance.view_revenue` when graph rows contain money values;
5. executes a named read-only query through a read-only Neo4j credential;
6. post-filters returned nodes by channel/company/sector scope before returning data to the UI;
7. writes an audit event for sensitive finance graph reads.

## Credential Separation
| Credential | Allowed Use |
|---|---|
| `neo4j_sync_writer` | Graph projection job only. Can upsert projected data from SQL/warehouse. |
| `neo4j_dashboard_reader` | Backend graph APIs only. Read-only. No dashboard write access. |
| `neo4j_admin` | Technical administration only. Not used by application request handlers. |

## Query Rules
- Dashboard query names must resolve to vetted read-only Cypher templates.
- Query text must reject write tokens such as `CREATE`, `MERGE`, `SET`, `DELETE`, `REMOVE`, and `DROP`.
- Scope parameters are mandatory for organization-specific graph views.
- Finance graph views cannot be served to users who only have analytics permissions.
- Backend filtering remains required even when Cypher includes scope filters.

## Leak Prevention Example
A company manager assigned to `company:company-tv-a` may request a company hierarchy graph for that company. If Neo4j accidentally returns a node from `company-news-a`, the backend read-only service filters the row out before the response is returned.

## Implementation Hooks
- `backend/ums_smart_revenue/graph/readonly_service.py` enforces application permissions and post-query scope filtering.
- `backend/ums_smart_revenue/graph/cypher.py` stores named read-only query templates and a query safety assertion.
- `tests/graph/test_readonly_service.py` verifies that finance graph reads reject users without finance visibility and do not leak another company.
