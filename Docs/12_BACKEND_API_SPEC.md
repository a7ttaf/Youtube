# Backend API Draft

## Purpose
Define initial API endpoints for the UMS Smart Revenue Control Center.

## API groups

```text
/auth
/users
/org-units
/channels
/groups
/reports
/revenue
/finance-close
/adsense
/exports
/graph
/audit
```

## Example endpoints

### Channels

```http
GET /channels
GET /channels/{channel_id}
POST /channels
PATCH /channels/{channel_id}
GET /channels/issues
GET /channels/outside-cms
```

### Groups

```http
GET /groups
POST /groups
PATCH /groups/{group_id}
POST /groups/{group_id}/members
DELETE /groups/{group_id}/members/{channel_id}
```

### Revenue

```http
GET /revenue/monthly?month=2026-03&scope_type=company&scope_id=123&currency=USD
GET /revenue/channels?month=2026-03&group_id=abc&currency=USD
GET /revenue/explain?month=2026-03&entity_type=channel&entity_id=UCxxxx&metric=net_revenue
POST /revenue/recalculate
```

### Finance close

```http
GET /finance-close/{month}
POST /finance-close/{month}/allocate
POST /finance-close/{month}/lock
POST /finance-close/{month}/unlock
```

The finance-close endpoints in the foundation control close state and allocation-rule metadata only. They must not calculate or expose revenue values without the reconciliation engine and finance permissions.

### AdSense

```http
GET /adsense/payments
POST /adsense/sync-payments
```

### Connectors

```http
GET /connectors/credentials
POST /connectors/credentials
POST /connectors/jobs
```

Connector credential responses expose metadata only, never raw credential material or secret references.

### Reports ingestion

```http
POST /reports/youtube/sync
GET /reports/youtube/runs
GET /reports/youtube/runs/{run_id}
```

### Exports

```http
POST /exports
GET /exports
GET /exports/{export_id}
```

### Graph

```http
GET /graph/hierarchy
GET /graph/revenue-flow?month=2026-03&scope_type=company&scope_id=123
GET /graph/issues?month=2026-03
GET /graph/outside-cms
```

## API rules

- Backend enforces permissions.
- UI cannot directly access Neo4j for restricted views.
- Every money API must support currency parameter.
- Every money API must return confidence and source metadata.
- Export APIs run async jobs.

## Standard money response

```json
{
  "value": 184250.0,
  "currency": "USD",
  "confidence": "B_RECONCILED",
  "source": "reconciliation_engine",
  "locked": true,
  "explain_url": "/revenue/explain?..."
}
```
