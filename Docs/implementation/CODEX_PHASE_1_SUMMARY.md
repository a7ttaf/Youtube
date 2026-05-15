# UMS Phase 1 Authorization Foundation Summary

## What Was Implemented
Created the first technical foundation for the UMS Smart Revenue Control Center security model:
- role and permission design documentation;
- permission matrix with scope and audit rules;
- backend Python authorization constants, models, seed definitions, scoped access policy helpers, guard helpers, audit event definitions, and UI-facing metadata;
- PostgreSQL starter schema and seed SQL for users, roles, permissions, scopes, assignments, grants, connector credentials, and audit logs;
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
- added FastAPI `/adsense/payments` guarded by global or requested finance-month `finance.view_finalized_payments`;
- rejected AdSense payment sync for locked finance months;
- audited payment sync as `ADSENSE_PAYMENT_SYNCED` and finalized-payment reads as `PAYMENT_VIEWED`;
- kept this slice to payment metadata and did not add fake revenue calculations, bank matching, Google password storage, or Neo4j financial source-of-truth behavior.

Twenty-second continuation update:
- added deterministic month-level payment matching that compares selected YouTube revenue facts to paid AdSense payment rows;
- added FastAPI `GET /revenue/months/{month}/payment-match`;
- required global `finance.view_revenue` and global `finance.view_finalized_payments` for holding-level match reads;
- audited payment-match reads as both `REVENUE_VIEWED` and `PAYMENT_VIEWED`;
- reported non-paid AdSense records while excluding them from the paid amount;
- kept the slice limited to source-of-truth comparison and did not invent tax, bank, net-revenue, or allocation logic.

Twenty-third continuation update:
- added SQL bank reconciliation receipt metadata with an Alembic migration for `bank_reconciliation_entries`;
- added a SQLAlchemy bank reconciliation repository that upserts finance-provided receipt rows by `(month, bank_reference)`;
- added FastAPI `POST /revenue/months/{month}/bank-reconciliation` guarded by `finance.manage_bank_reconciliation` on the requested finance-month scope;
- added FastAPI `GET /revenue/months/{month}/bank-reconciliation` guarded by `finance.view_bank_reconciliation` and `finance.view_finalized_payments` on the requested finance-month scope;
- rejected bank reconciliation writes for locked finance months;
- audited receipt writes as `BANK_RECONCILIATION_RECORDED` and summary reads as both `BANK_RECONCILIATION_VIEWED` and `PAYMENT_VIEWED`;
- computed only a month-level bank gap from paid USD AdSense payments versus finance-normalized bank receipts, without allocating transfer/FX gaps, calculating net revenue, automating exchange rates, or making Neo4j a financial source of truth.

Twenty-fourth continuation update:
- added a deterministic month-level smart-alert engine for the internal finance command center;
- added FastAPI `GET /revenue/months/{month}/smart-alerts`;
- derived alerts only from SQL-backed revenue facts, manual overrides, AdSense payments, bank reconciliation entries, and finance month-close state;
- required global `finance.view_revenue`, global `analytics.view_confidence`, and finance-month scoped finalized-payment and bank-reconciliation visibility;
- audited smart-alert reads as `REVENUE_VIEWED`, `PAYMENT_VIEWED`, and `BANK_RECONCILIATION_VIEWED`;
- reported `MISSING_REVENUE_SOURCE`, `PAYMENT_NOT_MATCHED`, `BANK_AMOUNT_MISSING`, `UNEXPLAINED_GAP_HIGH`, `MONTH_NOT_LOCKED`, and `MANUAL_OVERRIDE_USED` without inventing missing revenue, allocating bank gaps, or calculating net revenue.

Twenty-fifth continuation update:
- added a deterministic source-backed net-revenue summary foundation;
- added FastAPI `GET /revenue/months/{month}/net-revenue`;
- supported `global`, `sector`, `company`, and `channel` revenue scopes with `finance.view_revenue` and `analytics.view_confidence` checks;
- calculated net revenue only from official SQL revenue fact `net_revenue_usd` plus approved manual revenue overrides;
- reported `NET_REVENUE_SOURCE_MISSING` when a primary source lacks net revenue instead of inventing tax, deductions, or allocated bank/payment gaps;
- audited net-revenue summary reads as `REVENUE_VIEWED`;
- kept calculated values read-only and did not persist `channel_net_revenue` rows yet.

Twenty-sixth continuation update:
- added a deterministic finance workbook preview foundation for `FINANCE_EXCEL` export jobs;
- added FastAPI `GET /exports/{export_id}/finance-workbook-preview`;
- returns the planned finance workbook sheet manifest, executive summary, and source summaries without generating XLSX/PDF/slide artifacts or marking jobs complete;
- requires revenue export permission, scoped revenue visibility, and finance-month scoped finalized-payment and bank-reconciliation visibility;
- derives preview data only from SQL source-of-truth services: revenue facts, manual overrides, AdSense payments, bank reconciliation rows, finance close state, payment match, bank confirmation, net revenue, and smart alerts;
- audits preview reads as `REVENUE_VIEWED`, `PAYMENT_VIEWED`, and `BANK_RECONCILIATION_VIEWED`;
- rejects non-`FINANCE_EXCEL` jobs for workbook preview instead of silently fabricating another artifact type.

Twenty-seventh continuation update:
- added pinned stable `openpyxl==3.1.5` for XLSX generation and recorded it in the version baseline;
- added deterministic finance workbook XLSX generation from the existing `FINANCE_EXCEL` preview object;
- added FastAPI `GET /exports/{export_id}/finance-workbook.xlsx`;
- returns an on-demand XLSX response with the planned finance workbook sheets;
- reuses the same revenue export, scoped revenue, finalized-payment, and bank-reconciliation permission checks as preview;
- audits workbook downloads as `REVENUE_VIEWED`, `PAYMENT_VIEWED`, `BANK_RECONCILIATION_VIEWED`, and `EXPORT_DOWNLOADED`;
- keeps generated workbooks ephemeral in this phase: no object-storage upload, no `file_url` update, and no export-job completion mutation.

Twenty-eighth continuation update:
- verified and pinned stable `ReportLab==4.5.1` for PDF generation and `pypdf==6.11.0` for PDF test extraction;
- added deterministic executive finance PDF generation for `EXECUTIVE_PDF` export jobs;
- added FastAPI `GET /exports/{export_id}/executive.pdf`;
- returns an on-demand PDF response with the planned executive management sections;
- reuses the same revenue export, scoped revenue, finalized-payment, and bank-reconciliation permission checks as workbook downloads;
- audits executive PDF downloads as `REVENUE_VIEWED`, `PAYMENT_VIEWED`, `BANK_RECONCILIATION_VIEWED`, and `EXPORT_DOWNLOADED`;
- keeps generated PDFs ephemeral in this phase: no object-storage upload, no `file_url` update, and no export-job completion mutation.

