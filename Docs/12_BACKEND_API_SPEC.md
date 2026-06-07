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
/exchange-rates  (legacy scaffold; not official finance source)
/audit
```

The `/connectors` group is part of the implemented API surface and is detailed in the Connectors section below.
The current `/exchange-rates` group is historical scaffolding only. Revised
B1 work must prioritize Google/YouTube/AdSense source-reported monetary
ingestion rather than expanding exchange-rate APIs.

## Authentication and authorization source

The trusted gateway still supplies identity headers, but authorization can run in two modes:

- `headers`: bootstrap/test mode. The backend constructs the principal from `x-role`, `x-scope-type`, and `x-scope-id`.
- `database`: production-oriented mode. Set `UMS_AUTHZ_SOURCE=database` with `UMS_DATABASE_URL`; the backend loads the user, active role assignments, direct permission grants, and scopes from SQL. Header role/scope claims are ignored in this mode.

Database authorization rejects unknown users and disabled users before route code executes.

## Example endpoints

### Channels

```http
GET /channels
POST /channels
PATCH /channels/{youtube_channel_id}/mapping
GET /channels/issues
GET /channels/outside-cms
```

`GET /channels/outside-cms` is an implemented monitor endpoint for active
channels with `cms_status=OUTSIDE_CMS`. It requires `analytics.view` for the
caller scope and returns only channels visible through the caller's global,
sector, company, or channel access. The response contains an `items` array with
channel identity, company, content owner, `revenue_required`,
`revenue_source_status`, `missing_official_revenue`, and `recommended_action`,
plus a `summary` with outside-CMS, revenue-required, and missing-official-revenue
counts. It does not expose revenue amounts or finalized payment data.

`GET /channels/issues` is an implemented metadata-only registry health feed for
the smart issue panel. It requires `analytics.view` and applies the same global,
sector, company, and channel visibility boundaries as `GET /channels`. It returns
`items` with channel identity, `issue_type`, severity, message, and recommended
action, plus a `summary` with total issue count, affected channel count, and
issue-type counts. The foundation checks currently cover missing company,
missing sector, revenue-required outside-CMS channels, and revenue-required
channels that are not in any active group. It does not expose revenue amounts,
payment data, or revenue-fact-backed reconciliation issues.

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
GET /revenue/months/{month}/smart-alerts
GET /revenue/months/{month}/net-revenue?scope_type=company&scope_id=123&currency=USD
POST /revenue/facts
GET /revenue/channels/{channel_id}/months/{month}/facts
POST /revenue/channels/{channel_id}/months/{month}/explain?metric=adjusted_gross_revenue_usd
POST /revenue/recalculate
```

`POST /revenue/facts` is an implemented connector-controlled import endpoint
for monthly channel revenue facts. It requires `connectors.run_jobs` for the
connector scope, validates connector/source-kind compatibility, rejects locked
finance months, and audits `REPORT_IMPORTED`. The payload accepts optional
official `shorts_revenue_usd`, `longform_revenue_usd`, and
`subscription_revenue_usd` values when supplied by YouTube/AdSense reports.
Each component must be a finite non-negative USD decimal and the known component
sum must not exceed `gross_revenue_usd`. Null component values mean the source
did not provide that breakdown; the backend does not infer missing format
revenue from gross revenue.

`GET /revenue/channels/{channel_id}/months/{month}/facts` is an implemented
guarded revenue read for channel/month source facts. It requires
`finance.view_revenue` for the channel scope, audits `REVENUE_VIEWED`, and
returns the same optional revenue-format component fields as stored in SQL.

`GET /revenue/months/{month}/payment-match` is an implemented holding-level
finance read that compares selected monthly YouTube revenue facts against paid
AdSense payment rows for the same month. It requires global
`finance.view_revenue` plus `finance.view_finalized_payments` for the requested
finance-month scope, audits both `REVENUE_VIEWED` and `PAYMENT_VIEWED`, returns
match status, totals, gap, issues, and audit event metadata, and excludes
non-paid AdSense rows from the paid total while still reporting their count.
This endpoint does not calculate tax, bank gaps, net revenue, allocation
rules, or market FX conversions. Until source-reported non-USD matching exists,
`currency` must be `USD`; payment rows in another currency are excluded from
the match and surfaced as reconciliation issues.

