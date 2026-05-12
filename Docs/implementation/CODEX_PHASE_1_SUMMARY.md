# UMS Phase 1 Authorization Foundation Summary

## What Was Implemented
Created the first technical foundation for the UMS Smart Revenue Control Center security model:
- role and permission design documentation;
- permission matrix with scope and audit rules;
- backend Python authorization constants, models, seed definitions, scoped access policy helpers, guard helpers, audit event definitions, and UI-facing metadata;
- PostgreSQL starter schema and seed SQL for users, roles, permissions, scopes, assignments, grants, connector credentials, and audit logs;
- read-only Neo4j dashboard service stub that enforces application permissions and post-query scope filtering;
- pytest coverage for the core positive and negative permission cases.

Continuation update:
- verified current stable/LTS stack versions from upstream package registries and official release pages;
- pinned backend dependencies in `pyproject.toml`;
- added `Docs/implementation/TECH_VERSION_BASELINE.md`;
- added a FastAPI app shell with `/health`, `/security/roles`, and `/security/permissions`;
- added version baseline constants exposed to code and tests.

Second continuation update:
- added SQLAlchemy ORM metadata for the security schema;
- added Alembic scaffold and initial security migration;
- added a FastAPI current-principal dependency using explicit headers until the real identity provider is chosen;
- added an authorization-only guarded revenue route to prove route-level finance enforcement without returning fake revenue values;
- added a testable audit service contract for sensitive events and required reasons.

Third continuation update:
- added SQLAlchemy ORM metadata for org units, YouTube channels, channel groups, and channel group members;
- added a deterministic access-index builder that maps channels to companies and sectors from registry rows;
- moved the guarded revenue route to use an injectable org access-index dependency instead of an inline hardcoded mapping;
- added an Alembic migration for the channel registry foundation.

Fourth continuation update:
- added a scoped channel registry service and FastAPI `/channels` routes;
- added guarded channel listing, create, and mapping-update behavior;
- required `registry.manage_channels` for channel creation and `registry.manage_org_mapping` for mapping changes;
- added required audit records for channel/company mapping updates;
- documented channel registry API guard rules in the role model.

Fifth continuation update:
- added a SQLAlchemy-backed channel registry repository for `youtube_channels`;
- added a loader that builds `OrgAccessIndex` directly from `org_units` and `youtube_channels` SQL rows;
- added a database session factory and FastAPI dependency override path via `create_app(database_url=...)`;
- kept the bootstrap in-memory registry available for local/spec-mode tests while making production routes repository-ready;
- switched org ORM UUID columns to SQLAlchemy's portable `Uuid` type so PostgreSQL remains native and SQLite-backed tests can validate behavior.

Sixth continuation update:
- added a SQLAlchemy audit sink that persists sensitive audit records to `audit_logs`;
- wired database-backed FastAPI routes to use the SQL audit sink, sharing the same session/transaction as channel registry changes;
- kept the in-memory audit sink as the default bootstrap dependency for deterministic local tests;
- made security ORM UUID and JSON columns portable for SQLite-backed behavior tests while retaining PostgreSQL-native Alembic migrations.

Seventh continuation update:
- added channel-group registry domain models and a SQLAlchemy-backed group repository for `channel_groups` and `channel_group_members`;
- added FastAPI `/groups` routes for list, create, update, add members, and remove member;
- enforced `registry.manage_groups` over all existing and requested member channels for group mutations;
- filtered group listing so company-scoped users cannot see mixed groups containing out-of-scope channels;
- audited group create/update/member changes through `GROUP_UPDATED` events.

Eighth continuation update:
- added runtime settings loading for `UMS_DATABASE_URL`;
- updated `create_app()` so normal app startup uses SQL-backed dependencies when `UMS_DATABASE_URL` is present;
- preserved explicit `create_app(database_url=...)` override support for tests and controlled local runs;
- kept bootstrap in-memory mode as the default when no database URL is configured.

Ninth continuation update:
- added connector control-plane APIs for credential reference registration and connector job requests;
- added a SQLAlchemy connector credential repository backed by `api_connector_credentials`;
- enforced `connectors.manage` for credential administration and `connectors.run_jobs` for job requests;
- rejected raw credential-looking payloads by requiring external encrypted secret references;
- ensured connector API responses do not expose `encrypted_secret_ref`;
- audited connector settings changes and connector job requests without executing Google API calls.

Tenth continuation update:
- added finance-close control-plane ORM metadata and Alembic migration for `finance_month_close`;
- added a SQLAlchemy finance month-close repository;
- added FastAPI `/finance-close/{month}`, `/lock`, `/unlock`, and `/allocate` routes;
- enforced `finance.lock_month`, `finance.unlock_month`, and `finance.change_allocation_rule` on finance-month scope;
- audited month lock, unlock, and allocation-rule metadata changes;
- kept the finance-close API limited to control metadata and did not implement fake revenue calculations.

Eleventh continuation update:
- added raw report-file metadata ORM and Alembic migration for `raw_report_files`;
- added a SQLAlchemy raw report-file repository and FastAPI `/reports/raw-files` routes;
- enforced `connectors.run_jobs` for report metadata registration and `raw_files.view` for raw metadata reads;
- audited report registration as `REPORT_IMPORTED` and raw metadata reads as `RAW_FILE_VIEWED`;
- rejected local/inline storage paths by requiring approved object-storage URI prefixes;
- stored report artifact metadata only, not report contents or Google credentials.