Twenty-ninth continuation update:
- verified and pinned stable `python-pptx==1.0.2` for PowerPoint deck generation;
- added deterministic branded finance slide-pack generation for `BRANDED_SLIDE_PACK` export jobs;
- added FastAPI `GET /exports/{export_id}/branded-slide-pack.pptx`;
- returns an on-demand PPTX response with the planned 10-slide management deck;
- reuses the same revenue export, scoped revenue, finalized-payment, and bank-reconciliation permission checks as workbook downloads;
- audits branded slide-pack downloads as `REVENUE_VIEWED`, `PAYMENT_VIEWED`, `BANK_RECONCILIATION_VIEWED`, and `EXPORT_DOWNLOADED`;
- keeps generated slide packs ephemeral in this phase: no object-storage upload, no `file_url` update, and no export-job completion mutation.

Thirtieth continuation update:
- added filesystem-backed persistent export artifact storage with object-storage-like `file-store://exports/{export_id}/{filename}` URIs;
- added export job artifact metadata fields for filename, content type, byte size, SHA-256 checksum, and failure reason;
- added repository transitions that mark generated workbook, executive PDF, and branded slide-pack jobs `COMPLETED` after artifact persistence;
- added failure handling that marks a job `FAILED` when artifact storage is unavailable and does not emit `EXPORT_DOWNLOADED`;
- kept finance export generation guarded by the existing revenue export, revenue visibility, finalized-payment, and bank-reconciliation permission checks;
- kept SQL/PostgreSQL as source of truth and did not add Neo4j or fake revenue logic.

Thirty-first continuation update:
- snapshotted the resolved YouTube channel IDs into `export_jobs.scope_channel_ids` at job creation so non-global exports cannot drift when their source group, sector, or company membership changes;
- added Alembic migration `20260513_0006_export_job_scope_channel_snapshot.py` to introduce the nullable JSONB snapshot column;
- exposed `scope_channel_ids` on `ExportJobEntry.to_api()` and the `POST /exports`, `GET /exports`, and `GET /exports/{export_id}` responses;
- routed finance source-summary generation through the stored snapshot, falling back to live org-index/group resolution only for pre-snapshot legacy rows;
- added a regression test proving that mutating a group after queueing an export does not change the channel set returned by re-reads of the same `export_id`.

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
- `backend/ums_smart_revenue/reports/branded_slide_pack.py`
- `backend/ums_smart_revenue/reports/executive_pdf.py`
- `backend/ums_smart_revenue/reports/finance_workbook.py`
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
- `backend/ums_smart_revenue/db/alembic/versions/20260513_0001_bank_reconciliation.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260513_0002_retire_graph_permissions.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260513_0003_export_artifact_metadata.py`
- `backend/ums_smart_revenue/config/__init__.py`
- `backend/ums_smart_revenue/config/settings.py`
- `backend/ums_smart_revenue/config/version_baseline.py`
- `backend/ums_smart_revenue/connectors/__init__.py`
- `backend/ums_smart_revenue/connectors/credentials.py`
- `backend/ums_smart_revenue/finance/__init__.py`
- `backend/ums_smart_revenue/finance/adsense_payments.py`
- `backend/ums_smart_revenue/finance/bank_reconciliation.py`
- `backend/ums_smart_revenue/finance/explanations.py`
- `backend/ums_smart_revenue/finance/month_close.py`
- `backend/ums_smart_revenue/finance/net_revenue.py`
- `backend/ums_smart_revenue/finance/payment_matching.py`
- `backend/ums_smart_revenue/finance/smart_alerts.py`
- `backend/ums_smart_revenue/org/__init__.py`
- `backend/ums_smart_revenue/org/access_index.py`
- `backend/ums_smart_revenue/org/bootstrap_registry.py`
- `backend/ums_smart_revenue/org/channel_groups.py`
- `backend/ums_smart_revenue/org/channel_registry.py`
- `backend/ums_smart_revenue/org/sql_channel_groups.py`
- `backend/ums_smart_revenue/org/sql_channel_registry.py`
- `backend/ums_smart_revenue/reports/__init__.py`
- `backend/ums_smart_revenue/reports/artifact_storage.py`
- `backend/ums_smart_revenue/reports/exports.py`
- `backend/ums_smart_revenue/reports/raw_files.py`
- `tests/conftest.py`
- `tests/test_version_baseline.py`
- `tests/api/test_audit_api.py`
- `tests/api/test_adsense_payments_api.py`
- `tests/api/test_bank_reconciliation_api.py`
- `tests/api/test_app.py`
- `tests/api/test_channels_api.py`
- `tests/api/test_connectors_api.py`
- `tests/api/test_database_principals.py`
- `tests/api/test_export_preview_api.py`
- `tests/api/test_exports_api.py`
- `tests/api/test_finance_close_api.py`
- `tests/api/test_groups_api.py`
- `tests/api/test_payment_match_api.py`
- `tests/api/test_raw_report_files_api.py`
- `tests/api/test_net_revenue_api.py`
- `tests/api/test_revenue_explanations_api.py`
- `tests/api/test_smart_alerts_api.py`
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
- `tests/db/test_bank_reconciliation_migration.py`
- `tests/db/test_bank_reconciliation_models.py`
- `tests/db/test_export_artifact_migration.py`
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
- `tests/finance/test_payment_matching.py`
- `tests/finance/test_bank_reconciliation.py`
- `tests/finance/test_net_revenue.py`
- `tests/finance/test_smart_alerts.py`
- `tests/org/test_sql_channel_registry.py`
- `tests/reports/test_branded_slide_pack.py`
- `tests/reports/test_artifact_storage.py`
- `tests/reports/test_executive_pdf.py`
- `tests/reports/test_finance_workbook_preview.py`
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
Permissions are grouped around analytics, finance, finance control, exports, registry, connectors, raw files, audit, users, roles, and platform settings. Sensitive permissions are flagged for audit-on-use.

Scopes supported:
- `global`
- `sector`
- `company`
- `channel`
- `finance-month`
- `export`
- `connector`

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
- `can_manage_connectors(user)`
- `can_assign_roles(user)`

