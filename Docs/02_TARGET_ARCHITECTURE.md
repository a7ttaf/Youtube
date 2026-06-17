# Target Architecture

## Architecture summary

```text
Frontend Dashboard
    ↓
Backend API
    ↓
Smart Number Engine
    ├── YouTube Connector
    ├── AdSense Connector
    ├── Finance Input Module
    ├── Revenue Reconciliation Engine
    ├── Allocation Engine
    ├── Confidence Engine
    └── Export Engine
    ↓
Operational DB / Warehouse
```

## Recommended stack

| Layer | Shipped stack |
|---|---|
| Frontend | Vite 8 + React 19 + TypeScript 6 |
| Backend | Python 3.14 FastAPI + SQLAlchemy 2 + Alembic |
| App database | PostgreSQL 18 (sole source of truth) |
| Analytics warehouse | PostgreSQL (no separate warehouse) |
| Jobs | In-process `ThreadPoolExecutor` (bounded; off by default) |
| Object storage | Local file store / GCS (export artifact abstraction) |
| Exports | Excel templates, PDF generator, PPTX generator |

## System responsibilities

### Frontend
- Filter and select month, company, sector, channel, group, currency.
- Show numbers, explanation, confidence, and warnings.
- Trigger exports.
- Show hierarchy, ownership, issue, and revenue-flow views only from guarded backend APIs.

### Backend API
- Authentication and role enforcement.
- Data query layer.
- Calculation APIs.
- Export job creation.

### Smart Number Engine
- Normalizes raw data.
- Calculates gross/final/net values.
- Allocates deductions.
- Assigns confidence levels.
- Generates explanations.

### Operational DB / Warehouse
- Source of truth.
- Stores raw reports, normalized facts, month-close records, overrides, and locked values.
- Enforces tenant isolation via Postgres Row-Level Security: two non-superuser roles (`app_tenant` and `app_platform`) with per-table isolation policies on all 25 tenant-scoped tables, applied with `FORCE ROW LEVEL SECURITY` so the table owner is also policy-subject. See `Docs/17_MULTI_TENANT_ARCHITECTURE.md` for the full grant model and deployment preconditions.

### SQL-backed read models
- Serve hierarchy, ownership, issue, explanation, and reconciliation views from source-of-truth tables or warehouse projections.
- Apply the same organization and finance permissions as the underlying APIs.
- Do not introduce a separate graph database or dashboard-only data path.

## Non-negotiable design choices

- Never calculate finance numbers directly inside the UI.
- Do not add a Neo4j/graph database layer to the active architecture.
- Store raw source files before normalization.
- Keep locked finance results immutable unless admin unlocks with reason.
- Every manual override must be logged.
