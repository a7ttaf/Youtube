# PR #29 — Bank Reconciliation Tenant-Scope Tests — Changelog

**Date:** 2026-05-20
**PR:** https://github.com/XGenerationy/Youtube/pull/29
**Branch:** `pr/s2-4b-finance-bank-recon-tenant-tests`
**Base:** `pr/s2-4a-tenant-id-on-operational-tables`

## Added

- `tests/finance/test_bank_reconciliation_tenant_scope.py` (~350 lines, 13 tests).
- `docs/pulls/2026-05-20-pr29-bank-reconciliation-tenant-tests-report.md` (this PR's report artifact).
- `docs/pulls/2026-05-20-pr29-bank-reconciliation-tenant-tests-changelog.md` (this file).
- `docs/pulls/2026-05-20-pr29-bank-reconciliation-tenant-tests-handoff.md` (handoff artifact).

## Changed

### Source semantics — none

No `backend/ums_smart_revenue/**` file is touched. No business logic, finance calculation, tenant scoping, authorization rule, audit behavior, migration semantics, API contract, or Neo4j read-only projection changed. `bank_reconciliation.py` is unchanged.

### Lint / format — none

No Python source file is modified outside of the new test file. The new test file passes `ruff check` and `ruff format --check`. The 652 pre-existing ruff errors and 102 pre-existing `ruff format` unclean files on the base branch are documented but **not modified** by this PR — that work is owned by the still-open PR #27.

### Symbol renames — none

### Enum migration — none

### Generic syntax — none

### Cypher whitespace — none

### Alembic — none

### SQL — none

### Tests — added 13

Direct tenant-isolation tests for `SqlAlchemyBankReconciliationRepository`:

| Category | Tests added | Coverage |
|---|---|---|
| `record_entry` writes | 4 | Default tenant (no context), explicit constructor, `TENANT_CTX`-by-default, explicit beats `TENANT_CTX` |
| Composite uniqueness | 2 | Two tenants share `(month, bank_reference)`; upsert path scoped per-tenant |
| Month-close lock isolation | 1 | A locked month in tenant A does not block writes in tenant B |
| `list_month_entries` reads | 5 | Explicit, empty-tenant returns `[]`, default-without-context, `TENANT_CTX`-by-default, explicit beats `TENANT_CTX` |
| Constructor validation | 1 | Malformed `tenant_id` string raises `BankReconciliationValidationError` |
| **Total** | **13** | |

The tests use in-memory SQLite (`build_session()`), the same pattern as `tests/finance/test_adsense_payments_tenant_scope.py` (added in PR #26).

## Removed

- Nothing removed.

## Behavior changes

- **Source semantics: none.** Pytest count: 507 → 520 (+13).
- **Regression guard added:** if a future PR accidentally removes a `tenant_id` filter or stamp in `SqlAlchemyBankReconciliationRepository`, the relevant test in this file will fail. The IDOR/cross-tenant lock-leak vectors are now directly guarded at the repository layer.

## Test surface change

- Pytest total: 507 → 520 (+13).
- 1 new test file: `tests/finance/test_bank_reconciliation_tenant_scope.py`.
- 13 new test functions, all `test_*` discoverable by pytest's default collection.
- No existing test file, fixture, or conftest is modified.

## Documentation changes

- 3 new artifacts under `docs/pulls/` (report + changelog + handoff). No edits to existing `Docs/*.md` architecture or API specs.

## Schema / data

- **No** Prisma/Alembic migration. **No** DB column, index, constraint, enum, status, or JSON-shape change.

## Configuration / runtime

- No `.env`, `pyproject.toml`, `alembic.ini`, Docker, CI, or operational contract change.

## Pattern compatibility

- Mirrors `tests/finance/test_adsense_payments_tenant_scope.py` (PR #26) for surface, helpers, and ordering. Future bank-reconciliation maintainers can rely on the same mental model.
- Uses the same canonical tenant ids:
  - `DEFAULT_TENANT_ID = UUID(UMS_TENANT_ID)`
  - `SECOND_TENANT_ID = UUID("00000000-0000-0000-0000-000000041999")`
- Uses the same `_tenant(id, slug=...)` helper shape and the same `TENANT_CTX.set/.reset` token pattern.

## Compatibility with origin/main

- This PR is purely additive on the S2.4b stack. When the stack rebases onto / merges with `origin/main`, the new test file ships unchanged. Bank reconciliation source code on main is API-compatible with the stack version (S2.4a's tenant wiring is already present in both).