## Retired Graph Summary
Neo4j and graph-specific permissions are retired from the active architecture. The backend no longer contains a graph service package, graph-read scope, graph permissions, or graph API helper. Relationship and hierarchy views must be SQL/warehouse-backed and use the same authorization checks as the underlying finance and analytics APIs.

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
- Corporate Admin can assign roles without default finance visibility.
- Retired graph permissions are absent from the active role catalog.
- The active SQLAlchemy security model no longer allows `graph-read` scopes.
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
- Monthly payment matching selects YouTube revenue facts, compares them with paid AdSense payment rows, detects gaps, and excludes non-paid AdSense rows from the paid total.
- Finance Viewer can read holding-level monthly payment-match summaries with both `REVENUE_VIEWED` and `PAYMENT_VIEWED` audit events.
- Assistant Analyst and company-scoped finance roles cannot read holding-level payment-match summaries by default.
- Payment-match reads reject non-USD currency requests until exchange-rate support exists and exclude non-USD AdSense payment rows from USD matching.
- Finance Admin and Finance Approver can record finance-provided bank receipt metadata with a sensitive `BANK_RECONCILIATION_RECORDED` audit event.
- Finance Viewer can read monthly bank reconciliation summaries with both `BANK_RECONCILIATION_VIEWED` and `PAYMENT_VIEWED` audit events.
- Finance-month-scoped bank reconciliation grants work only for the matching month.
- Assistant Analyst cannot read bank reconciliation summaries by default.
- Finance Viewer cannot record bank reconciliation receipt metadata by default.
- Locked finance months reject bank reconciliation writes and persist no receipt rows.
- Bank reconciliation ORM and migration preserve receipt date, bank reference, original receipt amount/currency, finance-normalized USD amount, transfer fee, FX difference, source report id, recorder metadata, and month/reference uniqueness.
- Monthly bank reconciliation compares paid USD AdSense payment rows with finance-normalized bank receipt rows, reports month-level gaps, and excludes non-paid or non-USD payment rows from the paid USD total.
- Monthly smart alerts combine existing SQL-backed finance signals into command-center alerts without calculating new money values.
- Finance Viewer can read month smart alerts with sensitive revenue, payment, and bank-reconciliation audit events.
- Assistant Analyst cannot read month smart alerts by default.
- Smart alerts report payment mismatch, missing bank amount, high unexplained gaps, unlocked month state, missing revenue source, and approved manual override usage.
- Channel net revenue uses official source `net_revenue_usd` and approved manual revenue overrides only.
- Month net revenue totals include calculated channel net/deductions while counting channels whose primary source lacks net values.
- Finance Viewer can read scoped month net-revenue summaries with a sensitive `REVENUE_VIEWED` audit event.
- Assistant Analyst cannot read month net-revenue summaries by default, and non-USD net-revenue reads are rejected until exchange-rate support exists.
- Finance Admin can preview a `FINANCE_EXCEL` workbook from an export job with revenue, payment, and bank-reconciliation audit events.
- Export Operator cannot preview finance workbooks without revenue-export and finance visibility.
- Finance workbook preview rejects analytics export jobs instead of fabricating workbook output.
- Finance workbook preview returns the planned sheet manifest and source-backed executive summary from existing finance services.
- Finance Admin can download a generated `FINANCE_EXCEL` workbook with the planned sheet names and source-backed values.
- Finance workbook downloads are audited as sensitive export-download activity.
- Finance Admin can download generated executive PDFs and branded slide packs with source-backed management summaries.
- Generated workbook, PDF, and slide-pack downloads persist artifact files, `file_url`, filename, content type, byte size, SHA-256 checksum, and mark jobs `COMPLETED`.
- Artifact storage failure marks the export job `FAILED` with `failure_reason` and emits no `EXPORT_DOWNLOADED` audit event.
- The filesystem artifact store writes atomically under the configured root and rejects unsafe filenames.
- The export artifact Alembic migration adds metadata and checksum/byte-size constraints.

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
- `pytest tests/finance/test_payment_matching.py tests/api/test_payment_match_api.py -q` first failed with missing payment-match service/API support, then passed after implementation.
- `python -m ruff check backend/ums_smart_revenue/finance/payment_matching.py tests/finance/test_payment_matching.py tests/api/test_payment_match_api.py` passed.
- `python -m ruff check --select I backend/ums_smart_revenue/api/revenue.py backend/ums_smart_revenue/finance/adsense_payments.py` passed.
- `python -B -m pytest tests/finance/test_payment_matching.py tests/api/test_payment_match_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-payment-match-focused-3"` passed with 10 tests.
- `python -B -m pytest tests/api/test_adsense_payments_api.py tests/api/test_exports_api.py tests/api/test_finance_close_api.py tests/api/test_manual_overrides_api.py tests/api/test_payment_match_api.py tests/api/test_raw_report_files_api.py tests/api/test_revenue_explanations_api.py tests/api/test_revenue_facts_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-payment-match-api-finance-3"` passed with 70 tests.
- `python -B -m pytest tests/auth tests/db tests/finance tests/graph tests/org tests/test_version_baseline.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-payment-match-nonapi-2"` passed with 87 tests.
- `python -B -m pytest tests/api/test_app.py tests/api/test_audit_api.py tests/api/test_channels_api.py tests/api/test_connectors_api.py tests/api/test_database_principals.py tests/api/test_groups_api.py tests/api/test_guarded_routes.py tests/api/test_sql_backed_channel_dependencies.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-payment-match-api-core"` passed with 63 tests.
- `python -B -m pytest tests/api/test_user_access_read_api.py tests/api/test_user_accounts_api.py tests/api/test_user_permissions_api.py tests/api/test_user_roles_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-payment-match-user-access"` passed with 69 tests.
- `git diff --check` passed with CRLF conversion warnings only after payment-match implementation.
- `python -B -m pytest tests/finance/test_bank_reconciliation.py tests/db/test_bank_reconciliation_models.py tests/db/test_bank_reconciliation_migration.py tests/api/test_bank_reconciliation_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-red-2"` first failed with missing bank reconciliation service, ORM, migration, and guarded API support.
- `python -B -m pytest tests/api/test_bank_reconciliation_api.py::test_finance_month_scoped_admin_records_matching_month tests/api/test_bank_reconciliation_api.py::test_finance_month_scoped_viewer_cannot_read_another_month -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-month-scope-red"` first failed because bank reconciliation endpoint checks used global scope only.
- `python -B -m pytest tests/api/test_bank_reconciliation_api.py::test_finance_month_scoped_admin_records_matching_month tests/api/test_bank_reconciliation_api.py::test_finance_month_scoped_viewer_cannot_read_another_month -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-month-scope-green"` passed after bank reconciliation checks moved to the requested finance-month scope.
- `python -B -m pytest tests/finance/test_bank_reconciliation.py tests/db/test_bank_reconciliation_models.py tests/db/test_bank_reconciliation_migration.py tests/api/test_bank_reconciliation_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-focused-final-2"` passed with 12 tests after implementation, ORM formatting cleanup, and finance-month scope coverage.
- `python -m ruff check backend/ums_smart_revenue/finance/bank_reconciliation.py backend/ums_smart_revenue/db/alembic/versions/20260513_0001_bank_reconciliation.py tests/finance/test_bank_reconciliation.py tests/db/test_bank_reconciliation_models.py tests/db/test_bank_reconciliation_migration.py tests/api/test_bank_reconciliation_api.py` passed.
- `python -m ruff check --select I backend/ums_smart_revenue/api/revenue.py backend/ums_smart_revenue/db/finance_models.py backend/ums_smart_revenue/auth/permissions.py backend/ums_smart_revenue/auth/seed.py backend/ums_smart_revenue/auth/audit.py backend/ums_smart_revenue/auth/user_permissions.py backend/ums_smart_revenue/api/users.py` passed.
- The combined finance API regression command timed out locally, so the same files were validated individually to avoid masking results behind Windows test runtime noise.
- `python -B -m pytest tests/api/test_adsense_payments_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-adsense-file"` passed with 6 tests.
- `python -B -m pytest tests/api/test_exports_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-exports-file"` passed with 9 tests.
- `python -B -m pytest tests/api/test_finance_close_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-finance-close-file"` passed with 18 tests.
- `python -B -m pytest tests/api/test_manual_overrides_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-manual-overrides-file"` passed with 8 tests.
- `python -B -m pytest tests/api/test_payment_match_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-payment-match-file"` passed with 5 tests.
- `python -B -m pytest tests/api/test_raw_report_files_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-raw-report-files"` passed with 8 tests.
- `python -B -m pytest tests/api/test_revenue_explanations_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-explanations-file"` passed with 3 tests.
- `python -B -m pytest tests/api/test_revenue_facts_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-revenue-facts-file"` passed with 13 tests.
- `python -B -m pytest tests/api/test_user_permissions_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-user-permissions"` passed with 7 tests.
- `python -B -m pytest tests/auth tests/db tests/finance tests/graph tests/org tests/test_version_baseline.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-nonapi-final"` passed with 92 tests.
- `python -B -m pytest tests/api/test_user_access_read_api.py tests/api/test_user_accounts_api.py tests/api/test_user_permissions_api.py tests/api/test_user_roles_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-bank-reconciliation-user-access-final"` passed with 69 tests.
- `python -B -m alembic -c alembic.ini upgrade head --sql > "$env:TEMP\ums-bank-reconciliation-alembic.sql"` rendered migrations through `20260513_0001`.
- `Select-String -Path "$env:TEMP\ums-bank-reconciliation-alembic.sql" -Pattern "bank_reconciliation_entries|20260513_0001"` confirmed the rendered bank reconciliation table and migration revision.
- `git diff --check` passed with CRLF conversion warnings only after bank reconciliation implementation.
- `python -B -m pytest tests/finance/test_smart_alerts.py tests/api/test_smart_alerts_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-smart-alerts-red-1"` first failed with missing smart-alert module and route support.
- `python -B -m pytest tests/finance/test_smart_alerts.py tests/api/test_smart_alerts_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-smart-alerts-focused-final"` passed with 5 tests after implementation.
- `python -m ruff check backend/ums_smart_revenue/finance/smart_alerts.py tests/finance/test_smart_alerts.py tests/api/test_smart_alerts_api.py` passed.
- `python -m ruff check --select I backend/ums_smart_revenue/api/revenue.py backend/ums_smart_revenue/finance/manual_overrides.py` passed after import ordering.
- `python -B -m pytest tests/auth tests/db tests/finance tests/graph tests/org tests/test_version_baseline.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-smart-alerts-nonapi"` passed with 95 tests.
- `python -B -m pytest tests/api/test_smart_alerts_api.py tests/api/test_payment_match_api.py tests/api/test_bank_reconciliation_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-smart-alerts-api-core"` passed with 14 tests.
- `python -B -m pytest tests/api/test_revenue_facts_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-smart-alerts-revenue-facts"` passed with 13 tests.
- `python -B -m pytest tests/api/test_manual_overrides_api.py tests/api/test_finance_close_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-smart-alerts-api-close-overrides"` passed with 26 tests.
- `python -B -m pytest tests/api/test_adsense_payments_api.py tests/api/test_revenue_explanations_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-smart-alerts-api-adsense-explain"` passed with 9 tests.
- `git diff --check` passed with CRLF conversion warnings only after smart-alert implementation.
- `python -B -m pytest tests/finance/test_net_revenue.py tests/api/test_net_revenue_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-net-revenue-red-1"` first failed with missing net-revenue module and route support.
- `python -B -m pytest tests/finance/test_net_revenue.py tests/api/test_net_revenue_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-net-revenue-green-1"` passed with 6 tests after implementation.
- `python -B -m pytest tests/finance/test_net_revenue.py tests/api/test_net_revenue_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-net-revenue-focused-2"` passed with 6 tests after formatting cleanup.
- `python -m ruff check backend/ums_smart_revenue/finance/net_revenue.py tests/finance/test_net_revenue.py tests/api/test_net_revenue_api.py` passed.
- `python -m ruff check --select I backend/ums_smart_revenue/api/revenue.py backend/ums_smart_revenue/finance/net_revenue.py` passed.
- `python -B -m pytest tests/api/test_net_revenue_api.py tests/api/test_revenue_facts_api.py tests/api/test_revenue_explanations_api.py tests/api/test_manual_overrides_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-net-revenue-api-related-1"` passed with 27 tests.
- `python -B -m pytest tests/finance/test_net_revenue.py tests/finance/test_revenue_summary.py tests/finance/test_revenue_reconciliation.py tests/finance/test_payment_matching.py tests/finance/test_smart_alerts.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-net-revenue-finance-related-1"` passed with 17 tests.
- `python -B -m pytest tests/api/test_smart_alerts_api.py tests/api/test_payment_match_api.py tests/api/test_bank_reconciliation_api.py tests/api/test_finance_close_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-net-revenue-month-routes"` passed with 32 tests.
- `python -B -m pytest tests/auth tests/db tests/finance tests/graph tests/org tests/test_version_baseline.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-net-revenue-nonapi"` passed with 98 tests.
- `python -B -m pytest tests/finance/test_net_revenue.py tests/api/test_net_revenue_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-net-revenue-final-focused"` passed with 6 tests after final API helper formatting.
- `git diff --check` passed with CRLF conversion warnings only after net-revenue implementation.
- `python -B -m pytest tests/reports/test_finance_workbook_preview.py tests/api/test_export_preview_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-export-preview-focused-2"` passed with 5 tests after finance workbook preview implementation.
- `python -m ruff check backend/ums_smart_revenue/reports/finance_workbook.py backend/ums_smart_revenue/api/exports.py tests/reports/test_finance_workbook_preview.py tests/api/test_export_preview_api.py` passed after formatting cleanup.
- `python -B -m pytest tests/reports/test_finance_workbook_preview.py tests/finance/test_net_revenue.py tests/finance/test_payment_matching.py tests/finance/test_bank_reconciliation.py tests/finance/test_smart_alerts.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-export-preview-service-related"` passed with 16 tests.
- The combined API regression command for export preview timed out locally, so the same API files were validated individually to avoid masking results behind Windows test runtime noise.
- `python -B -m pytest tests/api/test_exports_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-export-preview-exports-file"` passed with 9 tests.
- `python -B -m pytest tests/api/test_export_preview_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-export-preview-api-file"` passed with 3 tests.
- `python -B -m pytest tests/api/test_net_revenue_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-export-preview-net-revenue-api"` passed with 3 tests.
- `python -B -m pytest tests/api/test_smart_alerts_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-export-preview-smart-alerts-api"` passed with 2 tests.
- `python -B -m pytest tests/api/test_payment_match_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-export-preview-payment-match-api-final"` passed with 5 tests.
- `python -B -m pytest tests/api/test_bank_reconciliation_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-export-preview-bank-api-final"` passed with 7 tests.
- `git diff --check` passed with CRLF conversion warnings only after finance workbook preview implementation.
- `python -B -m pytest tests/reports/test_finance_workbook_preview.py tests/api/test_export_preview_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-finance-xlsx-red"` first failed because `openpyxl` was not installed.
- `python -m pip install openpyxl==3.1.5` installed the pinned stable XLSX dependency locally.
- `python -B -m pytest tests/reports/test_finance_workbook_preview.py tests/api/test_export_preview_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-finance-xlsx-red-2"` then failed because `build_finance_workbook_xlsx` did not exist.
- `python -B -m pytest tests/reports/test_finance_workbook_preview.py tests/api/test_export_preview_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-finance-xlsx-green-1"` passed with 7 tests after XLSX generation and download route implementation.
- `python -m ruff check backend/ums_smart_revenue/reports/finance_workbook.py backend/ums_smart_revenue/api/exports.py backend/ums_smart_revenue/auth/audit.py backend/ums_smart_revenue/config/version_baseline.py tests/reports/test_finance_workbook_preview.py tests/api/test_export_preview_api.py tests/test_version_baseline.py` passed after formatting and import cleanup.
- `python -B -m pytest tests/reports/test_finance_workbook_preview.py tests/api/test_export_preview_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-finance-xlsx-focused-final-4"` passed with 7 tests.
- `python -B -m pytest tests/reports/test_finance_workbook_preview.py tests/finance/test_net_revenue.py tests/finance/test_payment_matching.py tests/finance/test_bank_reconciliation.py tests/finance/test_smart_alerts.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-finance-xlsx-services-final"` passed with 17 tests.
- The combined adjacent API/audit/version command timed out locally, so the same files were validated individually.
- `python -B -m pytest tests/api/test_exports_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-finance-xlsx-exports-final"` passed with 9 tests.
- `python -B -m pytest tests/auth/test_audit_service.py tests/api/test_audit_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-finance-xlsx-audit-final"` passed with 8 tests.
- `python -B -m pytest tests/test_version_baseline.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-finance-xlsx-version-final"` passed with 2 tests.
- `git diff --check` passed with CRLF conversion warnings only after XLSX generation.
- `python -m pip install reportlab==4.5.1 pypdf==6.11.0` installed the verified stable PDF dependencies locally.
- `python -B -m pytest tests/reports/test_executive_pdf.py tests/api/test_export_preview_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-executive-pdf-red"` first failed because the executive PDF report module and API route did not exist.
- `python -B -m pytest tests/reports/test_executive_pdf.py -vv -s -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-executive-pdf-report-isolate"` passed with 3 tests after the report builder was implemented.
- `python -B -m pytest tests/api/test_export_preview_api.py::test_finance_admin_downloads_generated_executive_pdf_with_audit -vv -s -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-executive-pdf-api-one-new"` passed with 1 test.
- `python -B -m pytest tests/api/test_export_preview_api.py::test_export_operator_cannot_download_executive_pdf -vv -s -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-executive-pdf-api-one-negative"` passed with 1 test.
- `python -m ruff check backend/ums_smart_revenue/reports/executive_pdf.py backend/ums_smart_revenue/api/exports.py backend/ums_smart_revenue/config/version_baseline.py tests/reports/test_executive_pdf.py tests/api/test_export_preview_api.py tests/test_version_baseline.py` passed.
- `python -B -m pytest tests/reports/test_executive_pdf.py tests/reports/test_finance_workbook_preview.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-executive-pdf-reports-final"` passed with 6 tests.
- `python -B -m pytest tests/test_version_baseline.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-executive-pdf-version-final"` passed with 2 tests.
- A parallel run of `tests/api/test_exports_api.py` timed out locally, then `python -B -m pytest tests/api/test_exports_api.py -vv -s -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-executive-pdf-exports-final-solo"` passed with 9 tests.
- `python -B -m pytest tests/api/test_export_preview_api.py -vv -s -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-executive-pdf-preview-final"` passed with 6 tests.
- `python -B -m pytest tests/auth/test_audit_service.py tests/api/test_audit_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-executive-pdf-audit-final"` passed with 8 tests.
- `git diff --check` passed with CRLF conversion warnings only after executive PDF generation.
- `python -m pip install python-pptx==1.0.2` installed the verified stable slide-generation dependency locally.
- `python -B -m pytest tests/reports/test_branded_slide_pack.py tests/api/test_export_preview_api.py::test_finance_admin_downloads_generated_branded_slide_pack_with_audit tests/api/test_export_preview_api.py::test_export_operator_cannot_download_branded_slide_pack -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-branded-slide-red"` first failed because the branded slide-pack report module and API route did not exist.
- `python -B -m pytest tests/reports/test_branded_slide_pack.py tests/api/test_export_preview_api.py::test_finance_admin_downloads_generated_branded_slide_pack_with_audit tests/api/test_export_preview_api.py::test_export_operator_cannot_download_branded_slide_pack -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-branded-slide-green-1"` passed with 5 tests after slide-pack generation and the guarded route were implemented.
- `python -B -m pytest tests/reports/test_branded_slide_pack.py tests/reports/test_executive_pdf.py tests/reports/test_finance_workbook_preview.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-branded-slide-reports-final"` passed with 9 tests.
- `python -B -m pytest tests/test_version_baseline.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-branded-slide-version-final"` passed with 2 tests.
- `python -m ruff check backend/ums_smart_revenue/reports/branded_slide_pack.py backend/ums_smart_revenue/reports/executive_pdf.py backend/ums_smart_revenue/reports/finance_workbook.py backend/ums_smart_revenue/api/exports.py backend/ums_smart_revenue/config/version_baseline.py tests/reports/test_branded_slide_pack.py tests/reports/test_executive_pdf.py tests/reports/test_finance_workbook_preview.py tests/api/test_export_preview_api.py tests/test_version_baseline.py` passed.
- `python -B -m pytest tests/api/test_export_preview_api.py -vv -s -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-branded-slide-preview-final"` passed with 8 tests.
- `python -B -m pytest tests/api/test_exports_api.py -vv -s -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-branded-slide-exports-final"` passed with 9 tests.
- `python -B -m pytest tests/auth/test_audit_service.py tests/api/test_audit_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-branded-slide-audit-final"` passed with 8 tests.
- `git diff --check` passed with CRLF conversion warnings only after branded slide-pack generation.
- `python -B -m pytest tests/reports/test_branded_slide_pack.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-branded-slide-refactor-final"` passed with 3 tests after slide-shape cleanup.