Twelfth continuation update:
- added number-explanation ORM metadata and Alembic migration for `number_explanations`;
- added a deterministic channel-month explanation service for `adjusted_gross_revenue_usd`;
- added FastAPI `/revenue/channels/{channel_id}/months/{month}/explain`;
- enforced both `finance.view_revenue` and `analytics.view_confidence` for explain-number reads;
- persisted explanation snapshots keyed by month, entity, and metric;
- audited explanation reads as sensitive `REVENUE_VIEWED` events;
- kept the explanation value derived only from stored revenue facts plus approved manual overrides, with pending overrides reported as warnings.

Thirteenth continuation update:
- added export-job ORM metadata and Alembic migration for `export_jobs`;
- added a SQLAlchemy export-job repository and FastAPI `/exports` routes;
- records queued export metadata only, without generating workbook, PDF, slide, or fake revenue files;
- enforces `exports.revenue` plus `finance.view_revenue` for finance exports;
- enforces `exports.analytics` plus analytics visibility for analytics exports;
- checks group export access against every member channel;
- captures month lock status on the export job and audits requests as `EXPORT_CREATED`.

Fourteenth continuation update:
- added a SQLAlchemy audit-log repository and FastAPI `/audit/events` route;
- enforced `audit.view` for audit-log reads;
- masked sensitive audit `details` unless the caller has `audit.view_sensitive_payloads`;
- audited audit-log reads as `AUDIT_LOG_VIEWED`.

Fifteenth continuation update:
- added a SQLAlchemy user-role assignment repository and FastAPI `/users/{user_id}/roles` routes;
- persisted scoped user-role assignments through the existing `user_role_assignments` and `access_scopes` tables;
- enforced `roles.assign` before parsing target user IDs to avoid probing;
- restricted Super Owner assignment to existing Super Owners;
- restricted finance role assignment to Finance Admin or Super Owner authority;
- audited role assignment and revocation as `USER_ROLE_CHANGED`.

Sixteenth continuation update:
- added a SQLAlchemy user-permission grant repository and FastAPI `/users/{user_id}/permissions` routes;
- persisted scoped direct permission grants through the existing `user_permission_grants` and `access_scopes` tables;
- enforced `roles.assign` before parsing target user IDs to avoid probing;
- restricted finance direct grants to Finance Admin or Super Owner authority;
- restricted connector/raw-file direct grants to Connector Admin or Super Owner authority;
- restricted administrative direct grants to Super Owner authority;
- validated direct grant scope types against the target permission family;
- audited direct permission grant and revocation as `USER_PERMISSION_CHANGED`.

Seventeenth continuation update:
- added a SQLAlchemy user-account repository and FastAPI `POST /users` plus `PATCH /users/{user_id}` routes;
- enforced `users.manage` before creating or updating user accounts;
- restricted service-account lifecycle changes to Super Owner authority;
- normalized user email addresses and rejected duplicate emails case-insensitively;
- audited user account create/update as `USER_ACCOUNT_CHANGED`.

Eighteenth continuation update:
- added a SQLAlchemy principal loader that builds `UserPrincipal` from active `user_role_assignments`, `user_permission_grants`, and `access_scopes`;
- added `authz_source="database"` / `UMS_AUTHZ_SOURCE=database` app mode to use SQL as the runtime authorization source;
- kept header-sourced principals as bootstrap/test mode so existing local tests remain explicit and stable;
- made database-backed principals ignore header role/scope claims;
- fail-closed for disabled or unregistered database users before route handlers run.

Nineteenth continuation update:
- added guarded `GET /users` account listing with bounded cursor pagination, capped offset compatibility, and optional status filtering;
- added guarded `GET /users/{user_id}/access` to return a user's active scoped role assignments and active direct permission grants;
- enforced `users.manage` before access-profile target id parsing so unauthorized callers cannot probe account ids;
- returned assignment and grant identifiers in access profiles so admin UI workflows can revoke active access explicitly;
- kept revoked historical access out of the active profile, leaving history to audit and future access-history endpoints.

Twentieth continuation update:
- hardened finance month-close readiness so active registry channels marked `revenue_required` must have at least one monthly revenue fact before the month can lock;
- added a `MISSING_REVENUE_FACTS` close blocker surfaced through `GET /finance-close/{month}/readiness` and lock conflict responses;
- kept performance-only or non-revenue-required channels from blocking close when they have no revenue facts;
- preserved the existing blockers for pending manual overrides and unresolved reconciliation issues;
- hardened lock attempts to acquire a transaction-scoped finance-month guard plus the month-close row, then re-run readiness with row locks on matching pending overrides, missing revenue-required channel rows, and monthly revenue facts so stale readiness snapshots and concurrent month-scoped writers cannot authorize an invalid lock.

