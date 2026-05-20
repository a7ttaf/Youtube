# PR #30 — Channel Group Registry Tenant-Scope Tests — Report

**Date:** 2026-05-20
**PR:** https://github.com/XGenerationy/Youtube/pull/30
**Branch:** `pr/s2-4b-org-2-channel-groups-tests`
**Base:** `pr/s2-4a-tenant-id-on-operational-tables` (rolling integration head `5c53593`)
**Head commit:** `64d70a6` (first commit; this report and the changelog/handoff land in a second commit)
**Status:** Open, all gates green locally, awaiting review.

## What was requested

After PR #29 (bank reconciliation tenant tests) was opened, the user picked direct registry-layer tenant-scope tests for `SqlAlchemyChannelGroupRegistry` as the next PR (item #3 of the three-PR sequence). The repository was tenant-wired in PR #25 (S2.4b-org-1) — every read filters by `tenant_id` and every write stamps it — but unlike the channel registry, it had no dedicated registry-layer tests. The local planning note ("PR B — S2.4b-org-2") proposed creating `tests/org/test_sql_channel_groups.py` from scratch, patterned after `tests/org/test_sql_channel_registry.py`. That is what this PR does.

## What was actually done

A single new test file, `tests/org/test_sql_channel_groups.py`, with **18 focused tests** for `SqlAlchemyChannelGroupRegistry`:

| # | Test | What it proves |
|---|---|---|
| 1 | `create_group_stamps_default_tenant_on_group_and_members` | Bootstrap callers stamp `DEFAULT_TENANT_ID` on the group row AND on every member row written by `_replace_member_rows`. |
| 2 | `create_group_stamps_explicit_constructor_tenant` | Explicit constructor `tenant_id` is stamped on group + members. |
| 3 | `create_group_uses_request_tenant_context_by_default` | Ambient `TENANT_CTX` scopes writes when no constructor arg. |
| 4 | `create_group_explicit_tenant_overrides_request_context` | Constructor beats `TENANT_CTX`. |
| 5 | `create_group_rejects_cross_tenant_channel_external_id` | `_channel_rows_by_external_ids` filters channels by `tenant_id`; passing another tenant's external id raises `KeyError`. |
| 6 | `create_group_allows_empty_channel_id_list_without_member_rows` | Empty `channel_ids` creates a tenant-stamped group and no member rows. |
| 7 | `list_groups_filters_to_bound_tenant_only` | `list_groups` never surfaces another tenant's groups. |
| 8 | `list_groups_excludes_inactive_groups` | `active.is_(True)` filter works. |
| 9 | `list_groups_uses_request_tenant_context_by_default` | `TENANT_CTX` scopes reads. |
| 10 | `get_group_returns_none_for_cross_tenant_group_id` | `_get_group_row` explicit-select replaces the old `session.get(...)` IDOR vector. |
| 11 | `get_group_returns_none_for_malformed_uuid_string` | `_get_group_row`'s `try/except` guard handles malformed input. |
| 12 | `add_members_for_cross_tenant_group_raises_keyerror` | `_require_group_row` rejects add to another tenant's group; no member row is created. |
| 13 | `add_members_stamps_bound_tenant_on_new_member_rows` | Every newly inserted member row carries the bound `tenant_id`. |
| 14 | `add_members_with_empty_channel_id_list_keeps_existing_members` | Empty `add_members` input leaves existing members unchanged. |
| 15 | `add_members_recovers_from_duplicate_member_integrity_error` | Duplicate-member `IntegrityError` recovery retries safely and preserves tenant stamping. |
| 16 | `remove_member_for_cross_tenant_group_raises_keyerror` | `_require_group_row` rejects remove on another tenant's group; the surviving row is unaffected. |
| 17 | `update_group_for_cross_tenant_group_raises_keyerror` | `_require_group_row` rejects mutation on another tenant's group; the row's `name` and `active` are preserved. |
| 18 | `channel_ids_by_group_dual_filter_excludes_cross_tenant_member_rows` | `_channel_ids_by_group` excludes both another tenant's group id and a mismatched-tenant member row on the same group id. |

## Phased execution

| Phase | Action | Pytest after | Notes |
|---|---|---|---|
| Baseline | Inspect base + confirm no `tests/org/test_sql_channel_groups.py` exists | 507 passed | 652 pre-existing ruff errors |
| 1 | Read `org/sql_channel_groups.py` (293 lines) | 507 passed | Confirmed full tenant wiring from PR #25 |
| 2 | Read `tests/org/test_sql_channel_registry.py` (344 lines) | 507 passed | Confirmed pattern: `build_session()` with `PRAGMA foreign_keys=ON`, `seed_org()` with parallel default+other tenant rows |
| 3 | Read `ChannelGroupORM` + `ChannelGroupMemberORM` definitions | 507 passed | Confirmed composite FK on `(tenant_id, group_id)` and unique on `(tenant_id, id)` |
| 4 | Write `test_sql_channel_groups.py` (~680 lines after review follow-up, 18 tests) | 525 passed | Includes CodeRabbit edge-case audit coverage |
| 5 | Ruff check on new file | 525 passed | All checks passed |
| 6 | `ruff format --check` on new file | 525 passed | Already formatted |
| 7 | Final full gate | 525 passed | Whole-tree baseline preserved |
| 8 | Commit `64d70a6`, push, open PR #30 | — | — |