## Neo4j Retirement Update
- Retired Neo4j and graph projections from the active roadmap, target architecture, API map, role model, permission matrix, and version baseline.
- Removed the active graph package, graph policy helper, graph permissions, graph-read scope helper, graph dependency, and graph service tests.
- Added `20260513_0002_retire_graph_permissions.py` to delete retired graph permissions/scopes from existing databases and tighten the active `access_scopes` check constraint.
- Updated seed SQL to clean retired graph rows when reapplied and to stop granting graph permissions to any role.

## Neo4j Retirement Validation
- `python -m ruff check ...` on the changed auth, API, config, DB, migration, and focused test files passed.
- `python -B -m pytest tests/auth/test_policy.py tests/auth/test_user_permissions_repository.py tests/api/test_user_permissions_api.py tests/db/test_security_orm.py tests/db/test_retire_graph_migration.py tests/test_version_baseline.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-retire-graph-focused-2"` passed with 27 tests.
- `python -B -m pytest tests/auth tests/db tests/test_version_baseline.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-retire-graph-auth-db"` passed with 58 tests.
- `python -B -m pytest tests/api/test_user_access_read_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-retire-graph-user-access-read"` passed with 18 tests.
- `python -B -m pytest tests/api/test_user_accounts_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-retire-graph-user-accounts"` passed with 35 tests.
- `python -B -m pytest tests/api/test_user_roles_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-retire-graph-user-roles"` passed with 9 tests.
- `python -B -m pytest tests/api/test_user_permissions_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-retire-graph-user-permissions"` passed with 7 tests.
- `python -B -m pytest tests/finance tests/org tests/reports tests/test_version_baseline.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-retire-graph-domain-reports"` passed with 54 tests.
- `python -B -m pytest tests/api/test_app.py tests/api/test_guarded_routes.py tests/api/test_database_principals.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-retire-graph-api-core"` passed with 29 tests.
- `python -B -m alembic -c alembic.ini upgrade head --sql > "$env:TEMP\ums-retire-graph-alembic.sql"` rendered migrations through `20260513_0002`; the final `access_scopes` check constraint contains only `global`, `sector`, `company`, `channel`, `finance-month`, `export`, and `connector`.
- The combined `tests/api/test_user_access_read_api.py tests/api/test_user_accounts_api.py tests/api/test_user_roles_api.py tests/api/test_user_permissions_api.py` command timed out locally; the same files passed when split by file.
- `rg` verified there are no active references to `VIEW_GRAPH`, `GRAPH_FINANCE`, `can_view_neo4j`, `ums_smart_revenue.graph`, `neo4j==`, `neo4j_driver`, or `neo4j_enterprise` in `backend`, `tests`, or `pyproject.toml`.
- `git diff --check` passed after LF normalization, with only expected Windows CRLF conversion warnings.