Twenty-first continuation update:
- added AdSense payment SQL metadata with an Alembic migration for `adsense_payments`;
- added a SQLAlchemy AdSense payment repository that validates official payment metadata and upserts by `(month, payment_name)` so connector reruns do not duplicate rows;
- added FastAPI `/adsense/sync-payments` guarded by `connectors.run_jobs` on connector scope `adsense`;
- added FastAPI `/adsense/payments` guarded by global `finance.view_finalized_payments`;
- rejected AdSense payment sync for locked finance months;
- audited payment sync as `ADSENSE_PAYMENT_SYNCED` and finalized-payment reads as `PAYMENT_VIEWED`;
- kept this slice to payment metadata and did not add fake revenue calculations, bank matching, Google password storage, or Neo4j financial source-of-truth behavior.

## Files Created
- `Docs/implementation/CODEX_PHASE_1_PLAN.md`
- `Docs/implementation/CODEX_PHASE_1_SUMMARY.md`
- `Docs/implementation/TECH_VERSION_BASELINE.md`
- `Docs/security/ROLE_PERMISSION_MODEL.md`
- `Docs/security/PERMISSION_MATRIX.md`
- `Docs/security/NEO4J_READ_ONLY_GRAPH_SECURITY.md`
- `pyproject.toml`
- `backend/ums_smart_revenue/__init__.py`
- `backend/ums_smart_revenue/app.py`
- `backend/ums_smart_revenue/api/__init__.py`
- `backend/ums_smart_revenue/api/adsense.py`
- `backend/ums_smart_revenue/api/audit.py`
- `backend/ums_smart_revenue/api/channels.py`
- `backend/ums_smart_revenue/api/connectors.py`
- `backend/ums_smart_revenue/api/dependencies.py`
- `backend/ums_smart_revenue/api/exports.py`
- `backend/ums_smart_revenue/api/finance_close.py`
- `backend/ums_smart_revenue/api/groups.py`
- `backend/ums_smart_revenue/api/reports.py`
- `backend/ums_smart_revenue/api/revenue.py`
- `backend/ums_smart_revenue/api/security.py`
- `backend/ums_smart_revenue/api/users.py`
- `backend/ums_smart_revenue/auth/__init__.py`
- `backend/ums_smart_revenue/auth/audit_log.py`
- `backend/ums_smart_revenue/auth/principals.py`
- `backend/ums_smart_revenue/auth/user_permissions.py`
- `backend/ums_smart_revenue/auth/user_roles.py`
- `backend/ums_smart_revenue/auth/permissions.py`
- `backend/ums_smart_revenue/auth/roles.py`
- `backend/ums_smart_revenue/auth/scopes.py`
- `backend/ums_smart_revenue/auth/models.py`
- `backend/ums_smart_revenue/auth/seed.py`
- `backend/ums_smart_revenue/auth/policy.py`
- `backend/ums_smart_revenue/auth/audit.py`
- `backend/ums_smart_revenue/auth/api_guards.py`
- `backend/ums_smart_revenue/auth/audit_service.py`
- `backend/ums_smart_revenue/auth/sql_audit_sink.py`
- `backend/ums_smart_revenue/auth/ui_metadata.py`
- `backend/ums_smart_revenue/auth/users.py`
- `backend/ums_smart_revenue/db/__init__.py`
- `backend/ums_smart_revenue/db/explanation_models.py`
- `backend/ums_smart_revenue/db/finance_models.py`
- `backend/ums_smart_revenue/db/session.py`
- `backend/ums_smart_revenue/db/security_models.py`
- `backend/ums_smart_revenue/db/org_models.py`
- `backend/ums_smart_revenue/db/report_models.py`
- `backend/ums_smart_revenue/db/security_schema.sql`
- `backend/ums_smart_revenue/db/security_seed.sql`
- `backend/ums_smart_revenue/db/alembic/env.py`
- `backend/ums_smart_revenue/db/alembic/script.py.mako`
- `backend/ums_smart_revenue/db/alembic/versions/20260510_0001_security_foundation.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260510_0002_org_registry.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260510_0003_finance_close.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260510_0004_revenue_facts.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260510_0005_manual_overrides.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260510_0006_raw_report_files.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260510_0007_number_explanations.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260510_0008_export_jobs.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260512_0002_adsense_payments.py`
- `backend/ums_smart_revenue/config/__init__.py`
- `backend/ums_smart_revenue/config/settings.py`
- `backend/ums_smart_revenue/config/version_baseline.py`
- `backend/ums_smart_revenue/connectors/__init__.py`
- `backend/ums_smart_revenue/connectors/credentials.py`
- `backend/ums_smart_revenue/finance/__init__.py`
- `backend/ums_smart_revenue/finance/adsense_payments.py`
- `backend/ums_smart_revenue/finance/explanations.py`
- `backend/ums_smart_revenue/finance/month_close.py`
- `backend/ums_smart_revenue/org/__init__.py`
- `backend/ums_smart_revenue/org/access_index.py`
- `backend/ums_smart_revenue/org/bootstrap_registry.py`
- `backend/ums_smart_revenue/org/channel_groups.py`
- `backend/ums_smart_revenue/org/channel_registry.py`
- `backend/ums_smart_revenue/org/sql_channel_groups.py`
- `backend/ums_smart_revenue/org/sql_channel_registry.py`
- `backend/ums_smart_revenue/graph/__init__.py`
- `backend/ums_smart_revenue/graph/readonly_service.py`
- `backend/ums_smart_revenue/graph/cypher.py`
- `backend/ums_smart_revenue/reports/__init__.py`
- `backend/ums_smart_revenue/reports/exports.py`
- `backend/ums_smart_revenue/reports/raw_files.py`
- `tests/conftest.py`
- `tests/test_version_baseline.py`
- `tests/api/test_audit_api.py`
- `tests/api/test_adsense_payments_api.py`
- `tests/api/test_app.py`
- `tests/api/test_channels_api.py`
- `tests/api/test_connectors_api.py`
- `tests/api/test_database_principals.py`
- `tests/api/test_exports_api.py`
- `tests/api/test_finance_close_api.py`
- `tests/api/test_groups_api.py`
- `tests/api/test_raw_report_files_api.py`
- `tests/api/test_revenue_explanations_api.py`
- `tests/api/test_user_access_read_api.py`
- `tests/api/test_user_accounts_api.py`
- `tests/api/test_user_permissions_api.py`
- `tests/api/test_user_roles_api.py`
- `tests/api/test_guarded_routes.py`
- `tests/api/test_sql_backed_channel_dependencies.py`
- `tests/auth/test_audit_service.py`
- `tests/auth/test_access_index_builder.py`
- `tests/auth/test_policy.py`
- `tests/auth/test_sql_audit_sink.py`
- `tests/db/test_alembic_scaffold.py`
- `tests/db/test_adsense_payment_migration.py`
- `tests/db/test_adsense_payment_models.py`
- `tests/db/test_export_job_migration.py`
- `tests/db/test_export_job_models.py`
- `tests/db/test_explanation_migration.py`
- `tests/db/test_explanation_models.py`
- `tests/db/test_finance_close_migration.py`
- `tests/db/test_finance_close_models.py`
- `tests/db/test_org_registry_migration.py`
- `tests/db/test_org_registry_models.py`
- `tests/db/test_raw_report_file_migration.py`
- `tests/db/test_raw_report_file_models.py`
- `tests/db/test_security_orm.py`
- `tests/graph/test_readonly_service.py`
- `tests/org/test_sql_channel_registry.py`
- `alembic.ini`

