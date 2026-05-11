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
    ↓
Read-only Graph Sync
    ↓
Neo4j Graph Projection
```

## Recommended stack

| Layer | Recommended option |
|---|---|
| Frontend | Next.js / React |
| Backend | Python FastAPI or Node.js/NestJS |
| App database | PostgreSQL |
| Analytics warehouse | BigQuery or PostgreSQL partitioned tables |
| Graph read-model | Neo4j AuraDB / Neo4j Enterprise / self-hosted Neo4j |
| Jobs | Celery/RQ, Temporal, Airflow, or Cloud Scheduler + workers |
| Object storage | Google Cloud Storage / S3-compatible storage |
| Exports | Excel templates, PDF generator, PPTX generator |

## System responsibilities

### Frontend
- Filter and select month, company, sector, channel, group, currency.
- Show numbers, explanation, confidence, and warnings.
- Trigger exports.
- Show graph exploration pages.

### Backend API
- Authentication and role enforcement.
- Data query layer.
- Calculation APIs.
- Export job creation.
- Neo4j query proxy for allowed graph views.

### Smart Number Engine
- Normalizes raw data.
- Calculates gross/final/net values.
- Allocates deductions.
- Assigns confidence levels.
- Generates explanations.

### Operational DB / Warehouse
- Source of truth.
- Stores raw reports, normalized facts, month-close records, overrides, and locked values.

### Neo4j read-model
- Receives projected graph data only.
- Supports relationship exploration and graph visualizations.
- Does not own business truth.

## Non-negotiable design choices

- Never calculate finance numbers directly inside the UI.
- Never use Neo4j as source of truth for financial calculations.
- Store raw source files before normalization.
- Keep locked finance results immutable unless admin unlocks with reason.
- Every manual override must be logged.
