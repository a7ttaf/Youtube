# UMS Role and Permission Model

## Security Position
UMS Smart Revenue Control Center is a corporate finance and revenue operations platform. Authorization must protect money values, finalized payments, bank reconciliation data, export files, raw API/report files, connector controls, channel ownership mappings, and month-close state.

The source of truth for permissions is the application database. SQL/PostgreSQL or warehouse tables remain the financial source of truth. Neo4j is a read-only graph projection and never grants access by itself.

Runtime authorization should use database-backed principals in production. In that mode the trusted gateway supplies only the authenticated user identity, and the application loads active role assignments, direct permission grants, and scopes from SQL. Header-provided role or scope claims are bootstrap/test conveniences only and must not be treated as the corporate source of truth.

## Scope Model
Permissions can be granted at these scope levels:

| Scope | Purpose |
|---|---|
| `global` | Entire UMS platform and every company, sector, channel, month, export, connector, and graph view. |
| `sector` | A specific sector such as TV or News, including companies and channels assigned to that sector. |
| `company` | A specific company and its mapped channels. |
| `channel` | A specific YouTube channel. |
| `finance-month` | A specific finance month such as `2026-03`; used for close, lock, unlock, override, and allocation decisions. |
| `export` | A specific export job or export template domain. |
| `connector` | A specific connector family such as YouTube Reporting, YouTube Analytics, YouTube Data, or AdSense. |
| `graph-read` | A graph view type, still filtered through `global`, `sector`, `company`, or `channel` data permissions. |

Scope inheritance is explicit:
- `global` includes every scope.
- `sector` includes companies and channels mapped to that sector.
- `company` includes channels mapped to that company.
- `channel` includes only the named channel.
- `finance-month`, `export`, `connector`, and `graph-read` scopes do not imply organization visibility by themselves.

## Roles

### Super Owner
Global break-glass owner. Can see all analytics, finance, raw files, graph views, users, exports, connectors, month locks, allocation rules, and audit logs. This role should be assigned to very few users and reviewed regularly.

### Corporate Admin
Global platform administrator for organization structure, users, channel registry, groups, export templates, and non-financial analytics. This role does not automatically include sensitive finance visibility unless paired with a finance role.

### Revenue Operations Admin
Global data operations role for channel registry quality, report ingestion visibility, sync health, and non-payment operational troubleshooting. Can run connector jobs but cannot administer OAuth credentials or change finance rules.

### Finance Admin
Global finance owner for revenue, finalized payments, bank reconciliation reads and receipt recording, manual overrides, allocation rules, exports, and finance month locking. Cannot assign Super Owner without Super Owner approval.

### Finance Approver
Second-control finance role for approving manual overrides, allocation rule changes, month unlock requests, and controlled bank reconciliation receipt updates. Designed for segregation of duties when Finance Admin enters the change.

### Finance Viewer
Read-only finance role. Can view revenue, finalized payments, bank/payment reconciliation, and finance exports within granted scope. Cannot lock months, create overrides, change allocation rules, or manage connectors.

### TV Sector Manager
Sector-scoped management role for TV. Can view TV analytics and scoped graph views. Finance visibility is optional and must be explicitly granted.

### News Sector Manager
Sector-scoped management role for News. Can view News analytics and scoped graph views. Finance visibility is optional and must be explicitly granted.

### Company Manager
Company-scoped role. Can view analytics and scoped graph views for assigned companies only. Finance visibility is optional and must be explicitly granted by Finance Admin or Super Owner.

### Channel Manager
Channel-scoped role. Can view performance analytics and channel metadata for assigned channels only. Finance visibility is not included by default.

### Assistant Analyst
Assigned-scope analyst role. Can view performance analytics, confidence labels, non-financial issue flags, and graph views for assigned scopes. Cannot view sensitive finance data, raw report files, finalized payments, bank reconciliation, or revenue exports unless explicitly granted.

### Export Operator
Scoped export execution role. Can create approved analytics exports and, when granted finance scope, finance exports. Cannot change allocation rules, create manual overrides, lock months, manage users, or administer connectors.

### Audit Viewer
Read-only audit and compliance role. Can view audit logs and security metadata. Sensitive payload details remain masked unless separately granted finance or raw-file permissions.

### System Integration User
Non-human service role for scheduled jobs and connectors. Can run connector jobs, write raw ingestion records through backend services, and create audit events. Cannot use dashboard sessions or assign human roles.

### Connector Admin
Technical integration administrator. Can manage OAuth/client configuration, rotate encrypted tokens, and run connector jobs. Does not receive finance visibility unless paired with a finance role.

### Data Steward
Scoped registry maintenance role. Can change channel/company/sector mappings and groups within granted scope. Does not receive finance visibility.

## Permission Families

| Family | Examples |
|---|---|
| Analytics | view channel metrics, view company performance, view issue flags. |
| Finance | view revenue, finalized payments, bank reconciliation, allocation results. |
| Finance control | manual override, override approval, lock/unlock month, change allocation rules, record bank reconciliation receipts. |
| Registry | manage channel registry, company mapping, sector mapping, groups. |
| Export | export analytics, export revenue, manage templates, view export history. |
| Connectors | run connector jobs, manage OAuth/API settings, view connector health. |
| Raw data | view raw API payloads, raw report files, parser errors. |
| Graph | view hierarchy, revenue flow, issue graph, outside-CMS graph. |
| Administration | assign roles, manage users, view audit logs, manage platform config. |

## Sensitive Actions
Every sensitive action must produce an audit event with actor, action, scope, entity, timestamp, request id, and reason where applicable.