## Role Model Summary
The foundation defines these initial roles:
- Super Owner
- Corporate Admin
- Revenue Operations Admin
- Finance Admin
- Finance Approver
- Finance Viewer
- TV Sector Manager
- News Sector Manager
- Company Manager
- Channel Manager
- Assistant Analyst
- Export Operator
- Audit Viewer
- System Integration User
- Connector Admin
- Data Steward

The model separates platform administration from finance visibility. Corporate Admin can manage users, registry, groups, settings, and templates but cannot view finance by default. Finance Admin controls revenue, payments, bank reconciliation, overrides, allocation rules, exports, and month locking. Assistant Analysts receive analytics only unless explicitly granted finance permission.

## Permission Matrix Summary
Permissions are grouped around analytics, finance, finance control, exports, registry, connectors, raw files, graph reads, audit, users, roles, and platform settings. Sensitive permissions are flagged for audit-on-use.

Scopes supported:
- `global`
- `sector`
- `company`
- `channel`
- `finance-month`
- `export`
- `connector`
- `graph-read`

Organization scope inheritance is explicit:
- global includes everything;
- sector includes mapped companies and channels;
- company includes mapped channels;
- channel includes only that channel.

## Backend Helpers Added
The backend now supports checks including:
- `can_view_channel_analytics(user, channel_id, org_index)`
- `can_view_channel_revenue(user, channel_id, org_index)`
- `can_view_company_revenue(user, company_id, org_index)`
- `can_export_finance_report(user, scope, org_index)`
- `can_lock_month(user, month)`
- `can_change_allocation_rule(user, month)`
- `can_view_neo4j_graph(user, scope, org_index, contains_finance=False)`
- `can_manage_connectors(user)`
- `can_assign_roles(user)`

## Neo4j Security Summary
Neo4j remains read-only for dashboard users. The backend graph service checks app permissions before graph access and filters returned rows by the requested scope. Finance graph views require both graph finance permission and underlying revenue visibility. Dashboard graph code has no write method.

