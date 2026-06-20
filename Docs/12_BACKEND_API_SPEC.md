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
PATCH /channels/{youtube_channel_id}/content-owner
GET /channels/issues
GET /channels/outside-cms
```

`PATCH /channels/{youtube_channel_id}/content-owner` sets or clears a channel's
CMS `content_owner_id` — the value `list_target_channels` matches against the
connector account id to choose which channels a revenue pull targets. It requires
`registry.manage_channels` (not `registry.manage_org_mapping`) at the target
channel scope, checked before the existence check so an unauthorized caller never
learns whether a channel exists (403 before any 404). The request body is
`{"content_owner_id": str | null, "reason": str}` where `content_owner_id` is
required-to-be-present but nullable: sending `null` clears the CMS content owner,
while a present-but-blank string is rejected as 422 (not silently coerced to
null). Authorization is verified before existence, so an unauthorized caller
always gets 403 regardless of whether the channel exists; an authorized caller
targeting a missing channel gets 404. A registry validation failure surfaces as
422, mirroring `POST /channels` and `PATCH .../mapping`. Re-submitting the
current value is a no-op: it writes nothing and returns 200 with
`audit_event: null` so idempotent retries do not produce a misleading audit
event. An applied change records a `CHANNEL_UPDATED` audit event tagged with
`permission=registry.manage_channels` (via an explicit permission override, so
permission-based audit filtering attributes the write to the permission that
authorized it), carrying `old_content_owner_id`/`new_content_owner_id` in the
details and the required `reason`. It applies to future ingestion targeting only;
it performs no locked-month guard and never rewrites a closed month's finance
attribution.

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
GET /revenue/scopes
POST /revenue/facts
GET /revenue/channels/{channel_id}/months/{month}/facts
POST /revenue/channels/{channel_id}/months/{month}/explain?metric=adjusted_gross_revenue_usd
POST /revenue/recalculate
GET /revenue/source-rows?month=2026-03&source_system=adsense_management&limit=50
GET /revenue/source-rows?month=2026-03&cursor_ingested_at=2026-05-10T12:00:00Z&cursor_id=<uuid>
GET /revenue/source-rows/{id}
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

Monthly channel revenue facts are also produced by the connector run path
without going through `POST /revenue/facts`: after a live run upserts source
rows, `connectors/runs/orchestrator.run_one` invokes the post-run normalizer
(`finance/google_source_normalizer`), which upserts facts and emits one
`REPORT_IMPORTED` audit row per created/updated fact. These run-driven rows
carry `triggered_by_run_id`, `triggered_by_connector_key`, and
`triggered_by_account_id` in the audit `details` (distinguishing them from the
`POST /revenue/facts` import path) and are scoped to the fact's finance-month.
When the post-run projection fails on a non-lock error, the run is rewritten to
`FAILED` and a `CONNECTOR_JOB_RUN` audit row with `lifecycle="PROJECTION_FAILED"`
is recorded. Locked finance months are never overwritten. On RLS-enforced
Postgres these platform-only writes (`audit_logs`,
`monthly_channel_revenue_facts`, `finance_month_close`) execute on the
privileged `app_platform` lane via `db.lane.platform_lane`. Only three steps
stay on the tenant `app_tenant` lane: the credential read, the LOCKED-month
prefilter `SELECT`, and the post-loop deferred Analytics stale-row flush. Each
per-report ingest transaction (raw files, source rows, `mark_parsed`, and the
in-savepoint stale deletes) is itself elevated, because its `DOWNLOADED` /
`PARSED` / `FAILED` audit edges (`audit_logs`, platform-only) commit atomically
with the ingest evidence in the same transaction.

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
finance-entered bank reconciliation receipt rows, finance month-close state,
and finance-month-scoped connector audit edges for projection-skipped source
rows. It requires global `finance.view_revenue`, global
`analytics.view_confidence`, `finance.view_finalized_payments` for the requested
finance-month scope, and `finance.view_bank_reconciliation` for the requested
finance-month scope. The response includes alert codes such as
`MISSING_REVENUE_SOURCE`, `CHANNELS_MISSING_REVENUE_FACTS`,
`SOURCE_ROWS_SKIPPED`, `PAYMENT_NOT_MATCHED`, `BANK_AMOUNT_MISSING`,
`UNEXPLAINED_GAP_HIGH`, `REVENUE_TREND_ANOMALY`, `MONTH_NOT_LOCKED`, and
`MANUAL_OVERRIDE_USED`.
`CHANNELS_MISSING_REVENUE_FACTS` flags active, revenue-required channels that
have no revenue fact for the month (per-channel coverage, distinct from the
month-level `MISSING_REVENUE_SOURCE`); its details report `channel_count` and a
sorted `sample_channel_ids` list capped at 20. `SOURCE_ROWS_SKIPPED` is gated
on global `audit.view` so the audit-derived signal cannot leak to viewers
without explicit audit authorization; without that permission the alert is
omitted entirely. When `audit.view` is granted but `audit.view_sensitive_payloads`
is not, the alert surfaces only the total `skipped_count` and the per-reason
breakdown (`skipped_by_reason`) is redacted to an empty map, mirroring the
redaction rule applied to `/audit/events`. The latest
`CONNECTOR_JOB_RUN` audit edge with `lifecycle=ROWS_SKIPPED` for the requested
finance month is the only signal consumed; historical or re-run edges do not
compound, and `skipped_count` is reconciled against `sum(skipped_by_reason)`
via `max()` so the details are internally consistent. `REVENUE_TREND_ANOMALY` compares
each channel's selected primary current-month SQL revenue fact with the
selected primary previous-month
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

`GET /revenue/scopes` is an implemented read-only metadata helper that powers
the Command Center rollup scope selector. It requires `finance.view_revenue`
and is fail-closed: a disabled principal, or one with no active
`finance.view_revenue` grant in any scope, returns `403`
(`Missing permission: finance.view_revenue`) — never a silent empty list. It
takes no path or query parameters and performs **no audit write** (it is a
metadata helper like `GET /org-units`, not a revenue-number disclosure). The
response is `{"scopes": [{"scope_type", "scope_id", "label"}, ...]}` and
contains **only** the rollup scopes the caller's `finance.view_revenue` grants
authorize, so the selector can never offer an out-of-scope org unit
(org-structure leak). These scopes are readable for the standard finance roles,
which couple `finance.view_revenue` with the `finance.view_confidence` (at the
scope) and `finance.view_finalized_payments` (at the finance month) that the
rollup read also requires. (A hand-crafted `finance.view_revenue`-only grant
could be offered a scope whose rollup read then `403`s; the Command Center
degrades that to an in-card "No permission" state — it is not a leak.) A global
`finance.view_revenue`
grant yields the `global` option (`scope_id: null`) plus every active sector and
company; a sector grant yields that sector plus its member companies; a company
grant yields that company only (it does not confer the sector). An active,
non-empty channel group yields a `group` option when every member channel is
covered by the caller's `finance.view_revenue` grants; there is no persisted
group grant scope. The `global` option is present only when a global grant
exists. Names resolve through the org-unit or channel-group reader with a raw-id
fallback for org units, and the list is deterministically ordered (global first,
then sectors by name, companies by name, and groups by name).

`POST /revenue/recalculate` is an implemented recalculation endpoint that supports
both a dry-run preview and a committed write path. The request includes `month`,
`allocation_method`, `scope_type`, optional `scope_id`, `currency`, `dry_run`,
`idempotency_key`, and `reason`. It always requires `finance.view_revenue` for the
selected data scope (channel-group scopes require every member channel to be
covered) and `finance.change_allocation_rule` for the requested `finance_month`.
`allocation_method=manual` is accepted for dry-run previews
(`dry_run=true`) but rejected with HTTP 422 on committed writes (`dry_run=false`);
manual allocations require explicit lines and must use the dedicated commit
endpoint (`POST /revenue/months/{month}/account-allocations/commit`). With `dry_run=true`
the response validates the allocation method, reports scoped source coverage
counts, reports blockers (e.g. missing net revenue for post-tax methods), and
returns `NO_WRITES_PERFORMED`; it does not persist rows, apply allocation deltas,
invent tax data, or mutate month-close metadata. With `dry_run=false` the endpoint
additionally requires `finance.view_finalized_payments` at the `finance_month`
scope, enforces `scope_type=global` and a non-empty `idempotency_key`, then
commits a versioned allocation snapshot via the committed-allocation repository.
On a fresh commit (no existing run for the idempotency key) the blocking-issues
pre-flight gate runs (409 `BLOCKED_BY_ISSUES` if any remain), then on success an
`ALLOCATION_COMMITTED` audit event is emitted and HTTP 201 is returned. On
idempotent replay (same key and fingerprint) the pre-flight gate is bypassed, no
second `ALLOCATION_COMMITTED` audit event is written (though `RECALCULATION_REQUESTED`
is still recorded for the replay), and HTTP 200 is returned. Pre-flight-blocked
writes (409 `BLOCKED_BY_ISSUES`) are not audited with `RECALCULATION_REQUESTED`
because the HTTP 409 is raised before the audit call.

`GET /revenue/source-rows` is an implemented read-only, tenant-scoped list of
Google source evidence rows (Track E, 2026-06-08). It requires global
`finance.view_revenue` and performs no audit write. `month` is required;
`source_system` is an optional enum filter
(`youtube_reporting`/`youtube_analytics`/`adsense_management`) and any other
value returns `422`. It uses newest-first (`ingested_at`, `id`) cursor
pagination: `limit` defaults to `50` and must be `1..100`; `cursor_ingested_at`
and `cursor_id` are both-or-neither (supplying only one half returns `422`). The
response envelope is `{items, pagination:{limit, returned, has_more,
next_cursor}}`, where `next_cursor` is `{ingested_at, id}` (or `null`). Each item
exposes `id`, `source_system`, `source_account_id`, `content_owner_id`,
`youtube_channel_id`, `report_type`, `report_month`, `period_start`,
`period_end`, `metric_key`, `value_kind`, `amount_native`, `currency_code`,
`source_report_id`, `ingested_at`, and `raw_payload_redacted` (always `true`).
The original `raw_payload` is **never** returned for any caller.

`GET /revenue/source-rows/{id}` is the implemented single-row read with the same
`finance.view_revenue` gate, tenant scoping, and redaction. It returns the same
item shape (no pagination envelope). A non-UUID id returns `422`; an id that is
missing or belongs to another tenant returns `404` with no existence leak.

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
GET /connectors/credentials/health?limit=50&offset=0
POST /connectors/credentials
POST /connectors/credentials/{connector_key}/{account_id}/test
POST /connectors/jobs
GET /connectors/runs?connector_key=youtube-reporting&limit=50&account=<placeholder-id>
GET /connectors/runs?limit=50&cursor_started_at=2026-05-10T12:00:00Z&cursor_id=<uuid>
```