## Export Artifact Storage Validation
- `pytest tests/api/test_export_preview_api.py -k "persists_artifact or storage_failure"` first failed with the expected red state: successful downloads left jobs `QUEUED`, and storage failure was not detected.
- `pytest tests/api/test_export_preview_api.py -k "persists_artifact or storage_failure"` passed with 2 tests after implementation.
- `python -m ruff check backend/ums_smart_revenue/reports/artifact_storage.py backend/ums_smart_revenue/reports/exports.py backend/ums_smart_revenue/api/exports.py backend/ums_smart_revenue/db/report_models.py tests/reports/test_artifact_storage.py tests/api/test_export_preview_api.py tests/db/test_export_job_models.py tests/db/test_export_artifact_migration.py` passed.
- `pytest tests/api/test_export_preview_api.py` passed with 15 tests.
- `pytest tests/api/test_exports_api.py` passed with 9 tests.
- `pytest tests/reports/test_artifact_storage.py tests/reports/test_finance_workbook_preview.py tests/reports/test_executive_pdf.py tests/reports/test_branded_slide_pack.py` passed with 13 tests.
- `pytest tests/db/test_export_job_models.py tests/db/test_export_artifact_migration.py tests/db/test_export_job_migration.py` passed with 3 tests.
- `python -B -m alembic -c alembic.ini upgrade head --sql > "$env:TEMP\ums-export-artifacts-alembic.sql"` rendered migrations through `20260513_0003`, including `artifact_filename` and `artifact_checksum_sha256`.
- `git diff --check` passed with Windows CRLF conversion warnings only.

