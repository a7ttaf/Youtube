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
GET /revenue/months/{month}/payment-match?currency=USD
GET /revenue/months/{month}/bank-reconciliation
POST /revenue/months/{month}/bank-reconciliation
POST /revenue/channels/{channel_id}/months/{month}/explain?metric=adjusted_gross_revenue_usd
POST /revenue/recalculate
```

`GET /revenue/months/{month}/payment-match` is an implemented holding-level
finance read that compares selected monthly YouTube revenue facts against paid
AdSense payment rows for the same month. It requires global
`finance.view_revenue` and global `finance.view_finalized_payments`, audits both
`REVENUE_VIEWED` and `PAYMENT_VIEWED`, returns match status, totals, gap,
issues, and audit event metadata, and excludes non-paid AdSense rows from the
paid total while still reporting their count. This endpoint does not calculate
tax, bank gaps, net revenue, or allocation rules. Until exchange-rate support
exists, `currency` must be `USD`; payment rows in another currency are excluded
from the match and surfaced as reconciliation issues.

`POST /revenue/months/{month}/bank-reconciliation` records finance-provided
bank receipt metadata for a finance month. It requires
`finance.manage_bank_reconciliation` for the requested finance-month scope
(global grants satisfy that scope), a non-empty reason, and an unlocked month,
then audits `BANK_RECONCILIATION_RECORDED`. Rows are upserted by
`(month, bank_reference)` and store normalized USD receipt values supplied by
finance, transfer fee metadata, FX difference metadata, notes, source report
reference, and recorder metadata. The endpoint does not calculate exchange
rates, allocate transfer/FX gaps to channels, or calculate net revenue.

`GET /revenue/months/{month}/bank-reconciliation` is an implemented
holding-level finance read that compares paid USD AdSense payment metadata with
finance-provided normalized bank receipt rows for the same month. It requires
`finance.view_bank_reconciliation` and `finance.view_finalized_payments` for the
requested finance-month scope (global grants satisfy that scope), audits both
`BANK_RECONCILIATION_VIEWED` and `PAYMENT_VIEWED`, returns bank confirmation
status, paid amount, received
amount, month-level bank gap, transfer-fee and FX-difference totals, receipt
entries, issues, and audit event metadata. Non-paid AdSense rows and non-USD
payment rows are excluded from the paid USD comparison and reported as issues.

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
`revenue_required` that have no monthly revenue facts (`MISSING_REVENUE_FACTS`). Channels marked
performance-only or not revenue-required do not block month close.

Lock requests must not trust a prior `GET /finance-close/{month}/readiness`
response. They acquire a transaction-scoped finance-month advisory guard and
the `finance_month_close` row, then re-run readiness in the same transaction
with row locks on matching pending overrides, missing revenue-required channel
rows, and monthly revenue facts. At the default read-committed isolation level,
revenue fact and manual-override writes acquire the same month guard before
committing month-scoped changes, so they serialize with lock/unlock operations.
Channel registry state committed before the lock-time recheck is authoritative
for the close decision; registry changes committed later require an unlock and
new close cycle if they affect a locked month.

### AdSense

```http
POST /adsense/sync-payments
GET /adsense/payments?month=2026-03&limit=50&offset=0
```

`POST /adsense/sync-payments` is an implemented control-plane ingestion endpoint
for official AdSense payment metadata that has already been downloaded through
approved connector storage. It requires `connectors.run_jobs` on connector scope
`adsense`, requires a non-empty audit reason, and records `ADSENSE_PAYMENT_SYNCED`.
The endpoint accepts up to 100 payment objects and upserts by `(month,
payment_name)`, so connector reruns update the existing payment row instead of
duplicating it. Sync is rejected for locked finance months.

`GET /adsense/payments` requires global `finance.view_finalized_payments` and is
audited as `PAYMENT_VIEWED`. It returns payment metadata only; it does not expose
bank reconciliation data and does not calculate revenue.

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