`/connectors` is an implemented API group in this draft and owns credential-reference metadata plus connector job requests.

Connector credential responses expose metadata only, never raw credential material or secret references.

`POST /connectors/jobs` submits a real Google ingest pull to the module-owned,
bounded, in-process `ConnectorJobExecutor` and returns **202** immediately with
`execution_status: "submitted"`. The worker thread runs the proven CLI pattern on
its own session (`build_session_factory()()` ->
`connector_tenant_context(tenant_id, session=session)` ->
`connectors/runs/orchestrator.run_one`), so the run executes correctly on
RLS-enforced Postgres. The CLI `scripts/run_google_connector.py` remains a valid
production trigger.

Request body: `connector_key`, `account_id`, `report_month` (`YYYY-MM`,
required), `dry_run` (bool, default `false`), `reason` (required, audited).

Responses:

- **202** `{connector_key, account_id, report_month, dry_run,
  execution_status: "submitted", audit_event}` (no `run_id` — the run surfaces in
  `GET /connectors/runs` once the worker commits the RUNNING row).
- **403** missing `connectors.run_jobs` (no audit).
- **503** `"Connector job executor is disabled"` when the fail-closed
  `connector_job_executor_enabled` setting is off (default OFF).
- **422** unknown connector key / malformed `report_month` / missing or inactive
  credential.
