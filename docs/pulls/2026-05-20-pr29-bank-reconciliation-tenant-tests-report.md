# PR #29 — Bank Reconciliation Tenant-Scope Tests — Report

**Date:** 2026-05-20
**PR:** https://github.com/XGenerationy/Youtube/pull/29
**Branch:** `pr/s2-4b-finance-bank-recon-tenant-tests`
**Base:** `pr/s2-4a-tenant-id-on-operational-tables` (rolling integration head `5c53593`)
**Head commit:** `f21a70f` (first commit; this report and the changelog/handoff land in a second commit)
**Status:** Open, all gates green locally, awaiting review.

## What was requested

After PR #28 (`.gitignore` backport) was opened, the user picked direct tenant-isolation tests for `SqlAlchemyBankReconciliationRepository` as the next PR (item #2 of the three-PR sequence). The repository was wired for tenancy in S2.4a (PR #21) — it stamps and filters by `tenant_id` — but it was only indirectly exercised through API and month-close integration tests. PR #26 had established a clear pattern for direct repository-layer tenant-scope tests (`tests/finance/test_adsense_payments_tenant_scope.py`); this PR mirrors that pattern.

## What was actually done

A single new test file, `tests/finance/test_bank_reconciliation_tenant_scope.py`, with **13 focused tests** for `SqlAlchemyBankReconciliationRepository`:

| # | Test | What it proves |
|---|---|---|
| 1 | `record_entry_stamps_default_tenant_without_context` | Bootstrap callers (no `TENANT_CTX`, no constructor arg) get the UMS default `tenant_id` stamped on the inserted row. |
| 2 | `record_entry_stamps_explicit_constructor_tenant` | Constructor `tenant_id` is stamped on the row. |
| 3 | `record_entry_uses_request_tenant_context_by_default` | Ambient `TENANT_CTX` scopes writes when no explicit constructor arg is supplied. |
| 4 | `record_entry_explicit_tenant_overrides_request_context` | Constructor `tenant_id` beats `TENANT_CTX` on writes. |
| 5 | `record_entry_allows_same_bank_reference_in_two_tenants` | The S2.4a composite uniqueness `(tenant_id, month, bank_reference)` lets two tenants share the same `(month, bank_reference)` pair. |
| 6 | `record_entry_upsert_is_scoped_to_one_tenant` | The `ON CONFLICT (tenant_id, month, bank_reference) DO UPDATE` upsert path mutates only the bound tenant's row; tenant B's row stays untouched even after tenant A re-upserts the same `bank_reference`. |
| 7 | `record_entry_locked_month_is_scoped_to_the_bound_tenant` | A locked `finance_month_close` row for tenant A does **not** block `record_entry` writes in tenant B. (Closes a critical lock-leak vector.) |
| 8 | `list_month_entries_filters_to_explicit_tenant_id` | Read filters correctly on the explicit tenant. |
| 9 | `list_month_entries_returns_empty_for_empty_tenant` | A foreign tenant id returns `[]` instead of leaking. |
| 10 | `list_month_entries_uses_default_tenant_without_context` | Bootstrap callers see only UMS-default rows. |
| 11 | `list_month_entries_uses_request_tenant_context_by_default` | `TENANT_CTX` scopes reads. |
| 12 | `list_month_entries_explicit_tenant_overrides_request_context` | Constructor wins over `TENANT_CTX` on reads. |
| 13 | `bank_reconciliation_repository_rejects_invalid_tenant_id_string` | Constructor validation fails closed before any DB access. |

## Phased execution

| Phase | Action | Pytest after | Notes |
|---|---|---|---|
| Baseline | Inspect base + existing `test_bank_reconciliation.py` (101 lines; only tests `build_month_bank_reconciliation_summary`; zero tenant tests) | 507 passed | 652 pre-existing ruff errors |
| 1 | Read `bank_reconciliation.py` (445 lines) | 507 passed | Confirmed tenant wiring: lines 112–213 |
| 2 | Read `test_adsense_payments_tenant_scope.py` (476 lines) | 507 passed | Confirmed pattern + helper shape |
| 3 | Read `BankReconciliationEntryORM` + `FinanceMonthCloseORM` | 507 passed | Confirmed server-defaults; confirmed composite unique key |
| 4 | Write `test_bank_reconciliation_tenant_scope.py` (~350 lines, 13 tests) | 520 passed | All 13 pass on first run |
| 5 | Targeted ruff revealed 2 long-docstring E501s | 520 passed | Shortened docstrings to fit 88 chars |
| 6 | Targeted `ruff format` applied | 520 passed | 1 file reformatted (mine) |
| 7 | Final full gate | 520 passed | Whole-tree baseline preserved |
| 8 | Commit `f21a70f`, push, open PR #29 | — | — |

## Quality checks performed

