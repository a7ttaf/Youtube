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
Neo4j = read-only graph projection
Dashboard = smart UI + calculations + exports
```

Neo4j should not be the main business database. It should be used for graph exploration, relationship discovery, hierarchy visualization, and problem investigation.

## Files

| File | Purpose |
|---|---|
| `01_IMPLEMENTATION_PLAN.md` | Delivery phases and acceptance gates |
| `02_TARGET_ARCHITECTURE.md` | System architecture |
| `03_DATA_STORES_AND_NEO4J.md` | Database and Neo4j read-model design |
| `04_NEO4J_GRAPH_MODEL.md` | Nodes, relationships, Cypher examples |
| `05_CONNECTORS_YOUTUBE_ADSENSE.md` | YouTube / AdSense connector scope |
| `06_CHANNEL_REGISTRY_GROUPS.md` | Channel registry and flexible grouping |
| `07_REVENUE_RECONCILIATION_ENGINE.md` | Gross/final/net revenue calculation engine |
| `08_CONFIDENCE_EXPLAINABILITY.md` | Confidence scoring and explanation logic |
| `09_SMART_DASHBOARD_UI.md` | Main UI pages and interactions |
| `10_EXPORTS_BRAND_REPORTS.md` | Excel, PDF, branded slides |
| `11_ACCESS_CONTROL_SECURITY.md` | Roles, permissions, audit controls |
| `12_BACKEND_API_SPEC.md` | Backend API draft |
| `13_SQL_DATA_MODEL.md` | Primary SQL/warehouse tables |
| `14_WORKFLOWS.md` | Monthly close and data workflows |
| `15_DELIVERY_BACKLOG.md` | Feature backlog by priority |
| `16_OPEN_DECISIONS.md` | Decisions to confirm before build |