- **409** duplicate in-flight (an in-process registry entry or a fresh RUNNING
  row for the exact scope). An orphan supersede flips a stale RUNNING row (older
  than `connector_job_stale_running_hours`, default 6h) to `FAILED` and proceeds
  to **202** with `superseded_run_id` recorded in the audit details.

Audit: exactly one route-owned `CONNECTOR_JOB_RUN` row (`details.action` is
`job_submitted` or `job_rejected`); the worker emits its own STARTED/FINISHED
lifecycle edges and, on a pre-start (Bucket-A) failure, a
`job_failed_before_start` row (canned error class name only, never message text).

`GET /connectors/credentials` now also returns `last_refresh_attempt_at`,
`token_expiry_at`, `last_refresh_status` (`succeeded`/`failed`/null), and
`last_refresh_error_class` (nullable; ISO-8601 timestamps), stamped at the single
`resolve_connector_credentials` refresh chokepoint.

`GET /connectors/credentials/health` is a read-only credential token-health
surface gated by `connectors.view_health` (fail-closed; this is the same
`VIEW_CONNECTOR_HEALTH` gate as `GET /connectors/runs`, and is **distinct** from
the `connectors.manage` gate on `GET /connectors/credentials`). The response is
`{credentials: [{...telemetry, health_state}]}` — each entry repeats the
credential metadata shape (`id`, `connector_key`, `account_id`, `status`,
`has_secret_ref`, plus the four refresh-telemetry fields above) and appends a
derived `health_state`. `health_state` is one of `healthy`, `expiring`
(token at or within 24h of expiry), `auth_failed` (last refresh failed or an
error class is recorded), `missing` (no stored secret reference), or `unknown`
(not yet determinable). The list is offset-paginated (`limit` defaults to `50`,
capped at `100`; `offset` defaults to `0`). Authorization mirrors
`GET /connectors/runs`: a connector-scoped viewer is **narrowed to only the
connector ids they are granted** (no foreign-credential leak), and a caller
without `connectors.view_health` at any scope (or a disabled principal) is
rejected `403`. The endpoint is read-only: no audit write, no migration, and it
derives `health_state` purely from already-persisted columns (no live OAuth
refresh is performed).

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