`POST /revenue/months/{month}/bank-reconciliation` records finance-provided
bank receipt metadata for a finance month. It requires
`finance.manage_bank_reconciliation` for the requested finance-month scope
(global grants satisfy that scope), a non-empty reason, and an unlocked month,
then audits `BANK_RECONCILIATION_RECORDED`. Rows are upserted by
`(month, bank_reference)` and store normalized USD receipt values supplied by
finance, transfer fee metadata, FX difference metadata, notes, source report
reference, and recorder metadata. The endpoint does not calculate market
exchange rates, derive official Google revenue from FX, allocate transfer/FX
gaps to channels, or calculate net revenue.

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

`GET /revenue/months/{month}/smart-alerts` is an implemented month-level issue
engine for the internal finance command center. It derives alerts only from SQL
source-of-truth data already stored by the backend: monthly revenue facts,
approved/pending manual overrides, official AdSense payment metadata,
finance-entered bank reconciliation receipt rows, and finance month-close
state. It requires global `finance.view_revenue`, global
`analytics.view_confidence`, `finance.view_finalized_payments` for the requested
finance-month scope, and `finance.view_bank_reconciliation` for the requested
finance-month scope. The response includes alert codes such as
`MISSING_REVENUE_SOURCE`, `PAYMENT_NOT_MATCHED`, `BANK_AMOUNT_MISSING`,
`UNEXPLAINED_GAP_HIGH`, `REVENUE_TREND_ANOMALY`, `MONTH_NOT_LOCKED`, and
`MANUAL_OVERRIDE_USED`. `REVENUE_TREND_ANOMALY` compares each channel's selected
primary current-month SQL revenue fact with the selected primary previous-month
SQL revenue fact and reports only channel ids plus current/prior gross revenue
and percent movement. Reads are audited as `REVENUE_VIEWED`, `PAYMENT_VIEWED`, and
`BANK_RECONCILIATION_VIEWED`. This endpoint does not calculate net revenue or
allocate bank gaps.

`GET /revenue/months/{month}/net-revenue` is an implemented read-only net
revenue foundation for `global`, `sector`, `company`, and `channel` scopes. It
requires `finance.view_revenue` and `analytics.view_confidence` for the
requested scope, audits `REVENUE_VIEWED`, and currently supports `USD` only. It
uses the selected primary SQL revenue fact's official `net_revenue_usd` plus
approved manual revenue overrides. Missing source net values are returned as
`NET_REVENUE_SOURCE_MISSING` channel issues and counted at month level. The
endpoint does not persist calculated rows, allocate bank/payment gaps, invent
tax values, or depend on a graph database.

`POST /revenue/recalculate` is an implemented dry-run recalculation request
foundation for allocation-method review. The request includes `month`,
`allocation_method`, `scope_type`, optional `scope_id`, `currency`, `dry_run`,
and `reason`. It requires `finance.view_revenue` for the selected data scope
and `finance.change_allocation_rule` for the requested finance month, then
audits `RECALCULATION_REQUESTED`. In the current foundation `dry_run` must be
`true`: the response validates the allocation method, reports scoped source
coverage counts, reports blockers such as missing net revenue for post-tax
methods, and returns `NO_WRITES_PERFORMED`. It intentionally does not persist
calculated revenue rows, apply allocation deltas, invent tax data, or mutate
month-close allocation metadata.

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
without the reconciliation engine and finance permissions. `GET /finance-close/{month}`
requires `finance.view_revenue` on any grantable revenue scope (`global`,
`sector`, `company`, or `channel`) because close status is month-wide control
metadata rather than scoped revenue data. Readiness checks are required before
locking and currently block on pending manual overrides,
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
The endpoint accepts up to 100 payment objects. Every payment must include a
non-blank `source_account_id`; `accounts/{id}` is canonicalized to `{id}`, while
blank, whitespace-padded, or malformed account paths are rejected with `422`.
Rows are upserted by `(source_account_id, month, payment_name)`, so connector
reruns update the existing account-scoped payment row instead of duplicating it.
Sync is rejected for locked finance months.

`GET /adsense/payments` requires global `finance.view_finalized_payments` when
listing all months. When `month` is provided, the same permission on that
`finance-month` scope is sufficient and the read is audited as `PAYMENT_VIEWED`
for that scope. It returns payment metadata only; it does not expose bank
reconciliation data and does not calculate revenue.

### Exchange rates (legacy scaffold)

```http
POST /exchange-rates/sync
GET /exchange-rates/latest?base_currency=EUR&quote_currency=USD&as_of_date=2026-04-22&provider_key=ecb
```

These endpoints exist as pre-S2 scaffolding around `currency_exchange_rates`.
They are not the B1 path for official finance values and must not be expanded
into `MANAGE_FX_RATES`, manual uploads, ECB/exchangerate provider sync, or
locked-month FX behavior. Official revenue, payment, tax, deduction, and
reconciliation values must come from Google/YouTube/AdSense source reports and
finance-entered bank evidence. Market FX rates may later support display-only
conversion if the response clearly labels the converted amount as non-official.

