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
- `backend/ums_smart_revenue/api/channels.py`
- `backend/ums_smart_revenue/api/connectors.py`
- `backend/ums_smart_revenue/api/dependencies.py`
- `backend/ums_smart_revenue/api/finance_close.py`
- `backend/ums_smart_revenue/api/groups.py`
- `backend/ums_smart_revenue/api/revenue.py`
- `backend/ums_smart_revenue/api/security.py`
- `backend/ums_smart_revenue/auth/__init__.py`
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
- `backend/ums_smart_revenue/db/__init__.py`
- `backend/ums_smart_revenue/db/finance_models.py`
- `backend/ums_smart_revenue/db/session.py`
- `backend/ums_smart_revenue/db/security_models.py`
- `backend/ums_smart_revenue/db/org_models.py`
- `backend/ums_smart_revenue/db/security_schema.sql`
- `backend/ums_smart_revenue/db/security_seed.sql`
- `backend/ums_smart_revenue/db/alembic/env.py`
- `backend/ums_smart_revenue/db/alembic/script.py.mako`
- `backend/ums_smart_revenue/db/alembic/versions/20260510_0001_security_foundation.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260510_0002_org_registry.py`
- `backend/ums_smart_revenue/db/alembic/versions/20260510_0003_finance_close.py`
- `backend/ums_smart_revenue/config/__init__.py`
- `backend/ums_smart_revenue/config/settings.py`
- `backend/ums_smart_revenue/config/version_baseline.py`
- `backend/ums_smart_revenue/connectors/__init__.py`
- `backend/ums_smart_revenue/connectors/credentials.py`
- `backend/ums_smart_revenue/finance/__init__.py`
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
- `tests/conftest.py`
- `tests/test_version_baseline.py`
- `tests/api/test_app.py`
- `tests/api/test_channels_api.py`
- `tests/api/test_connectors_api.py`
- `tests/api/test_finance_close_api.py`
- `tests/api/test_groups_api.py`
- `tests/api/test_guarded_routes.py`
- `tests/api/test_sql_backed_channel_dependencies.py`
- `tests/auth/test_audit_service.py`
- `tests/auth/test_access_index_builder.py`
- `tests/auth/test_policy.py`
- `tests/auth/test_sql_audit_sink.py`
- `tests/db/test_alembic_scaffold.py`
- `tests/db/test_finance_close_migration.py`
- `tests/db/test_finance_close_models.py`
- `tests/db/test_org_registry_migration.py`
- `tests/db/test_org_registry_models.py`
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
- Export Operator cannot change allocation rules.
- Finance Admin can record allocation-rule metadata and create an `ALLOCATION_RULE_CHANGED` audit event without calculating revenue.

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

## Remaining Next Steps
- Expand SQL audit persistence to each new sensitive endpoint as those routes are added.
- Add broader integration tests around real API routes as modules are built.
- Add a concrete secret-manager adapter after UMS chooses the provider; the current foundation stores external encrypted secret references only.
- Replace the temporary header-based principal dependency with the chosen corporate identity provider.

## Unresolved Assumptions
- Final identity provider is not specified.
- Exact TV/News sector IDs are not specified, so tests use placeholder sector ids.
- No frontend exists yet, so UI permission metadata is exposed as backend metadata functions rather than a frontend package.