Google source-ingestion is live. The connector run path upserts
`google_revenue_source_rows` linked to these raw files, preserving
Google-reported amounts, currencies, account/report identity, period boundaries,
and raw payload references; `GET /revenue/source-rows` (and
`GET /revenue/source-rows/{id}`, listed under the revenue group above) reads
them. The post-run normalizer then projects eligible source rows into
`monthly_channel_revenue_facts` and emits run-driven `REPORT_IMPORTED` audit rows
(see the `POST /revenue/facts` section above for the run-driven import and
`PROJECTION_FAILED` semantics).

### Exports

```http
POST /exports
GET /exports?limit=50&offset=0
GET /exports/{export_id}
GET /exports/{export_id}/finance-workbook-preview
GET /exports/{export_id}/analytics-summary.csv
GET /exports/{export_id}/finance-workbook.xlsx
GET /exports/{export_id}/executive.pdf
GET /exports/{export_id}/branded-slide-pack.pptx
POST /export-templates
GET /export-templates?limit=50&offset=0&export_type=FINANCE_EXCEL&include_inactive=false
GET /export-templates/{template_id}
PATCH /export-templates/{template_id}
DELETE /export-templates/{template_id}?reason=...
```

`POST /exports`, `GET /exports/{export_id}`, and the `GET /exports` list
include `scope_channel_ids`: an array of YouTube channel IDs frozen at job
creation time for non-global exports (sector, company, channel, group). The
field is `null` for `scope_type='global'` jobs and for pre-snapshot legacy
rows. Subsequent edits to the source group, sector, or company membership do
not change the data returned for the same `export_id`, keeping finance
numbers deterministic per export.

`POST /exports` also accepts an optional `template_id`. When supplied, the
backend validates that the export template exists in the tenant, is active, and
has the same `export_type` as the requested export job. The selected template is
persisted on `export_jobs.template_id`, returned in export job API payloads, and
included in `EXPORT_CREATED` audit details. Template selection does not change
source SQL reads, formulas, scope snapshots, or finance calculations.

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

`POST /exports` accepts `ANALYTICS_SUMMARY_CSV` jobs only when the caller has
`exports.analytics`, `analytics.view`, and `finance.view_revenue` for the
requested export scope; this prevents queuing a revenue-bearing CSV that the
same caller cannot download. `GET /exports/{export_id}/analytics-summary.csv`
supports `ANALYTICS_SUMMARY_CSV` export jobs. It requires `exports.analytics`,
`analytics.view`, and `finance.view_revenue` for the export scope, using the
frozen `scope_channel_ids` snapshot for non-global jobs. The endpoint generates
an on-demand UTF-8 CSV
response with media type `text/csv` and an attachment filename. Rows are sourced
from normalized `google_revenue_source_rows` where
`source_system='youtube_analytics'`, filtered by tenant, month, export currency,
and scope, then aggregated by channel, metric, value kind, and currency. The CSV includes
`report_month`, `source_system`, `youtube_channel_id`, `channel_name`,
`metric_key`, `value_kind`, `currency_code`, period bounds, `source_row_count`,
`amount_native`, `formula`, and `confidence`; it does not export raw payloads,
account IDs, content-owner IDs, or source report IDs. The endpoint persists the
generated artifact through the configured export artifact store, records
`file_url`, filename, content type, byte size, SHA-256 checksum, marks the export
job `COMPLETED`, and emits `REVENUE_VIEWED` plus `EXPORT_DOWNLOADED` with
artifact metadata. If artifact storage fails before completion, the job remains
non-terminal and retryable, the endpoint returns `503`, and no revenue/download
audit event is emitted.