### Connectors

API group: `/connectors`.

```http
GET /connectors/credentials
POST /connectors/credentials
POST /connectors/credentials/{connector_key}/{account_id}/test
POST /connectors/jobs
GET /connectors/runs?connector_key=youtube-reporting&account_id=acct-1&limit=50
GET /connectors/runs?limit=50&cursor_started_at=2026-05-10T12:00:00Z&cursor_id=<uuid>
```

`/connectors` is an implemented API group in this draft and owns credential-reference metadata plus connector job requests.

Connector credential responses expose metadata only, never raw credential material or secret references.

`GET /connectors/runs` lists connector run history (read-only operational
metadata) and requires `connectors.view_health`. It is tenant-scoped, accepts
optional `connector_key`/`account_id` filters, and uses newest-first
(`started_at`, `id`) cursor pagination: `limit` defaults to `50`, capped at
`100`; when `pagination.has_more` is true, pass `pagination.next_cursor.started_at`
as `cursor_started_at` and `pagination.next_cursor.id` as `cursor_id`. Supplying
only one half of the cursor returns `422`. Each item exposes `connector_key`,
`account_id`, `report_month`, `triggered_by_user_id`, `started_at`,
`finished_at`, `status` (`RUNNING`/`SUCCEEDED`/`PARTIAL`/`FAILED`), the
`counts` breakdown, and `error_summary`. The endpoint performs no audit write.

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

Future Google source-ingestion endpoints should expose source rows linked to
these raw files. Those rows preserve Google-reported amounts, currencies,
account/report identity, period boundaries, and raw payload references before
any normalized finance facts are written.

### Exports

```http
POST /exports
GET /exports?limit=50&offset=0
GET /exports/{export_id}
GET /exports/{export_id}/finance-workbook-preview
GET /exports/{export_id}/finance-workbook.xlsx
GET /exports/{export_id}/executive.pdf
GET /exports/{export_id}/branded-slide-pack.pptx
```

`POST /exports`, `GET /exports/{export_id}`, and the `GET /exports` list
include `scope_channel_ids`: an array of YouTube channel IDs frozen at job
creation time for non-global exports (sector, company, channel, group). The
field is `null` for `scope_type='global'` jobs and for pre-snapshot legacy
rows. Subsequent edits to the source group, sector, or company membership do
not change the data returned for the same `export_id`, keeping finance
numbers deterministic per export.

`GET /exports` is paginated. Query parameters are `limit` (default `50`, maximum `100`) and `offset` (default `0`). Requests above the maximum are rejected with `422`. Responses include paging metadata:

```json
{
  "items": [],
  "pagination": {
    "limit": 50,
    "offset": 0,
    "returned": 0,
    "has_more": false
  }
}
```

`GET /exports/{export_id}/finance-workbook-preview` is an implemented read-only
foundation for future `FINANCE_EXCEL` generation. It requires `exports.revenue`
and `finance.view_revenue` for the export scope, plus
`finance.view_finalized_payments` and `finance.view_bank_reconciliation` on the
requested finance-month scope. The endpoint returns a
`FINANCE_EXCEL_WORKBOOK_PREVIEW` payload with the planned sheet manifest,
executive summary, and source summaries from SQL-backed revenue facts, manual
overrides, AdSense payment metadata, bank reconciliation rows, finance close
state, payment matching, bank confirmation, net revenue, and smart alerts. It
uses month-wide AdSense payment and bank reconciliation rows only for global
exports; scoped exports keep those summaries empty until payment/bank cash can
be attributed below the month level. Group exports audit revenue reads per
member channel. It
audits reads as `REVENUE_VIEWED`, `PAYMENT_VIEWED`, and
`BANK_RECONCILIATION_VIEWED`. It does not create an XLSX/PDF/slide artifact,
does not update `file_url`, and does not mark the export job complete.

`GET /exports/{export_id}/finance-workbook.xlsx` uses the same permission checks
and SQL source summaries as the preview endpoint, then generates an on-demand
XLSX response with media type
`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` and an
attachment filename. It audits `REVENUE_VIEWED`, `PAYMENT_VIEWED`,
`BANK_RECONCILIATION_VIEWED`, and `EXPORT_DOWNLOADED`. The endpoint persists
the generated workbook through the configured export artifact store, records a
`file-store://exports/{export_id}/{filename}` `file_url`, filename, content type,
byte size, SHA-256 checksum, and marks the export job `COMPLETED`. If artifact
storage fails, the endpoint marks the job `FAILED`, records `failure_reason`,
returns `503`, and does not emit `EXPORT_DOWNLOADED`.

