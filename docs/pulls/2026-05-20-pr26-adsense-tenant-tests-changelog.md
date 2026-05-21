# PR #26 — AdSense Payment Repository Tenant-Scope Tests — Changelog

**Date:** 2026-05-20
**PR:** https://github.com/XGenerationy/Youtube/pull/26
**Branch:** `pr/s2-4b-finance-adsense-tenant-tests`
**Base:** `pr/s2-4a-tenant-id-on-operational-tables`

## Added

- `tests/finance/test_adsense_payments_tenant_scope.py` — direct repository-layer tenant-isolation tests for `SqlAlchemyAdSensePaymentRepository`. 17 test functions, +475 lines.
- `docs/pulls/2026-05-20-pr26-adsense-tenant-tests-report.md` — this PR's report artifact.
- `docs/pulls/2026-05-20-pr26-adsense-tenant-tests-changelog.md` — this file.
- `docs/pulls/2026-05-20-pr26-adsense-tenant-tests-handoff.md` — handoff artifact.
- `docs/pulls/` — new directory establishing the OPUS-style PR artifact convention in the UMS repo.

## Changed

- Tightened two write-isolation tests with row-cardinality assertions before dict projection.
- Added `list_month_payments` tenant-resolution precedence parity with the existing `list_payments` coverage.
- Replaced the handoff rerun snippet's machine-specific path with repo-agnostic commands.
- No `backend/` source file was modified. No migration, ORM, route, service, or repository signature changed.

## Removed

- Nothing.

## Behavior changes

- None at runtime. The repository's tenant scoping was already wired in PR #21 (S2.4a) and exercised indirectly via API and month-close tests. This PR adds direct proof of that scoping behavior at the repository layer.

## Test surface change

- Total pytest count: 490 → 507 (+17).
- New tests:
  - `test_sync_payments_stamps_default_tenant_without_context`
  - `test_sync_payments_stamps_explicit_constructor_tenant`
  - `test_sync_payments_uses_request_tenant_context_by_default`
  - `test_sync_payments_explicit_tenant_overrides_request_context`
  - `test_sync_payments_allows_same_payment_name_in_two_tenants`
  - `test_sync_payments_upsert_is_scoped_to_one_tenant`
  - `test_list_payments_filters_to_explicit_tenant_id`
  - `test_list_payments_uses_default_tenant_without_context`
  - `test_list_payments_returns_empty_page_for_empty_tenant`
  - `test_list_payments_explicit_tenant_overrides_request_context`
  - `test_list_payments_uses_request_tenant_context_by_default`
  - `test_list_month_payments_filters_to_explicit_tenant_id`
  - `test_list_month_payments_returns_empty_when_target_tenant_has_no_rows`
  - `test_list_month_payments_uses_default_tenant_without_context`
  - `test_list_month_payments_uses_request_tenant_context_by_default`
  - `test_list_month_payments_explicit_tenant_overrides_request_context`
  - `test_adsense_repository_rejects_invalid_tenant_id_string`

## Documentation changes

- New `docs/pulls/` convention adopted in UMS, mirroring OPUS CLAUDE.md §Document.
- No edits to existing `Docs/*.md` architecture or API specs (no behavioral contract change).

## Schema / data

- No Prisma/Alembic migration. No DB column, index, constraint, enum, status, or JSON-shape change.

## Configuration / runtime

- No `.env`, `pyproject.toml`, `alembic.ini`, Docker, CI, or operational contract change.