## Tests Added
Pytest coverage includes:
- Super Owner can see and manage all sensitive controls.
- Assistant Analyst can view assigned analytics but not finance by default.
- Explicit finance grants allow scoped finance visibility only where granted.
- Company Manager cannot view another company.
- Export Operator cannot change allocation rules.
- Finance Viewer cannot lock month or change allocation rules.
- Finance Admin can lock month and change allocation rules.
- Connector Admin can manage connectors while Revenue Operations Admin cannot.
- Finance graph reads require finance visibility.
- Corporate Admin can assign roles without default finance visibility.
- Neo4j revenue flow results are filtered to the authorized company.
- Neo4j finance graph rejects Assistant Analyst without finance visibility.
- SQL-backed channel registry methods read, create, remap, and persist channel rows.
- SQL-loaded organization access indexes derive sector/company/channel scope maps from active database rows.
- FastAPI can run channel endpoints against a configured database URL and commit route writes.
- SQL audit sink persists sensitive audit records with actor, scope, reason, entity, details, and sensitivity flag.
- Database-backed channel mapping updates persist both the remap and the audit log through route dependencies.
- Company-scoped group listing hides groups that include out-of-scope member channels.
- Data Steward can create in-scope groups and cannot create groups containing another company's channel.
- Corporate Admin can add and remove group members, with both operations audited.
- `create_app()` can pick up `UMS_DATABASE_URL` from the environment and use SQL-backed channel dependencies without a manual parameter.
- Connector Admin can register credential references without exposing secret references in API responses.
- Connector credential registration rejects raw secret-like payloads.
- Assistant Analyst cannot create connector credentials.
- Revenue Operations Admin can record a connector job request and produce a `CONNECTOR_JOB_RUN` audit event without executing a connector.
- Finance Admin can lock a month and create a `MONTH_LOCKED` audit event.
- Finance Viewer cannot lock a month.
- Finance Approver can unlock a month and create a `MONTH_UNLOCKED` audit event.
- Finance close readiness blocks missing facts for active revenue-required channels before month lock.
- Finance close readiness does not block performance-only channels without facts.
- Export Operator cannot change allocation rules.
- Finance Admin can record allocation-rule metadata and create an `ALLOCATION_RULE_CHANGED` audit event without calculating revenue.
- System Integration User can register raw report-file metadata and create a sensitive `REPORT_IMPORTED` audit event.
- Connector Admin can view scoped raw report-file metadata and create a sensitive `RAW_FILE_VIEWED` audit event.
- Assistant Analyst cannot view raw report files.
- Connector-scoped admins cannot view another connector's raw report metadata.
- Raw report registration rejects local/inline storage references.
- Duplicate raw report artifact metadata returns a controlled `409` conflict.
- Finance Viewer can fetch an adjusted revenue explanation with an audit event and persisted snapshot.
- Assistant Analyst cannot fetch revenue explanations by default.
- Unsupported explanation metrics are rejected explicitly.
- Number explanation ORM persists formula components and warnings.
- Finance Admin can request a queued finance export with an audit event and month-lock snapshot.
- Export Operator cannot request finance exports without finance visibility.
- Export Operator can request scoped analytics exports.
- Company Manager cannot request exports for another company.
- Group export requests require access to every member channel.
- Export requests reject non-USD currency until exchange-rate support exists.
- Export listing returns only the requesting user's jobs.
- Export detail reads allow a user to fetch their own export metadata.
- Users without export permission cannot probe export ids.
- Export job ORM persists queued job metadata.
- Audit Viewer can list audit events with sensitive details masked.
- Super Owner can view sensitive audit details.
- Assistant Analyst cannot view audit events.
- Audit-log reads create an `AUDIT_LOG_VIEWED` audit event.
- Corporate Admin can assign scoped Assistant Analyst access and produce a `USER_ROLE_CHANGED` audit event.
- Assistant Analyst cannot assign roles or probe target user ids.
- Corporate Admin cannot assign finance roles or Super Owner.
- Finance Admin can assign Finance Viewer.
- Corporate Admin can revoke role assignments with audit logging.
- Finance Admin can grant and revoke scoped direct revenue permission with `USER_PERMISSION_CHANGED` audit logging.
- Corporate Admin can grant non-finance direct permissions but cannot grant or revoke finance direct permissions.
- Assistant Analyst cannot grant direct permissions or probe target user ids.
- Duplicate active direct permission grants are rejected.
- Corporate Admin can create and disable human user accounts with `USER_ACCOUNT_CHANGED` audit events.
- Assistant Analyst cannot create or update users.
- Duplicate user emails are rejected case-insensitively.
- Service account creation is restricted to Super Owner.
- No-op user account updates are rejected before creating an audit event.
- Corporate Admin can list user accounts with bounded cursor pagination, status filtering, capped offset rejection, and empty large-offset handling.
- Corporate Admin can read a user's active access profile, including scoped role assignments and direct grants.
- User access-profile reads return 404 for missing users and 422 for invalid target UUIDs after valid authorization.
- Assistant Analyst cannot list users or probe access-profile target ids.
- DB-backed principals use stored active role assignments instead of claimed header roles.
- DB-backed principals load direct permission grants and use them on guarded routes.
- DB-backed principals reject disabled users even when headers claim Super Owner.
- DB-backed principals reject users that are authenticated by the gateway but not registered in SQL.
- System Integration User can sync official AdSense payment metadata with a sensitive `ADSENSE_PAYMENT_SYNCED` audit event.
- AdSense payment sync is idempotent for repeated `(month, payment_name)` rows.
- Finance Viewer can list finalized AdSense payment metadata with a sensitive `PAYMENT_VIEWED` audit event.
- Assistant Analyst cannot view AdSense payment metadata by default.
- Connector-scoped users cannot sync AdSense payments through another connector scope.
- Locked finance months reject AdSense payment sync and persist no payment rows.
- AdSense payment ORM and migration preserve payment date, amount, currency, status, raw payload reference data, source report id, and importer metadata.

