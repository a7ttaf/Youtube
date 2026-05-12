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

The `/connectors` group is part of the implemented API surface and is detailed in the Connectors section below.

## Authentication and authorization source

The trusted gateway still supplies identity headers, but authorization can run in two modes:

- `headers`: bootstrap/test mode. The backend constructs the principal from `x-role`, `x-scope-type`, and `x-scope-id`.
- `database`: production-oriented mode. Set `UMS_AUTHZ_SOURCE=database` with `UMS_DATABASE_URL`; the backend loads the user, active role assignments, direct permission grants, and scopes from SQL. Header role/scope claims are ignored in this mode.

Database authorization rejects unknown users and disabled users before route code executes.

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

### Users and roles

```http
GET /users
GET /users?limit=50&cursor_email=admin@example.com&cursor_id=00000000-0000-0000-0000-000000000001
GET /users/{user_id}/access
POST /users
PATCH /users/{user_id}
POST /users/{user_id}/roles
POST /users/{user_id}/roles/{assignment_id}/revoke
POST /users/{user_id}/permissions
POST /users/{user_id}/permissions/{grant_id}/revoke
```

`GET /users` requires `users.manage` and returns a bounded, email-sorted page
of user accounts with optional status filtering. Pagination uses a keyset cursor:
when `pagination.has_more` is true, clients pass `pagination.next_cursor.email`
as `cursor_email` and `pagination.next_cursor.id` as `cursor_id` to continue
from the same normalized `(email, id)` position even if earlier-sorting accounts
are inserted between requests. `offset` remains accepted for compatibility and
empty-result handling, capped at `10000`, but cursor pagination is preferred for
iteration.
`GET /users/{user_id}/access` also requires `users.manage` before target-id
parsing and returns the user's active scoped role assignments and active direct
permission grants, including assignment/grant ids for follow-up revocation
workflows. Revoked historical rows remain available through audit/history
surfaces rather than the active profile.

User create/update endpoints require `users.manage` and a non-empty reason capped at 500 characters.
Service account creation and any `PATCH /users/{user_id}` updates to service
accounts, including status or other account fields, require Super Owner.
Every user account lifecycle change is audited as `USER_ACCOUNT_CHANGED`.
`POST /users` is not backed by a separate server-side idempotency-key store in
this phase; normalized email uniqueness is the retry guard, so duplicate create
retries return `409 Conflict` and clients must treat that as a possible prior
success after a timeout. User-account storage operations retry one transient
database disconnect, operational error, or timeout, then fail closed with `503`
instead of waiting indefinitely.

Role assignment and revocation both require `roles.assign`. Super Owner assignment requires an existing Super Owner. Finance roles require Finance Admin or Super Owner authority. Every assignment and revocation is audited as `USER_ROLE_CHANGED`.

Direct permission grants and revocations also require `roles.assign` and are audited as `USER_PERMISSION_CHANGED`. Finance permissions can only be granted or revoked by Finance Admin or Super Owner. Connector/raw-file permissions require Connector Admin or Super Owner. Administrative permissions such as `roles.assign`, `users.manage`, `platform.manage_settings`, and `audit.view_sensitive_payloads` require Super Owner. Direct grants must be scoped with a compatible scope type for the permission.

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
GET /finance-close/{month}/readiness
POST /finance-close/{month}/allocate
POST /finance-close/{month}/lock
POST /finance-close/{month}/unlock
```

The finance-close endpoints in the foundation control close state and
allocation-rule metadata only. They must not calculate or expose revenue values
without the reconciliation engine and finance permissions. Readiness checks are
required before locking and currently block on pending manual overrides,
unresolved reconciliation issues, and active registry channels marked
`revenue_required` that have no monthly revenue facts. Channels marked
performance-only or not revenue-required do not block month close.

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
POST /reports/raw-files
GET /reports/raw-files?source=youtube_reporting&report_month=2026-03&limit=50&offset=0
GET /reports/raw-files/{raw_file_id}
```

Raw report metadata records the immutable file reference before parsing. `POST /reports/raw-files` requires `source`, `report_type`, `report_month`, `storage_uri`, `checksum`, `parse_status`, and `reason`; responses include `id`, `source`, `report_type`, `report_month`, `storage_uri`, `checksum`, `parse_status`, `downloaded_by`, `downloaded_at`, and `audit_event`. `GET /reports/raw-files` is offset-paginated with `limit` capped at `100`, optional `source`, `report_type`, and `report_month` filters, and returns `items` plus `pagination.limit`, `pagination.offset`, `pagination.returned`, and `pagination.has_more`.

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
GET /audit/events?limit=50
GET /audit/events?limit=50&cursor_created_at=2026-05-10T12:00:00Z&cursor_id=00000000-0000-0000-0000-000000000001
```

Audit event reads require `audit.view`. Sensitive audit `details` are masked unless the caller also has `audit.view_sensitive_payloads`. Audit reads are themselves recorded as `AUDIT_LOG_VIEWED`.

`GET /audit/events` uses newest-first cursor pagination. `limit` defaults to `50` and is capped at `100`; when `pagination.has_more` is true, clients pass `pagination.next_cursor.created_at` as `cursor_created_at` and `pagination.next_cursor.id` as `cursor_id` to continue from the same `(created_at, id)` position. Older unpaginated and `page`/`page_size` examples are outdated. `AUDIT_LOG_VIEWED` records created by audit reads are stored, but this listing excludes them from the paginated result set so read-generated audit entries cannot shift the pages a client is iterating. `audit.view` and `audit.view_sensitive_payloads` only affect visibility within that filtered result set.

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
