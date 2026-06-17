# UMS Smart Revenue Control Center — Spec Pack

## Purpose
Build an internal, smart, numbers-first system for UMS YouTube operations.

The system answers:

- What did each channel/company/sector generate?
- What was finalized?
- What reached AdSense/payment?
- What was deducted?
- Why does the bank/payment amount differ?
- Which numbers are official, reconciled, allocated, or missing?

## Core product principle
Every number shown in the product must have:

1. **Source** — YouTube report, system-managed report, AdSense payment, manual finance input, or allocation model.
2. **Formula** — how the number was calculated.
3. **Confidence** — Official, Reconciled, Allocated, Estimated, Missing.
4. **Export value** — usable in Excel, PDF, and branded slide reports.

## Recommended architecture

```text
Operational DB / Warehouse = source of truth
SQL-backed query/read models = explanations, hierarchy, reconciliation, exports
Dashboard = smart UI + calculations + exports
```

Neo4j and graph projections are removed from the active architecture. Relationship, hierarchy, ownership, and issue-tracing views are served from SQL-backed read models with the same application permissions as finance and analytics APIs.

Migration `20260513_0002_retire_graph_permissions` dropped the retired `graph-read` scope and `graph.*` permissions. Any remaining deployment configuration referencing `graph-scope` must be removed.

## Files

| File | Purpose |
|---|---|
| `01_IMPLEMENTATION_PLAN.md` | Delivery phases and acceptance gates |
| `02_TARGET_ARCHITECTURE.md` | System architecture and shipped stack |
| `05_CONNECTORS_YOUTUBE_ADSENSE.md` | YouTube / AdSense connector scope |
| `06_CHANNEL_REGISTRY_GROUPS.md` | Channel registry and flexible grouping |
| `07_REVENUE_RECONCILIATION_ENGINE.md` | Gross/final/net revenue calculation engine |
| `08_CONFIDENCE_EXPLAINABILITY.md` | Confidence scoring and explanation logic |
| `09_SMART_DASHBOARD_UI.md` | Main UI pages and interactions |
| `10_EXPORTS_BRAND_REPORTS.md` | Excel, PDF, branded slides |
| `11_ACCESS_CONTROL_SECURITY.md` | Roles, permissions, audit controls |
| `12_BACKEND_API_SPEC.md` | Backend API specification |
| `13_SQL_DATA_MODEL.md` | Primary SQL tables and schema |
| `14_WORKFLOWS.md` | Monthly close and data workflows |
| `15_DELIVERY_BACKLOG.md` | Feature backlog by priority |
| `16_OPEN_DECISIONS.md` | Decisions to confirm before build |
| `17_MULTI_TENANT_ARCHITECTURE.md` | Postgres RLS, tenant isolation, role grants |
| `18_MULTI_CURRENCY_ENGINE.md` | Multi-currency rate store and conversion path |
| `security/ROLE_PERMISSION_MODEL.md` | Role catalog, permission list, gate registry |
| `security/PERMISSION_MATRIX.md` | Role → permission matrix |
| `_archived/` | Retired specs (Neo4j graph model and data-store docs) |