## Outside-CMS Monitor Validation
- Added the scoped `GET /channels/outside-cms` foundation endpoint for the dashboard monitor. It returns only analytics-visible outside-CMS channels, operational revenue-source status, missing-official-revenue flags, recommended actions, and summary counts. It does not expose revenue amounts or finalized payment data.
- Extended `ChannelRegistryEntry` and the SQL-backed channel registry mapping to carry `content_owner_id` and `revenue_source_status` from the SQL `youtube_channels` source of truth.
- `pytest tests/api/test_channels_api.py -k "outside_cms_monitor"` first failed with the expected red state: `/channels/outside-cms` returned `404`.
- `pytest tests/org/test_sql_channel_registry.py -k outside_cms_revenue_metadata` first failed with the expected red state: `ChannelRegistryEntry` did not expose `content_owner_id`.
- `python -m ruff check backend/ums_smart_revenue/api/channels.py backend/ums_smart_revenue/org/channel_registry.py backend/ums_smart_revenue/org/sql_channel_registry.py tests/api/test_channels_api.py tests/org/test_sql_channel_registry.py` passed after scoped formatting cleanup.
- `python -B -m pytest tests/api/test_channels_api.py tests/org/test_sql_channel_registry.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-outside-cms-focused-style-final"` passed with 19 tests.
- `python -B -m pytest tests/org -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-outside-cms-org-style-final"` passed with 10 tests.
- `python -B -m pytest tests/api/test_channels_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-outside-cms-channel-file"` passed with 13 tests.
- `tests/api/test_finance_close_api.py` is very slow on this Windows checkout. A full-file run timed out locally, so the channel-readiness cases were validated individually: missing required revenue facts, performance-only channels, and lock-time recheck after a channel becomes revenue-required each passed.
- `git diff --check` passed with Windows CRLF conversion warnings only after normalizing `backend/ums_smart_revenue/org/sql_channel_registry.py`.