- `python -m ruff check backend tests` — 652 errors (pre-existing baseline; this PR adds 0; resolved by PR #27).
- `python -m ruff check backend tests --statistics` — identical per-category breakdown as base (E501 ×582, I001 ×52, UP037 ×8, N818 ×3, UP042 ×3, UP035 ×2, UP045 ×1, UP047 ×1).
- `python -m ruff check tests/finance/test_bank_reconciliation_tenant_scope.py` — **All checks passed**.
- `python -m ruff format --check backend tests` — 102 files would be reformatted (pre-existing; the new file is clean).
- `python -m ruff format --check tests/finance/test_bank_reconciliation_tenant_scope.py` — Already formatted.
- `python -m pytest -q` — **520 passed, 7 warnings in 29s** (507 base + 13 new).
- `python -m pytest -q tests/finance/test_bank_reconciliation_tenant_scope.py` — 13 passed in 0.30s.
- `python -m pytest -q tests/finance/` — 71 passed (full finance subset, no regression).
- `git diff --check` — clean (exit 0).
- Conflict-marker scan (`git grep -nE '^(<{7}|={7}|>{7})( |$)' -- ':!docs/pulls/' ':!*.md'`) — clean.
- Working-tree conflict-marker scan over new file — clean.
- Import smoke: `from ums_smart_revenue.finance.bank_reconciliation import SqlAlchemyBankReconciliationRepository, BankReconciliationLockedMonthError, BankReconciliationValidationError` — ok.
- Alembic linear history — single head `20260518_0001` as of the 2026-05-20 PR #29 branch. Integrated branches after PR #36 use merge head `20260521_0001`.

## Architecture & quality posture

- **No source semantics change.** Pytest count is +13 (507 → 520) and every base-suite test still passes unchanged. `bank_reconciliation.py` source is untouched.
- **No tenant scoping change.** The tests exercise the tenant filters and stamps that S2.4a already wired; they prove the wiring is correct, not modify it.
- **No graph projection impact detected.** This PR is test-only; the Neo4j read-only contract is untouched.
- **No authorization or audit behavior change.**
- **No finance number behavior change.** Same `build_month_bank_reconciliation_summary` math.
- **Security**: zero new attack surface. The new tests act as a regression guard against IDOR and cross-tenant lock-leak vectors that could be reintroduced if the tenant filter or stamp is accidentally removed.
- **Observability**: no logging change.
- **Testability**: +13 dedicated tests for a previously-thin surface.

## Blast-radius statement

*No graph projection impact detected.* No SQLAlchemy ORM change, no Alembic migration, no route, no service, no repository, no DI provider, no schema change. The PR adds one new test file and nothing else. PostgreSQL/Neo4j contract is unchanged.

## Pre-existing baseline (NOT introduced by this PR)

The base branch `pr/s2-4a-tenant-id-on-operational-tables` at `5c53593` carries 652 ruff errors and 102 `ruff format` would-reformat files. **This PR adds 0** to either count (verified by per-file ruff check and per-file format check). Both pre-existing categories are addressed by the still-open PR #27 (full repo ruff cleanup). The two PRs are deliberately independent and can land in either order.

## Validation that could NOT be run

- The `ci/` self-hosted pre-push gate referenced in OPUS CLAUDE.md does not exist on the S2.4b stack (it lives on `origin/main`, which this stack has not yet rebased onto). Per UMS CLAUDE.md, the required local gates are `ruff check + pytest -q + git diff --check`, augmented with the "no by luck work" additions (statistics, conflict markers, import smoke, alembic heads) — all of which **were** run.

## Remaining risks

- **Code risk: zero.** No code is touched.
- **Test-flake risk: very low.** All 13 tests use in-memory sqlite isolated per test (`build_session()` creates a fresh engine), no shared state, no time-dependent assertions beyond a fixed `CREATED_AT` constant.
- **Reviewer-flow risk: low.** One file, ~350 lines, 13 tests. Each test is short, focused, and named explicitly.

## Follow-up recommendations

- **PR #30 (queued)** — create `tests/org/test_sql_channel_groups.py` from scratch. `sql_channel_groups.py` has tenant wiring (from PR #25) but no direct registry-layer tests. Pattern after `tests/org/test_sql_channel_registry.py`. Independent of this PR.
- **Future (lower priority)** — direct tests for `finance/explanations.py` (206 lines, no direct test file), `finance/revenue_facts.py` thin coverage, `reports/exports.py`, `reports/raw_files.py`, `connectors/credentials.py`.

## Rollback notes

- Single-file PR. Revert is `git revert <merge-commit>` — touches one new test file.
- No data, schema, runtime state, or downstream consumer is touched; rollback is safe to apply to a running deployment.
- If the test file is reverted, the bank reconciliation repository still functions identically — the production code is unchanged.