## Quality checks performed

- `python -m ruff check backend tests` — 652 errors (pre-existing baseline; this PR adds 0; resolved by PR #27).
- `python -m ruff check backend tests --statistics` — identical per-category breakdown as base (E501 ×582, I001 ×52, UP037 ×8, N818 ×3, UP042 ×3, UP035 ×2, UP045 ×1, UP047 ×1).
- `python -m ruff check tests/org/test_sql_channel_groups.py` — **All checks passed**.
- `python -m ruff format --check backend tests` — 102 files would be reformatted (pre-existing; new file is clean).
- `python -m ruff format --check tests/org/test_sql_channel_groups.py` — Already formatted.
- `python -m pytest -q` — **525 passed, 7 warnings in 30s** (507 base + 18 new).
- `python -m pytest -q tests/org/test_sql_channel_groups.py` — 18 passed in 0.34s.
- `python -m pytest -q tests/org/` — 33 passed (full org subset, no regression on `test_sql_channel_registry.py`).
- `git diff --check` — clean (exit 0).
- Conflict-marker scan (`git grep -nE '^(<{7}|={7}|>{7})( |$)' -- ':!docs/pulls/' ':!*.md'`) — clean.
- Working-tree conflict-marker scan over new file — clean.
- Import smoke: `from ums_smart_revenue.org.sql_channel_groups import SqlAlchemyChannelGroupRegistry` — ok.
- Alembic linear history — single head `20260518_0001`.

## Architecture & quality posture

- **No source semantics change.** Pytest count is +18 (507 → 525). `org/sql_channel_groups.py` source is untouched.
- **No tenant scoping change.** The tests exercise the tenant filters and stamps that PR #25 already wired; they prove the wiring is correct, not modify it.
- **No graph projection impact detected.** Channel groups are PostgreSQL-only data; Neo4j is downstream and read-only.
- **No authorization or audit behavior change.**
- **No finance number behavior change.**
- **Security**: zero new attack surface. The new tests guard against IDOR (cross-tenant `group_id`) and cross-tenant channel-id reuse vectors that could be reintroduced if `_require_group_row`, `_get_group_row`, or `_channel_rows_by_external_ids` were accidentally weakened.
- **Observability**: no logging change.
- **Testability**: +18 dedicated tests for a previously-thin surface.

## Blast-radius statement

*No graph projection impact detected.* No SQLAlchemy ORM change, no Alembic migration, no route, no service, no repository, no DI provider, no schema change. The PR adds one new test file and nothing else. PostgreSQL/Neo4j contract is unchanged.

## Pre-existing baseline (NOT introduced by this PR)

The base branch `pr/s2-4a-tenant-id-on-operational-tables` at `5c53593` carries 652 ruff errors and 102 `ruff format` would-reformat files. **This PR adds 0** to either count. Both pre-existing categories are addressed by the still-open PR #27. Independent and can land in any order.

## Validation that could NOT be run

- The `ci/` self-hosted pre-push gate (referenced in OPUS CLAUDE.md) does not exist on the S2.4b stack (it lives on `origin/main`, which this stack has not yet rebased onto). All UMS-required gates plus the "no by luck work" additions **were** run.

## Remaining risks

- **Code risk: zero.** No code is touched.
- **Test-flake risk: very low.** All 18 tests use isolated in-memory SQLite with `PRAGMA foreign_keys=ON` by default, no shared state, no time-dependent assertions. One read-filter test temporarily disables FK checks only to insert deliberately inconsistent fixture data.
- **Reviewer-flow risk: low.** One file, ~680 lines, 18 tests, each short and focused. The naming and ordering directly mirror `tests/org/test_sql_channel_registry.py`.

## Follow-up recommendations

After this PR ships, the three-PR sequence (PR #28 `.gitignore`, PR #29 bank reconciliation tests, PR #30 channel groups tests) is complete. Remaining queued items from PR #26's handoff (lower priority):

- Direct tests for `finance/explanations.py` (206 lines, no direct test file).
- Direct tests for `finance/revenue_facts.py` (currently thin coverage).
- Direct tests for `reports/exports.py`, `reports/raw_files.py`, `connectors/credentials.py`.

These are independent of any current PR and can be picked off in any order.

## Rollback notes

- Single-file PR. Revert is `git revert <merge-commit>` — touches one new test file.
- No data, schema, runtime state, or downstream consumer is touched; rollback is safe to apply to a running deployment.
- If the test file is reverted, `org/sql_channel_groups.py` still functions identically — the production code is unchanged.