## Channel Smart Issues Validation
- Added `GET /channels/issues` as a scoped metadata-only registry health feed for the dashboard smart issue panel. It currently reports missing company, missing sector, revenue-required outside-CMS channels, and revenue-required channels not assigned to an active group. It does not expose revenue amounts, payment data, or month-specific reconciliation facts.
- Added `backend/ums_smart_revenue/org/channel_issues.py` for deterministic issue classification and summary counts.
- Moved shared group registry dependencies into `backend/ums_smart_revenue/api/registry_dependencies.py` so `/groups`, exports, and `/channels/issues` use the same in-memory or SQL-backed group store without circular API imports.
- `python -B -m pytest tests/api/test_channels_api.py -k "channel_issues" -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-channel-issues-red"` first failed with the expected red state: `/channels/issues` returned `404`.
- `python -B -m pytest tests/api/test_channels_api.py -k "channel_issues" -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-channel-issues-green"` passed with 2 selected tests after implementation.
- `python -B -m pytest tests/api/test_groups_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-channel-issues-groups-smoke"` passed with 7 tests after the shared group dependency refactor.
- `python -m ruff check backend/ums_smart_revenue/api/channels.py backend/ums_smart_revenue/api/groups.py backend/ums_smart_revenue/api/registry_dependencies.py backend/ums_smart_revenue/org/channel_issues.py tests/api/test_channels_api.py tests/api/test_groups_api.py` passed after formatting cleanup.
- `python -B -m pytest tests/api/test_channels_api.py tests/api/test_groups_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-channel-issues-api-files-after-format"` passed with 22 tests.
- Final validation reran the same API files split by file after a combined run timed out locally: `tests/api/test_channels_api.py` passed with 15 tests and `tests/api/test_groups_api.py` passed with 7 tests.
- `python -B -m pytest tests/api/test_app.py tests/api/test_guarded_routes.py tests/api/test_sql_backed_channel_dependencies.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-channel-issues-core-app"` passed with 11 tests.
- After LF normalization, `git diff --check` passed with Windows CRLF conversion warnings only, and `python -B -m pytest tests/api/test_channels_api.py tests/api/test_groups_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-channel-issues-post-lf"` passed with 22 tests.

## Recalculation Dry-Run Foundation Validation
- Added `POST /revenue/recalculate` as a guarded dry-run recalculation request foundation. It requires scoped `finance.view_revenue`, `finance.change_allocation_rule` for the finance month, and a reason; audits `RECALCULATION_REQUESTED`; returns allocation-method validation, scoped source coverage, blockers, and `NO_WRITES_PERFORMED`.
- Added `backend/ums_smart_revenue/finance/recalculation.py` for deterministic allocation-method validation and source/blocker summaries without financial writes or invented revenue/tax/payment-gap calculations.
- Added `tests/api/test_revenue_recalculation_api.py` covering successful audited preview, finance-viewer denial, committed-write rejection, and unknown allocation-method rejection.
- Updated the reconciliation/API/workflow/backlog docs to state the current endpoint is a dry-run foundation, not the full persisted allocation engine.
- `python -B -m pytest tests/api/test_revenue_recalculation_api.py -q` first failed with the expected red state: `/revenue/recalculate` returned `404`.
- `python -B -m pytest tests/api/test_revenue_recalculation_api.py -q` passed with 4 tests after implementation.
- `python -m ruff check backend/ums_smart_revenue/finance/recalculation.py backend/ums_smart_revenue/auth/audit.py tests/api/test_revenue_recalculation_api.py` passed.
- `python -m ruff check --select I backend/ums_smart_revenue/api/revenue.py` passed. A full lint of `api/revenue.py` still reports existing file-wide line-length debt unrelated to this slice.
- `python -B -m pytest tests/api/test_revenue_recalculation_api.py tests/auth/test_audit_service.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-recalculation-audit"` passed with 8 tests.
- `python -B -m pytest tests/api/test_finance_close_api.py -k "allocation_rule" -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-recalculation-finance-close"` passed with 4 selected tests.
- `python -B -m pytest tests/api/test_net_revenue_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-recalculation-net-revenue"` passed with 3 tests.
- `python -B -m pytest tests/api/test_revenue_facts_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-recalculation-facts-long"` passed with 13 tests after a shorter split run timed out locally.
- `git diff --check` passed with Windows CRLF conversion warnings only.

## Currency Exchange-Rate Foundation Validation
- Added SQL source-of-truth FX-rate storage through `currency_exchange_rates` with provider/date/pair uniqueness, positive-rate constraints, ISO currency-pair constraints, source report references, raw provider payload storage, and importer metadata.
- Added `backend/ums_smart_revenue/finance/exchange_rates.py` for deterministic rate normalization, provider/date/pair upsert, and latest-rate lookup on or before an `as_of_date`.
- Added `POST /exchange-rates/sync` guarded by `connectors.run_jobs` for the provider connector scope, with required reason and `EXCHANGE_RATE_SYNCED` audit logging.
- Added `GET /exchange-rates/latest` guarded by global `finance.view_revenue`; it reads stored SQL rates only and does not expose raw provider payloads.
- Added `20260513_0004_currency_exchange_rates.py` and tests for the migration, ORM constraints, repository behavior, and API authorization/audit behavior.
- `python -B -m pytest tests/finance/test_exchange_rates.py tests/api/test_exchange_rates_api.py tests/db/test_currency_exchange_rate_models.py tests/db/test_currency_exchange_rate_migration.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-fx-red"` first failed with expected missing-module/model import errors.
- `python -B -m pytest tests/finance/test_exchange_rates.py tests/api/test_exchange_rates_api.py tests/db/test_currency_exchange_rate_models.py tests/db/test_currency_exchange_rate_migration.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-fx-green-2"` passed with 9 tests.
- `python -m ruff check --fix backend/ums_smart_revenue/finance/exchange_rates.py backend/ums_smart_revenue/api/exchange_rates.py backend/ums_smart_revenue/db/alembic/versions/20260513_0004_currency_exchange_rates.py tests/finance/test_exchange_rates.py tests/api/test_exchange_rates_api.py tests/db/test_currency_exchange_rate_models.py tests/db/test_currency_exchange_rate_migration.py` passed.
- `python -m ruff check --select I backend/ums_smart_revenue/db/finance_models.py backend/ums_smart_revenue/app.py backend/ums_smart_revenue/auth/audit.py` passed. A full lint of `db/finance_models.py` still reports existing file-wide line-length debt before the new FX class.