## Commands Run
- `git status --short --branch` failed because this workspace is not a Git repository.
- `pytest -q` was run before implementation and failed with missing authorization modules, confirming the red state.
- `pytest -q` was run after implementation and passed.
- `python -m compileall backend` passed.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` passed with 12 tests.
- Direct Python import check first failed until `PYTHONPATH` was pointed at `backend`; with `$env:PYTHONPATH=(Resolve-Path 'backend').Path`, the backend imports passed.
- PyPI JSON registry checks were run for FastAPI, Pydantic, Uvicorn, pytest, SQLAlchemy, Alembic, asyncpg, neo4j, Celery, Redis, httpx, Ruff, and mypy.
- npm registry checks were run for Next.js, React, React DOM, TypeScript, ESLint, Vitest, and Playwright.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` passed with 16 tests after the latest-stable baseline and FastAPI shell were added.
- `python -m pip install SQLAlchemy==2.0.49 alembic==1.18.4` installed the local verification dependencies matching `pyproject.toml`.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` passed with 27 tests after ORM, Alembic, route guard, and audit-service additions.
- `$env:PYTHONDONTWRITEBYTECODE='1'; python -B -m alembic -c alembic.ini upgrade head --sql` rendered the initial migration SQL successfully when redirected to a temporary file.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` passed with 34 tests after org registry and access-index additions.
- Alembic offline SQL render confirmed revision `20260510_0002` creates `org_units` and `youtube_channels`.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` passed with 41 tests after channel registry API additions.
- `pytest tests/org/test_sql_channel_registry.py -q -p no:cacheprovider` first failed on missing SQL repository/access loader APIs, then passed with 2 tests after implementation.
- `pytest tests/api/test_sql_backed_channel_dependencies.py -q -p no:cacheprovider` first failed because `create_app(database_url=...)` did not exist, then passed with 1 test after dependency wiring.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` passed with 44 tests after SQL repository and database-backed dependency wiring.
- Generated Python `__pycache__` directories were removed after test execution.
- `pytest tests/auth/test_sql_audit_sink.py tests/api/test_sql_backed_channel_dependencies.py -q -p no:cacheprovider` first failed because database-backed routes still used the in-memory audit sink, then passed with 3 tests after SQL audit wiring.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` passed with 46 tests after persistent audit logging was added.
- Generated Python `__pycache__` directories were removed again after the final test run.
- `pytest tests/api/test_groups_api.py -q -p no:cacheprovider` first failed with `/groups` returning 404, then passed with 4 tests after group registry routes and SQL repository wiring were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` passed with 50 tests after channel-group APIs were added.
- Generated Python `__pycache__` directories were removed after the channel-group test run.
- `pytest tests/api/test_sql_backed_channel_dependencies.py -q -p no:cacheprovider` first failed because `create_app()` ignored `UMS_DATABASE_URL`, then passed with 3 tests after settings wiring.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` passed with 51 tests after environment-backed app startup was added.
- Generated Python `__pycache__` directories were removed after the environment-backed startup test run.
- `pytest tests/api/test_connectors_api.py -q -p no:cacheprovider` first failed with missing `/connectors` routes, then passed with 4 tests after connector repository and API wiring were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` passed with 55 tests after connector control-plane APIs were added.
- Generated Python `__pycache__` directories were removed after the connector test run.
- `pytest tests/api/test_finance_close_api.py -q -p no:cacheprovider` first failed on the missing finance model module, then passed with 5 tests after finance-close control APIs were added.
- `pytest tests/db/test_finance_close_models.py tests/db/test_finance_close_migration.py -q -p no:cacheprovider` passed with 3 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider` passed with 63 tests after finance-close control APIs were added.
- Generated Python `__pycache__` directories were removed after the finance-close test run.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp tests/api/test_raw_report_files_api.py tests/db/test_raw_report_file_models.py tests/db/test_raw_report_file_migration.py` first failed because `report_models` did not exist, then passed with 10 tests after raw report-file metadata APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp tests/api tests/db` passed with 96 tests after the raw report-file API was added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp` passed with 135 tests after the raw report-file API was added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; python -B -m alembic -c alembic.ini upgrade head --sql` rendered migrations through `20260510_0006`.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp tests/api/test_revenue_explanations_api.py tests/db/test_explanation_models.py tests/db/test_explanation_migration.py` first failed because `explanation_models` did not exist, then passed with 5 tests after explain-number APIs were added.
- `git diff --check` passed with CRLF conversion warnings only.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp tests/api tests/db tests/finance` passed with 105 tests after explain-number APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp` passed with 140 tests after explain-number APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; python -B -m alembic -c alembic.ini upgrade head --sql` rendered migrations through `20260510_0007`.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp tests/api/test_exports_api.py tests/db/test_export_job_models.py tests/db/test_export_job_migration.py` first failed because `ExportJobORM` did not exist, then passed with 11 tests after export-job APIs were added.
- `git diff --check` passed again with CRLF conversion warnings only after export-job APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp tests/api tests/db tests/finance` passed with 116 tests after export-job APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp` passed with 151 tests after export-job APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; python -B -m alembic -c alembic.ini upgrade head --sql` rendered migrations through `20260510_0008`.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp tests/api/test_audit_api.py` first failed with missing `/audit/events`, then passed with 3 tests after the guarded audit-log API was added.
- `git diff --check` passed again with CRLF conversion warnings only after audit-log APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp tests/api tests/db tests/finance` passed with 119 tests after audit-log APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp .pytest-tmp` passed with 154 tests after audit-log APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; python -B -m alembic -c alembic.ini upgrade head --sql` still rendered migrations through `20260510_0008`.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-permissions-red" tests/api/test_user_permissions_api.py` first failed with missing `/users/{user_id}/permissions` routes.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-permissions-green" tests/api/test_user_permissions_api.py` passed with 7 tests after direct permission grant APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-security" tests/api/test_user_roles_api.py tests/api/test_user_permissions_api.py` passed with 16 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-full-permissions"` passed with 182 tests.
- `git diff --check` passed with CRLF conversion warnings only.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-accounts-red" tests/api/test_user_accounts_api.py` first failed with missing `/users` account lifecycle routes.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-accounts-green" tests/api/test_user_accounts_api.py` passed with 7 tests after user account lifecycle APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-security-accounts" tests/api/test_user_accounts_api.py tests/api/test_user_roles_api.py tests/api/test_user_permissions_api.py` passed with 23 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-full-user-accounts"` passed with 192 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-accounts-noop-red" tests/api/test_user_accounts_api.py::test_user_update_requires_at_least_one_account_field` first failed because no-op updates returned `200`.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-accounts-noop-green" tests/api/test_user_accounts_api.py::test_user_update_requires_at_least_one_account_field` passed after update validation was added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-accounts-final" tests/api/test_user_accounts_api.py` passed with 8 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-security-accounts-final" tests/api/test_user_accounts_api.py tests/api/test_user_roles_api.py tests/api/test_user_permissions_api.py` passed with 24 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-full-user-accounts-final"` passed with 193 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-userroles" tests/api/test_user_roles_api.py` first failed with missing `/users/{user_id}/roles`, then passed with 6 tests after user-role assignment APIs were added.
- `git diff --check` passed with CRLF conversion warnings only after user-role assignment APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-broad" tests/api tests/db tests/auth` passed with 146 tests after user-role assignment APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-full"` passed with 171 tests after user-role assignment APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; python -B -m alembic -c alembic.ini upgrade head --sql` still rendered migrations through `20260510_0008`.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-db-principals-red" tests/api/test_database_principals.py` first failed because `create_app()` had no `authz_source` argument.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-db-principals-green" tests/api/test_database_principals.py` passed with 4 tests after DB-backed principal loading was added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-db-principals-docs" tests/api/test_database_principals.py` passed with 4 tests after documentation updates.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-authz-slice" tests/api/test_database_principals.py tests/api/test_user_roles_api.py tests/api/test_user_permissions_api.py tests/auth` passed with 43 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-full-db-principals"` passed with 189 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-access-red" tests/api/test_user_access_read_api.py` first exposed a test-fixture duplicate-scope issue under SQLite, then the corrected red run failed with missing `GET /users` and `GET /users/{user_id}/access`.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-access-green" tests/api/test_user_access_read_api.py` passed with 5 tests after user access read APIs were added.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-access-related" tests/api/test_user_access_read_api.py tests/api/test_user_accounts_api.py tests/api/test_user_roles_api.py tests/api/test_user_permissions_api.py` passed with 56 tests.
- `python -m ruff check backend tests` was attempted and showed existing repo-wide style debt outside this slice; changed-file Ruff validation was used for this branch.
- `python -m ruff check backend/ums_smart_revenue/api/users.py backend/ums_smart_revenue/auth/users.py tests/api/test_user_access_read_api.py` passed.
- `git diff --check` passed with CRLF conversion warnings only.
- Full-suite pytest exceeded the local command timeout in this Windows workspace, so orphaned pytest processes were stopped and the suite was validated in captured chunks.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-access-nonapi" tests/auth tests/db tests/finance tests/graph tests/org tests/test_version_baseline.py` passed with 74 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-access-related-final" tests/api/test_user_access_read_api.py tests/api/test_user_accounts_api.py tests/api/test_user_roles_api.py tests/api/test_user_permissions_api.py` passed with 56 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-api-core-final" tests/api/test_app.py tests/api/test_audit_api.py tests/api/test_channels_api.py tests/api/test_connectors_api.py tests/api/test_groups_api.py tests/api/test_guarded_routes.py tests/api/test_sql_backed_channel_dependencies.py` passed with 42 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-api-revenue-final" tests/api/test_exports_api.py tests/api/test_finance_close_api.py tests/api/test_manual_overrides_api.py tests/api/test_raw_report_files_api.py tests/api/test_revenue_explanations_api.py tests/api/test_revenue_facts_api.py` passed with 53 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-db-principals-final" tests/api/test_database_principals.py` passed with 21 tests.
- `python -m ruff check backend/ums_smart_revenue/api/users.py backend/ums_smart_revenue/auth/users.py tests/api/test_user_access_read_api.py` passed after PR #7 pre-merge edge coverage was added.
- `python -B -m pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-access-edge" tests/api/test_user_access_read_api.py` passed with 9 tests after user-list cursor pagination and access-profile edge cases were added.
- `python -B -m pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-access-related-pr7" tests/api/test_user_access_read_api.py tests/api/test_user_accounts_api.py tests/api/test_user_roles_api.py tests/api/test_user_permissions_api.py` passed with 60 tests.
- `git diff --check` passed with CRLF conversion warnings only after the PR #7 pre-merge fix.
- `python -m ruff check backend/ums_smart_revenue/api/users.py backend/ums_smart_revenue/auth/users.py tests/api/test_user_access_read_api.py` passed after the user-list offset cap was added.
- `python -B -m pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-access-offset-cap" tests/api/test_user_access_read_api.py` passed with 18 tests after the offset cap and query-boundary coverage were added.
- `python -B -m pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-user-access-related-offset-cap" tests/api/test_user_access_read_api.py tests/api/test_user_accounts_api.py tests/api/test_user_roles_api.py tests/api/test_user_permissions_api.py` passed with 69 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-close-readiness-red" tests/api/test_finance_close_api.py` first failed because required channels with no facts did not block close readiness.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-close-readiness-target" tests/api/test_finance_close_api.py::test_finance_close_readiness_blocks_missing_required_revenue_facts tests/api/test_finance_close_api.py::test_finance_close_readiness_ignores_performance_only_channels` passed with 2 tests.
- `$env:PYTHONDONTWRITEBYTECODE='1'; pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-close-readiness-file" tests/api/test_finance_close_api.py` passed with 14 tests.
- `python -m ruff check backend/ums_smart_revenue/finance/month_close_readiness.py` passed.
- `$env:PYTHONDONTWRITEBYTECODE='1'; python -B -m pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-pr8-red" tests/api/test_finance_close_api.py::test_finance_lock_requests_pessimistic_readiness_recheck` first failed because lock attempts did not request a pessimistic readiness recheck.
- `$env:PYTHONDONTWRITEBYTECODE='1'; python -B -m pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-pr8-green-lock" tests/api/test_finance_close_api.py::test_finance_lock_requests_pessimistic_readiness_recheck` passed after the lock-time recheck started using row-lock mode.
- `$env:PYTHONDONTWRITEBYTECODE='1'; python -B -m pytest -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-pr8-edge" tests/api/test_finance_close_api.py::test_finance_close_readiness_counts_bulk_missing_required_revenue_facts tests/api/test_finance_close_api.py::test_finance_lock_rechecks_after_channel_becomes_revenue_required` passed with 2 tests.
- `pytest tests/api/test_adsense_payments_api.py tests/db/test_adsense_payment_migration.py tests/db/test_adsense_payment_models.py -q` first failed with missing AdSense payment routes, model, table, and migration.
- `pytest tests/api/test_adsense_payments_api.py tests/db/test_adsense_payment_migration.py tests/db/test_adsense_payment_models.py -q` passed with 8 tests after AdSense payment sync/list support was added.
- `python -m ruff check backend/ums_smart_revenue/api/adsense.py backend/ums_smart_revenue/finance/adsense_payments.py tests/api/test_adsense_payments_api.py tests/db/test_adsense_payment_migration.py tests/db/test_adsense_payment_models.py` passed.
- `python -m ruff check --select I backend/ums_smart_revenue/db/finance_models.py backend/ums_smart_revenue/auth/audit.py backend/ums_smart_revenue/app.py` passed.
- `pytest tests/auth tests/db tests/finance tests/graph tests/org tests/test_version_baseline.py -q` passed with 82 tests.
- `python -B -m alembic -c alembic.ini upgrade head --sql` rendered migrations through `20260512_0002`.
- `pytest tests/api/test_app.py tests/api/test_audit_api.py tests/api/test_channels_api.py tests/api/test_connectors_api.py tests/api/test_database_principals.py tests/api/test_groups_api.py tests/api/test_guarded_routes.py tests/api/test_sql_backed_channel_dependencies.py -q` passed with 63 tests.
- `pytest tests/api/test_adsense_payments_api.py tests/api/test_exports_api.py tests/api/test_finance_close_api.py tests/api/test_manual_overrides_api.py tests/api/test_raw_report_files_api.py tests/api/test_revenue_explanations_api.py tests/api/test_revenue_facts_api.py -q` passed with 65 tests.
- `pytest tests/api/test_user_access_read_api.py tests/api/test_user_accounts_api.py tests/api/test_user_permissions_api.py tests/api/test_user_roles_api.py -q` passed with 69 tests.
- `git diff --check` passed with CRLF conversion warnings only.

## Remaining Next Steps
- Expand SQL audit persistence to each new sensitive endpoint as those routes are added.
- Add broader integration tests around real API routes as modules are built.
- Add a concrete secret-manager adapter after UMS chooses the provider; the current foundation stores external encrypted secret references only.
- Replace the trusted gateway identity headers with the chosen corporate identity provider integration; database-backed authorization is now available behind `UMS_AUTHZ_SOURCE=database`.

## Unresolved Assumptions
- Final identity provider is not specified.
- Exact TV/News sector IDs are not specified, so tests use placeholder sector ids.
- No frontend exists yet, so UI permission metadata is exposed as backend metadata functions rather than a frontend package.