`GET /exports/{export_id}/executive.pdf` supports `EXECUTIVE_PDF` export jobs.
It uses the same finance export, revenue visibility, finalized-payment, and
bank-reconciliation checks as the workbook download, then generates an
on-demand PDF response with media type `application/pdf` and an attachment
filename. The report sections are sourced from SQL-backed net revenue, payment
matching, bank reconciliation, finance month-close state, and smart-alert
summaries. It audits `REVENUE_VIEWED`, `PAYMENT_VIEWED`,
`BANK_RECONCILIATION_VIEWED`, and `EXPORT_DOWNLOADED`. The endpoint persists
the generated PDF through the same artifact store and marks the export job
`COMPLETED` with artifact metadata.

`GET /exports/{export_id}/branded-slide-pack.pptx` supports
`BRANDED_SLIDE_PACK` export jobs. It uses the same finance export, revenue
visibility, finalized-payment, and bank-reconciliation checks as the workbook
download, then generates an on-demand PPTX response with media type
`application/vnd.openxmlformats-officedocument.presentationml.presentation` and
an attachment filename. The 10-slide deck is sourced from SQL-backed net
revenue, payment matching, bank reconciliation, finance month-close state, and
smart-alert summaries. It audits `REVENUE_VIEWED`, `PAYMENT_VIEWED`,
`BANK_RECONCILIATION_VIEWED`, and `EXPORT_DOWNLOADED`. The endpoint persists
the generated deck through the same artifact store and marks the export job
`COMPLETED` with artifact metadata.

### Audit

```http
GET /audit/events?limit=50
GET /audit/events?limit=50&cursor_created_at=2026-05-10T12:00:00Z&cursor_id=00000000-0000-0000-0000-000000000001
GET /audit/events/export?event_type=REVENUE_VIEWED&entity_type=channel&entity_id=chan-1
```

Audit event reads require `audit.view`. Sensitive audit `details` are masked unless
the caller also has `audit.view_sensitive_payloads`. Audit reads are themselves
recorded as `AUDIT_LOG_VIEWED`.

`GET /audit/events/export` returns the current filtered slice as CSV (`text/csv`)
with a `Content-Disposition: attachment; filename="audit-events.csv"` header. It
accepts the `event_type`, `entity_type`, and `entity_id` filters only — **no
`cursor_created_at`/`cursor_id`/`limit` params** (any such params are ignored). The
export always starts from the newest event, never the caller's loaded-page state.
Rows are gathered up to a fixed cap of **10,000**; when more rows remain beyond the
cap the response sets `X-Truncated: true`. The CSV columns are, in order:
`created_at` (ISO-8601), `event_type`, `user_id`, `entity_type`, `entity_id`,
`scope_type`, `scope_id`, `request_id`, `reason`, `sensitive`, `details_redacted`,
`details`. Booleans render lowercase; `details` is stable compact JSON
(`sort_keys=True`) for visible rows and the **empty string** for redacted rows
(`details_redacted=true`). String cells beginning with `=`, `+`, `-`, `@`, tab, or
CR are prefixed with an apostrophe to neutralize spreadsheet formula injection.
The export rows are materialized before the download is recorded, so the CSV never
contains its own event; one `EXPORT_DOWNLOADED` event (entity_type
`audit_events_export`) is then written with the filter set, returned row count,
`truncated`, and `details_redacted` flags.

`GET /audit/events` uses newest-first cursor pagination. `limit` defaults to `50` and is capped at `100`; when `pagination.has_more` is true, clients pass `pagination.next_cursor.created_at` as `cursor_created_at` and `pagination.next_cursor.id` as `cursor_id` to continue from the same `(created_at, id)` position. Older unpaginated and `page`/`page_size` examples are outdated. `AUDIT_LOG_VIEWED` records created by audit reads are stored, but this listing excludes them from the paginated result set so read-generated audit entries cannot shift the pages a client is iterating. `audit.view` and `audit.view_sensitive_payloads` only affect visibility within that filtered result set.

## API rules

- Backend enforces permissions.
- Backend does not expose graph/Neo4j endpoints in the active roadmap.
- Every money API must return source currency, confidence, and source metadata.
- Currency parameters are allowed only where implemented. If a response applies
  display-only conversion, it must expose the native/source amount separately
  and label the conversion as non-official.
- Export APIs may run async jobs or return guarded on-demand artifacts,
  depending on the endpoint contract.

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
