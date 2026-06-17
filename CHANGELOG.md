# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Channel `content_owner_id` write path: `content_owner_id` is now an optional
  field on `POST /channels`, and a new `PATCH /channels/{youtube_channel_id}/content-owner`
  (gated on `MANAGE_CHANNELS`, audited as `CHANNEL_UPDATED` with old/new values,
  no-op suppressed, nullable to clear) lets operators set the CMS content owner
  that `list_target_channels` matches against the connector account id. Closes
  the latent silent-zero gap where every channel kept `content_owner_id=None`
  and no channel was ever selected for ingestion. No migration — the
  `youtube_channels.content_owner_id` column already existed.
- Root governance: `README.md`, `SECURITY.md`, `CONTRIBUTING.md`, `CODEOWNERS`, `CODE_OF_CONDUCT.md`, `.gitignore`.

### Changed
- `SqlAlchemyGoogleRevenueSourceRowRepository.upsert_many(...)` now returns
  `SourceRowUpsertResult` instead of a bare list of rows. Callers that need the
  persisted rows should read `.entries`; connector-run accounting should use
  `.created`, `.updated`, and `.unchanged` for source-row classification.

### Fixed
- `ConnectorJobExecutor._audit_failed_before_start` now sets `TENANT_CTX` to a
  minimal `Tenant` (id-only) before opening the audit session, so the
  `after_begin` hook in `db/session.py` writes the trusted tenant-context row
  and the `audit_logs` RLS `WITH CHECK (tenant_id = app_current_tenant_id())`
  policy admits the Bucket-A failure-audit INSERT on Postgres. Without this,
  `app_platform` (which is `NOBYPASSRLS`) could not satisfy the policy and
  every pre-start connector failure was silently lost. The contextvar is
  reset in a `finally` block so the prior tenant is always restored.
- Centralized the placeholder-tenant fabrication in
  `tenancy.models.make_placeholder_tenant` (with `PLACEHOLDER_TENANT_EPOCH` and
  `DEFAULT_PRIMARY_CURRENCY` module constants). Both
  `ConnectorJobExecutor._audit_failed_before_start` and the test-only branch
  of `connector_tenant_context` now route through the same factory, so the
  placeholder shape can no longer drift between the audit and the unit-test
  paths.

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