Sensitive actions include:
- viewing revenue;
- viewing finalized payments;
- viewing bank/payment reconciliation;
- recording bank/payment reconciliation receipts;
- exporting revenue files;
- creating or approving manual overrides;
- locking or unlocking a finance month;
- changing allocation rules;
- creating, disabling, or updating user accounts;
- assigning users to roles;
- assigning direct scoped permissions;
- changing channel/company/sector mapping;
- managing OAuth/API connector settings;
- running connector jobs;
- viewing raw API or report files;
- reading Neo4j revenue-flow graphs that contain money values.

## Guard Rules
- UI must never rely on hidden columns alone. Backend permissions decide every sensitive response.
- Production authorization must use SQL-backed principals (`UMS_AUTHZ_SOURCE=database`) so persisted role assignments and direct permission grants are enforced at runtime.
- Unknown or disabled database users must fail closed before route handlers run.
- Money APIs require `VIEW_REVENUE` for the requested organization scope.
- Payment APIs require `VIEW_FINALIZED_PAYMENTS`.
- Bank reconciliation read APIs require `VIEW_BANK_RECONCILIATION` plus finalized-payment visibility for the requested finance-month scope when payment rows are part of the response.
- Bank reconciliation write APIs require `MANAGE_BANK_RECONCILIATION` for the requested finance-month scope, a non-empty reason, an unlocked month, and audit logging.
- Revenue exports require both `EXPORT_REVENUE_REPORT` and `VIEW_REVENUE`.
- Manual overrides require `CREATE_MANUAL_OVERRIDE`, a reason, and audit logging.
- Override approval requires `APPROVE_MANUAL_OVERRIDE` and should be a different user from the creator.
- Month lock/unlock requires `LOCK_FINANCE_MONTH` or `UNLOCK_FINANCE_MONTH` for the month scope.
- Allocation changes require `CHANGE_ALLOCATION_RULE` for the month scope.
- Connector administration requires `MANAGE_CONNECTORS`; running jobs requires `RUN_CONNECTOR_JOBS`.
- User account creation and updates require `users.manage`, a reason, and a `USER_ACCOUNT_CHANGED` audit event.
- User account list and access-profile reads require `users.manage`; account lists use bounded cursor pagination and access profiles expose only active role assignments and direct grants for administration workflows.
- Service account lifecycle changes require Super Owner authority.
- Role assignment APIs require `roles.assign`; Super Owner assignment requires an existing Super Owner, and finance role assignment requires Finance Admin or Super Owner authority.
- Direct permission grant APIs require `roles.assign`, enforce the target permission family, and audit every grant or revocation as `USER_PERMISSION_CHANGED`.
- Finance direct grants such as `finance.view_revenue`, `exports.revenue`, and `graph.view_finance` require Finance Admin or Super Owner.
- Connector and raw-file direct grants require Connector Admin or Super Owner.
- Administrative direct grants such as `roles.assign`, `users.manage`, `platform.manage_settings`, and `audit.view_sensitive_payloads` require Super Owner.
- Neo4j graph reads must pass through backend guards and filter nodes by the same organization scopes used for SQL reads.
- Dashboard users never receive Neo4j write credentials.

## Channel Registry API Rules
- Listing channels requires `analytics.view` for each returned channel scope; the backend filters rows rather than returning every channel and trusting the UI.
- Creating a channel requires `registry.manage_channels` for the target company scope.
- Changing channel/company mapping requires `registry.manage_org_mapping` for both the current channel scope and the target company scope.
- Mapping changes require a non-empty reason and produce a `CHANNEL_UPDATED` audit event.
- Channel registry endpoints must not return revenue values.

## Channel Group API Rules
- Listing groups requires `analytics.view` for every active member channel in the returned group; mixed-company groups are hidden from company-scoped users unless every member is in scope.
- Creating a group requires `registry.manage_groups` for every requested member channel. Empty groups require global group-management permission.
- Updating a group or changing members requires `registry.manage_groups` for every existing member channel plus every newly added channel.
- Group create, update, member-add, and member-remove operations require a non-empty reason and produce a `GROUP_UPDATED` audit event.
- Group endpoints return group metadata and channel IDs only; revenue values remain behind finance APIs.

## Connector API Rules
- Connector credential administration requires `connectors.manage` for the connector scope.
- Connector credential APIs accept only external encrypted secret references such as secret-manager, vault, KMS, or cloud key-vault URIs.
- Connector credential API responses must not expose `encrypted_secret_ref` or raw credential material.
- Connector job requests require `connectors.run_jobs` for the connector scope.
- Connector job request endpoints in the foundation record and audit control-plane intent only; actual Google API execution belongs in the worker connector implementation.
- Connector credential changes require a non-empty reason and produce `CONNECTOR_SETTINGS_CHANGED`; connector job requests produce `CONNECTOR_JOB_RUN`.

## Finance Close API Rules
- Viewing finance-close status requires `finance.view_revenue` for the finance-month scope because close status affects finance workflows.
- Locking a month requires `finance.lock_month`, a non-empty reason, and a `MONTH_LOCKED` audit event.
- Month readiness must block locks while pending overrides, unresolved reconciliation issues, or missing facts for active revenue-required channels exist.
- Unlocking a month requires `finance.unlock_month`, a non-empty reason, and a `MONTH_UNLOCKED` audit event.
- Recording allocation-rule metadata requires `finance.change_allocation_rule`, a non-empty reason, and an `ALLOCATION_RULE_CHANGED` audit event.
- The foundation finance-close API records control metadata only; it must not invent revenue calculations or mutate locked revenue facts.
