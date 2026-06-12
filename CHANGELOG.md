# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Root governance: `README.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CODEOWNERS`, `CODE_OF_CONDUCT.md`, `.gitignore`.
- `connectors.runs.repository.find_active_runs_for_scope` now accepts a
  ``connector_keys`` alias-candidate tuple so the
  `POST /connectors/jobs` duplicate / orphan-supersede preflight matches a
  RUNNING row opened under a public hyphen key (e.g.
  `youtube-reporting`) when the request arrives with the source-system
  underscore key (`youtube_reporting`), and vice versa. Symmetric with the
  credential + authorise-alias expansion the rest of the preflight uses.

### Changed
- `SqlAlchemyGoogleRevenueSourceRowRepository.upsert_many(...)` now returns
  `SourceRowUpsertResult` instead of a bare list of rows. Callers that need the
  persisted rows should read `.entries`; connector-run accounting should use
  `.created`, `.updated`, and `.unchanged` for source-row classification.
- `ConnectorJobExecutor._audit_failed_before_start` now sets `TENANT_CTX`
  to a minimal `Tenant` (id-only; lifecycle check intentionally
  bypassed) so the after_begin hook writes the trusted tenant-context
  row required by the `20260608_0001` RLS `WITH CHECK` policy on
  `audit_logs`. The token is reset on every exit path via try/finally.
  The pre-start failure audit is now durable on Postgres for all
  tenant lifecycle states (ACTIVE / SUSPENDED / ARCHIVED), closing the
  silent audit-loss window for inactive-tenant failures.
- `POST /connectors/jobs` now writes the `job_submitted` audit row on
  the route's tenant session (with `platform_lane` elevation) instead
  of the dependency-injected platform audit sink, so the audit commit
  and the after_commit activation hook share the same transaction.
  The acceptance record and the worker enqueue are now atomic: a
  stale-run supersede or other tenant-session commit failure after the
  audit would have been written can no longer leave a durable
  `job_submitted` record with no worker activation.

### Removed
- *(planned)* Neo4j component dropped entirely — `backend/ums_smart_revenue/graph/`, `tests/graph/`, `neo4j==6.2.0` dependency, related auth permissions, retired specs archived to `Docs/_archived/`.

### Security
- *(planned)* `UMS_TRUSTED_GATEWAY_TOKEN` removed from `tests/conftest.py`; loaded from env/fixture only.

### Stabilization roadmap (S1 ship-blockers)

1. **No `.gitignore`** → ✅ added.
2. **No CI** → 🔴 pending (`.github/workflows/ci.yml`).
3. **Hardcoded test token** → 🔴 pending.
4. **No Docker / K8s manifests** → 🔴 pending.
5. **No dependency lock file** → 🔴 pending (`uv.lock`).
6. **No CORS / rate limit / global exception handler** → 🔴 pending.
7. **No observability** → 🔴 pending.
8. **No PG backup / DR drill** → 🔴 pending.
9. **No `mypy` strict on money modules** → 🔴 pending.
10. **No multi-tenant model** → 🔴 pending (Phase S2).

---

## [0.1.0] — 2026-05-10 — Phase 1 foundation

### Added
- Authorization model: 14 roles, 34 permissions, 6 scope kinds (`GLOBAL`, `CHANNEL`, `COMPANY`, `SECTOR`, `FINANCE_MONTH`, `CONNECTOR`).
- `Principal`, role assignments, direct permission grants, family-restricted grant logic.
- SQL-backed principal loader (`UMS_AUTHZ_SOURCE=database`) with SERIALIZABLE isolation, retryable failure handling, 256-role / 512-grant caps; header-trusted fallback for bootstrap (`UMS_AUTHZ_SOURCE=headers`).
- 24 audit event types; sensitive-payload masking; SQL audit sink.
- Channel registry (SQL) + channel groups + access index.
- Finance domain — read foundation:
  - Revenue facts ingestion (`POST /revenue/facts`).
  - Monthly net-revenue summary (`GET /revenue/months/{month}/net-revenue`).
  - Payment matching, bank reconciliation, smart alerts, reconciliation issue queue.
  - Manual overrides with approval workflow + locked-month immutability.
  - Number explanation snapshots (`POST /revenue/channels/{id}/months/{m}/explain`).
  - Finance month-close state machine + readiness gate.
- Connector credential storage with secret-manager URI references.
- Exports — ephemeral generation:
  - Finance workbook XLSX preview + download (`openpyxl`, 9 sheets).
  - Executive PDF (`reportlab`).
  - Branded slide pack PPTX (`python-pptx`, 10 branded slides).
- 11 API routers, ~50 endpoints, all permission-guarded.
- 13 Alembic migrations covering security, org, finance, raw report files, number explanations, export jobs, AdSense payments, bank reconciliation.
- 67 test files: API (24), Auth (6), DB (18 — 9 migration + 9 model), Finance (9), Graph (1), Org (2), Reports (3).
- CodeRabbit `.coderabbit.yaml` (assertive profile).
- `.sqlfluff` Postgres dialect config.
- `alembic.ini` baseline.

### Stabilization passes
- 2026-05-10: revenue facts foundation through finance close readiness gate stabilized; 154 tests passing after audit-log APIs added.

[Unreleased]: https://github.com/XGenerationy/Youtube/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/XGenerationy/Youtube/releases/tag/v0.1.0
