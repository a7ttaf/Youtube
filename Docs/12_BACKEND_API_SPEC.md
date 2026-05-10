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
/connectors
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
POST /revenue/channels/{channel_id}/months/{month}/explain?metric=adjusted_gross_revenue_usd
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

API group: `/connectors`.

```http
GET /connectors/credentials
POST /connectors/credentials
POST /connectors/jobs
```

`/connectors` is an implemented API group in this draft and owns credential-reference metadata plus connector job requests.

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
GET /exports?page=1&page_size=50
GET /exports/{export_id}
```

`GET /exports` is paginated. Query parameters are `page` (default `1`) and `page_size` (default `50`, maximum `100`). Requests above the maximum are rejected with `422`. Responses include paging metadata:

```json
{
  "items": [],
  "pagination": {
    "total_count": 0,
    "page": 1,
    "page_size": 50,
    "next_link": null
  }
}
```

### Audit

```http
GET /audit/events?page=1&page_size=50
```

Audit event reads require `audit.view`. Sensitive audit `details` are masked unless the caller also has `audit.view_sensitive_payloads`. Audit reads are themselves recorded as `AUDIT_LOG_VIEWED`.

`GET /audit/events` follows the same pagination contract as `GET /exports`: `page` defaults to `1`, `page_size` defaults to `50`, and `page_size` is capped at `100`. Older unpaginated examples are outdated; clients must iterate `next_link` or increment `page` until no next page is returned.

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
  "value": "184250.00",
  "currency": "USD",
  "confidence": "B_RECONCILED",
  "source": "reconciliation_engine",
  "locked": true,
  "explain_url": "/revenue/channels/{channel_id}/months/{month}/explain?metric=adjusted_gross_revenue_usd"
}
```

Money `value` fields are fixed-precision decimal strings with two fractional digits unless an endpoint documents a higher scale. Outdated floating-number examples such as `184250.0` must be migrated by clients before using finance values for reconciliation or exports.
