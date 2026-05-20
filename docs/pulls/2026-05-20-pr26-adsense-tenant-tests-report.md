# PR #26 — AdSense Payment Repository Tenant-Scope Tests — Report

**Date:** 2026-05-20
**PR:** https://github.com/XGenerationy/Youtube/pull/26
**Branch:** `pr/s2-4b-finance-adsense-tenant-tests`
**Base:** `pr/s2-4a-tenant-id-on-operational-tables`
**Branch head:** See PR metadata for the latest head SHA; this file may be updated during review follow-up.
**Status:** Open, review feedback addressed locally, tests green locally, awaiting final bot re-review after push.

## What was requested

Continue the S2.4b tenant-scoping work on the UMS Smart Revenue repo. The prior session's recap claimed two slices were still pending (`S2.4b-org-2` for `sql_channel_groups.py` and `S2.4b-finance` for `manual_overrides.py`). The user authorized cloning the live repo to a fresh Linux working copy, setting up a Python 3.14 venv, basing on `main`, ship PR A end-to-end (code + tests + ruff + pytest + push + open PR) and then stop.

## What was actually found

The recap was off. PR #25 (`063d8f6 feat(org): tenant-scoped channel registry + access index (S2.4b-org-1)`) had a much wider scope than just the channel registry and access index — it also fully wired `manual_overrides.py` (+98 lines) and `sql_channel_groups.py` (+297 lines), plus `adsense_payments.py`, `bank_reconciliation.py`, `month_close.py`, `month_close_readiness.py`, `revenue_facts.py`. Both supposedly-pending slices were already merged. Also, `main` is at S2.1 (PR #18), not on top of the S2.4b stack; the rolling integration branch is `pr/s2-4a-tenant-id-on-operational-tables`. The user's original message had stated that explicitly; the answer of "main" in the AskUserQuestion was a slip, corrected mid-session.

## Pivot

A full audit of every backend module that touches a tenant-scoped ORM, against its direct unit-test coverage, identified concrete remaining gaps:

- **Real gaps** (no direct cross-tenant tests at the repository layer): `finance/adsense_payments.py`, `finance/bank_reconciliation.py`, `finance/explanations.py`, `org/sql_channel_groups.py`, `reports/exports.py`, `reports/raw_files.py`, `connectors/credentials.py`.
- **Thin coverage**: `finance/manual_overrides.py`, `finance/revenue_facts.py`, `auth/user_roles.py`, `auth/user_permissions.py`.
- **Fully covered**: `auth/audit_log.py + auth/sql_audit_sink.py`, `auth/users.py`, `auth/principals.py`, `org/sql_channel_registry.py + org/access_index.py`, `finance/month_close*.py`, `tenancy/repository.py`.

The user picked `finance/adsense_payments.py` as PR A (single-file scope), with `finance/bank_reconciliation.py` queued as PR B.

## What was changed

Single file added: `tests/finance/test_adsense_payments_tenant_scope.py` (+475 lines, 0 deletions). No source changes. No migration. No model changes.

Test layout mirrors `tests/auth/test_audit_tenant_scope.py` and `tests/auth/test_user_account_tenant_scope.py`:

- `build_session()` — in-memory sqlite + `FinanceBase.metadata.create_all`.
- `_payment_input(...)` — `AdSensePaymentInput` factory with `month=2026-03`, `payment_status=PAID`, `payment_currency=USD`.
- `_tenant(...)` — active `Tenant` for `TENANT_CTX.set(...)` cases.
- `_seed_payment(...)` — direct ORM insert bypassing `_require_month_open` for read-test fixtures.

Seventeen test functions:

| # | Test | What it proves |
|---|------|----------------|
| 1 | `test_sync_payments_stamps_default_tenant_without_context` | Bootstrap callers (no ctx, no arg) stamp the UMS default. |
| 2 | `test_sync_payments_stamps_explicit_constructor_tenant` | Explicit `tenant_id=` kwarg is stamped on inserted row. |
| 3 | `test_sync_payments_uses_request_tenant_context_by_default` | `TENANT_CTX` scopes writes when no explicit kwarg is supplied. |
| 4 | `test_sync_payments_explicit_tenant_overrides_request_context` | Explicit kwarg beats ambient context. |
| 5 | `test_sync_payments_allows_same_payment_name_in_two_tenants` | Per-tenant unique constraint `(tenant_id, month, payment_name)` lets two tenants share names and creates exactly one row per tenant. |
| 6 | `test_sync_payments_upsert_is_scoped_to_one_tenant` | Re-syncing in tenant A leaves tenant B's row untouched and preserves exactly one row per tenant. |
| 7 | `test_list_payments_filters_to_explicit_tenant_id` | `list_payments` filters to the chosen tenant. |
| 8 | `test_list_payments_uses_default_tenant_without_context` | Default tenant fallback on reads. |
| 9 | `test_list_payments_returns_empty_page_for_empty_tenant` | Empty page for an unknown tenant (no leak). |
| 10 | `test_list_payments_explicit_tenant_overrides_request_context` | Explicit wins on reads too. |
| 11 | `test_list_payments_uses_request_tenant_context_by_default` | Context wins when no explicit kwarg on reads. |
| 12 | `test_list_month_payments_filters_to_explicit_tenant_id` | Month-scoped read does not surface other-tenant rows. |
| 13 | `test_list_month_payments_returns_empty_when_target_tenant_has_no_rows` | Empty result when only the other tenant has rows. |
| 14 | `test_list_month_payments_uses_default_tenant_without_context` | Default tenant fallback on month-scoped reads. |
| 15 | `test_list_month_payments_uses_request_tenant_context_by_default` | Context tenant wins when no explicit kwarg is supplied on month-scoped reads. |
| 16 | `test_list_month_payments_explicit_tenant_overrides_request_context` | Explicit tenant wins over ambient context on month-scoped reads. |
| 17 | `test_adsense_repository_rejects_invalid_tenant_id_string` | Constructor input validation fails closed. |