## Smart Alert Trend Anomaly Validation
- Added deterministic month-over-month revenue movement detection to the SQL-backed smart-alert engine.
- `REVENUE_TREND_ANOMALY` compares each channel's selected primary current-month revenue fact against the selected primary previous-month revenue fact, skips zero-prior channels, and reports the channel id plus current gross revenue, previous gross revenue, and percent movement when the threshold is met.
- The smart-alert API now reads previous-month revenue facts from the SQL source of truth and includes the anomaly alert only for callers already authorized to read sensitive month smart alerts.
- `python -B -m pytest tests/finance/test_smart_alerts.py::test_smart_alerts_detect_month_over_month_revenue_anomaly tests/api/test_smart_alerts_api.py::test_month_smart_alerts_include_month_over_month_revenue_anomaly -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-anomaly-red"` first failed with missing `current_revenue_facts` support and no API anomaly alert.
- `python -B -m pytest tests/finance/test_smart_alerts.py::test_smart_alerts_detect_month_over_month_revenue_anomaly tests/api/test_smart_alerts_api.py::test_month_smart_alerts_include_month_over_month_revenue_anomaly -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-anomaly-green-1"` passed with 2 tests after implementation.
- `python -B -m pytest tests/finance/test_smart_alerts.py tests/api/test_smart_alerts_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-anomaly-smart-alerts"` passed with 8 tests.
- `python -m ruff check backend/ums_smart_revenue/finance/smart_alerts.py tests/finance/test_smart_alerts.py tests/api/test_smart_alerts_api.py` passed.
- `python -m ruff check --select I,F backend/ums_smart_revenue/api/revenue.py` passed. A full lint of `api/revenue.py` still reports existing file-wide line-length debt unrelated to this slice.
- The combined adjacent API regression command timed out locally, so adjacent route coverage was split by file and focused case.
- `python -B -m pytest tests/finance/test_smart_alerts.py tests/api/test_smart_alerts_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-anomaly-smart-alerts-final-seq"` passed with 8 tests.
- `python -B -m pytest tests/api/test_payment_match_api.py::test_finance_viewer_reads_payment_match_with_revenue_and_payment_audits -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-anomaly-payment-one"` passed with 1 test.
- `python -B -m pytest tests/api/test_bank_reconciliation_api.py::test_finance_viewer_reads_bank_reconciliation_summary_with_audit -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-anomaly-bank-one"` passed with 1 test.
- `python -B -m pytest tests/api/test_revenue_facts_api.py::test_finance_viewer_reads_channel_month_facts_with_revenue_audit -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-anomaly-revenue-facts-one"` passed with 1 test.
- `git diff --check` passed with Windows CRLF conversion warnings only.

## Shorts Revenue Breakdown Foundation Validation
- Added optional official `shorts_revenue_usd`, `longform_revenue_usd`, and `subscription_revenue_usd` columns to SQL monthly channel revenue facts through `20260513_0005_revenue_format_breakdown.py`.
- Extended revenue fact import/read models so connector imports can store official format component values and finance reads can see them through existing guarded revenue-fact APIs.
- Added validation that revenue-format component values must be finite non-negative decimals and that the known component total cannot exceed gross revenue. Null remains "not provided" and is not inferred from gross revenue.
- `python -B -m pytest tests/db/test_revenue_format_breakdown_migration.py tests/db/test_revenue_fact_models.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-shorts-db-red"` first failed with the expected missing migration and missing ORM fields.
- `python -B -m pytest tests/api/test_revenue_facts_api.py::test_system_integration_user_imports_monthly_revenue_fact_with_audit tests/api/test_revenue_facts_api.py::test_import_rejects_revenue_breakdown_above_gross tests/api/test_revenue_facts_api.py::test_finance_viewer_reads_channel_month_facts_with_revenue_audit -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-shorts-api-red"` first failed with missing API fields and missing gross-bound validation.
- `python -B -m pytest tests/db/test_revenue_format_breakdown_migration.py tests/db/test_revenue_fact_models.py tests/db/test_revenue_fact_migration.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-shorts-db-final"` passed with 4 tests.
- `python -B -m pytest tests/api/test_revenue_facts_api.py::test_system_integration_user_imports_monthly_revenue_fact_with_audit tests/api/test_revenue_facts_api.py::test_import_rejects_revenue_breakdown_above_gross tests/api/test_revenue_facts_api.py::test_finance_viewer_reads_channel_month_facts_with_revenue_audit -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-shorts-api-final"` passed with 3 tests.
- `python -B -m pytest tests/api/test_revenue_facts_api.py::test_system_integration_user_imports_monthly_revenue_fact_with_audit tests/api/test_revenue_facts_api.py::test_import_rejects_revenue_breakdown_above_gross tests/api/test_revenue_facts_api.py::test_finance_viewer_reads_channel_month_facts_with_revenue_audit -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-shorts-api-audit-final"` passed with 3 tests after adding the new component fields to sensitive import audit details.
- `python -B -m pytest tests/api/test_revenue_facts_api.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-shorts-revenue-facts-api-file"` passed with 14 tests.
- `python -B -m pytest tests/finance/test_revenue_summary.py tests/finance/test_revenue_reconciliation.py tests/finance/test_net_revenue.py tests/finance/test_smart_alerts.py tests/finance/test_payment_matching.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-shorts-finance-final"` passed with 20 tests.
- `python -B -m pytest tests/finance/test_revenue_facts.py -q -p no:cacheprovider --basetemp "$env:TEMP\ums-pytest-shorts-revenue-facts-service"` passed with 2 tests.
- `python -m ruff check --select I,F backend/ums_smart_revenue/finance/revenue_facts.py backend/ums_smart_revenue/db/finance_models.py backend/ums_smart_revenue/api/revenue.py tests/api/test_revenue_facts_api.py tests/db/test_revenue_fact_models.py tests/db/test_revenue_format_breakdown_migration.py backend/ums_smart_revenue/db/alembic/versions/20260513_0005_revenue_format_breakdown.py` passed.
- `python -m ruff check backend/ums_smart_revenue/db/alembic/versions/20260513_0005_revenue_format_breakdown.py tests/db/test_revenue_format_breakdown_migration.py` passed.
- `python -B -m alembic -c alembic.ini upgrade head --sql > "$env:TEMP\ums-shorts-breakdown-alembic.sql"` rendered migrations through `20260513_0005`, including `shorts_revenue_usd` and `ck_monthly_channel_revenue_facts_format_total`.
- `git diff --check` passed with Windows CRLF conversion warnings only.

## Remaining Next Steps
- Replace the local filesystem export artifact store with the chosen production object-storage adapter while preserving the current `file_url` and checksum metadata contract.
- Expand SQL audit persistence to each new sensitive endpoint as those routes are added.
- Add broader integration tests around real API routes as modules are built.
- Add a concrete secret-manager adapter after UMS chooses the provider; the current foundation stores external encrypted secret references only.
- Replace the trusted gateway identity headers with the chosen corporate identity provider integration; database-backed authorization is now available behind `UMS_AUTHZ_SOURCE=database`.

## Unresolved Assumptions
- Final identity provider is not specified.
- Exact TV/News sector IDs are not specified, so tests use placeholder sector ids.
- Frontend is preview-only and not wired to live backend auth/data yet, so UI permission metadata is exposed as backend metadata functions rather than a frontend package.