`/export-templates` manages tenant-scoped reusable export layout configuration.
Create/list operations require global `exports.manage_templates`; item reads,
updates, and soft-deletes accept either the global grant or a matching
`export:{template_id}` scoped grant. Templates include `name`, `export_type`,
optional `description`, JSON-object `layout_config`, `is_active`, `created_by`,
`created_at`, and `updated_at`. `DELETE /export-templates/{template_id}` does
not remove the row; it sets `is_active=false` so historical export jobs can keep
their nullable `template_id` reference. Template writes emit
`EXPORT_TEMPLATE_CHANGED` with a required operator reason. No backfill is
required for existing export jobs because `export_jobs.template_id` is nullable.

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
storage fails before completion, the job remains non-terminal and retryable, the
endpoint returns `503`, and does not emit `EXPORT_DOWNLOADED`.

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
GET /audit/summary?window_hours=24
GET /audit/events/export?event_type=REVENUE_VIEWED&entity_type=channel&entity_id=chan-1
```

Audit event reads require `audit.view`. Sensitive audit `details` are masked unless
the caller also has `audit.view_sensitive_payloads`. Audit reads are themselves
recorded as `AUDIT_LOG_VIEWED`.

`GET /audit/summary` returns tenant-scoped `total_events`, `sensitive_events`,
and `recent_count` aggregates. The snapshot excludes `AUDIT_LOG_VIEWED` rows
from every count, then the successful read records one `AUDIT_LOG_VIEWED` row
with `entity_type=audit_log_summary`; that just-written row is excluded from the
response that triggered it. `window_hours` defaults to `24` and is bounded to
`1..8760`.

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

## Smart revenue reconciliation (Track F)

`POST /revenue/months/{month}/reconcile` computes and persists the month's smart
revenue reconciliation, then audits it (`REVENUE_RECONCILED`). Gates:
`CHANGE_ALLOCATION_RULE` at `finance_month(month)`, `VIEW_REVENUE` globally,
`VIEW_FINALIZED_PAYMENTS` and `VIEW_BANK_RECONCILIATION` at `finance_month(month)`,
and `VIEW_CONFIDENCE` globally (confidence/warnings surface in the response).
Reconciliation attributes account-level tax/transfer-fee/FX deltas across
channels proportional to gross and may write ALLOCATION revenue facts, so it
reuses the allocation permission and requires read gates for the finance
values returned in the response. The JSON body requires a non-empty `reason`.
The response is
`{month, channels:[{youtube_channel_id, gross_usd, us_tax_usd, yt_adsense_fee_usd,
adsense_bank_fee_usd, fx_variance_usd, net_received_usd, us_view_share}],
totals:{gross_total_usd, us_tax_total_usd, yt_adsense_fee_total_usd,
adsense_bank_fee_total_usd, fx_total_usd, net_total_usd, yt_adsense_fee_pct},
warnings:[...]}`. Errors: locked month -> **409**; malformed month (not YYYY-MM,
month 01-12) -> **422**; missing permission -> **403**. The run persists typed
`deduction_components` plus a `revenue_reconciliation_usd` explanation; only the
`TAX` component feeds `net_revenue_usd` (transfer-fee/FX are evidence-only).

`GET /revenue/channels/{channel_id}/months/{month}/reconciliation` returns the
persisted `revenue_reconciliation_usd` explanation for that channel-month. Gates:
`VIEW_REVENUE` and `VIEW_CONFIDENCE` at `channel(channel_id)`, plus
`VIEW_FINALIZED_PAYMENTS` and `VIEW_BANK_RECONCILIATION` at
`finance_month(month)`. The read path audits `REVENUE_VIEWED` and
`PAYMENT_VIEWED` events; when the persisted explanation includes non-zero
bank-derived components (`adsense_bank_fee_usd` or `fx_variance_usd`), a
`BANK_RECONCILIATION_VIEWED` event is also recorded. Malformed month -> **422**;
no persisted explanation for the channel-month -> **404**.

`DELETE /reports/raw-files/{raw_file_id}` purges a raw report file. Gate:
`MANAGE_CONNECTORS` at `connector(source)` (the source is resolved from the file;
a boundary any-scope check runs first so unauthorized callers cannot probe
existence, then the connector-scoped check applies). The JSON body requires a
non-empty `reason` (missing/blank -> **422**). The purge marks the row `PURGED`
and clears `file_url` while keeping all metadata (source, report_type,
report_month, checksum) for the audit trail; it sets `purged_at`/`purged_by` and
emits a `REPORT_PURGED` audit event. Unknown id or a cross-tenant id (no
connector-scope grant) -> **404**; re-purging an already-PURGED row -> **409**.

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