## Bot audit follow-up

- CodeRabbit requested parity tests for `list_month_payments` tenant precedence and a portable handoff rerun snippet.
- Codex requested row-cardinality assertions before dict projections in the two write isolation tests.
- All four findings were verified as valid and addressed in this follow-up.

## Quality checks performed

- `.venv/bin/python -m ruff check tests/finance/test_adsense_payments_tenant_scope.py` — `All checks passed!`
- `.venv/bin/python -m pytest -q tests/finance/test_adsense_payments_tenant_scope.py` — `17 passed in 0.34s`.
- `.venv/bin/python -m pytest -q` (full suite) — `507 passed, 7 warnings in 29.95s` (baseline was 490 before this PR; +17 = 507).
- `git diff --check` — clean.
- Re-read final diff, confirmed only the intended test and PR artifact files are changed.
- No `.venv` or `__pycache__` directories leaked into the commit (git considers them untracked and they were never staged).

## Architecture & quality posture

- **UMS architecture preserved.** Tests live in `tests/finance/`, matching the established layer convention. No code in `backend/` was changed.
- **No FastAPI route, service, repository, ORM, migration, or DI change.** Constructor signatures unchanged.
- **No graph projection impact detected.** No Neo4j cypher, no read-only projection code touched.
- **No authorization, audit, or finance-number behavior change.** This PR adds proof of existing scoping; it cannot loosen anything.
- **Security:** the tests directly exercise IDOR-style attacks (cross-tenant lookups, third-tenant reads with no rows). Closing this coverage gap is the security value of the PR.
- **Observability:** no new logs/metrics; tests-only.
- **Testability:** 17 direct unit tests using in-memory sqlite, sub-second runtime; no Docker, no fixtures outside the file.

## Blast-radius statement

*No graph projection impact detected.* No SQLAlchemy ORM, route, service, or repository was modified — the test file imports `AdSensePaymentORM` only to read its data. The PostgreSQL/Neo4j contract is unchanged.

## Validation that could NOT be run

- The `ci/` self-hosted pre-push gate referenced in OPUS CLAUDE.md does not exist in the UMS repo (there is no `ci/` directory, no Makefile target for `make ci-dashboard`, no `.github/workflows/`). The UMS validation gate per its own CLAUDE.md is `ruff check + pytest -q + git diff --check`, which was run locally on the post-change tree.
- `make codeql-triage` — not applicable; OPUS-only.
- `pytest -q tests/integration/` against Docker testcontainers — not applicable; UMS tests run on in-memory sqlite per `pyproject.toml`'s `pythonpath = ["backend"]`. There are no UMS integration tests gated on Docker.

## Remaining risks

- **None at the code level.** The diff is +475/0 in a brand-new test file; nothing to regress at runtime.
- **Process risk:** follow-up commits on an open PR were limited to review feedback, tests, and PR artifacts.

## Follow-up recommendations

- **PR B** — `finance/bank_reconciliation.py` direct tenant-isolation tests. Same template as this PR. Its existing `tests/finance/test_bank_reconciliation.py` has zero tenant keyword hits despite the source being fully tenant-wired.
- **PR C–E (lower priority)** — `finance/explanations.py`, `org/sql_channel_groups.py` direct registry-layer tests, and thin-coverage expansion on `finance/manual_overrides.py` + `finance/revenue_facts.py`.
- **Lower priority** — `reports/exports.py`, `reports/raw_files.py`, `connectors/credentials.py`. These have only API-layer test coverage today; if/when those modules grow, dedicated repository-layer tenant tests would close their gaps.

## Rollback notes

- Pure additive PR. Revert is `git revert <merge-commit>` — no data, schema, or runtime state is touched. If a follow-up commit is rejected, revert that commit without touching production code.
