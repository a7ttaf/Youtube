# PR #30 — Channel Group Registry Tenant-Scope Tests — Changelog

**Date:** 2026-05-20
**PR:** https://github.com/XGenerationy/Youtube/pull/30
**Branch:** `pr/s2-4b-org-2-channel-groups-tests`
**Base:** `pr/s2-4a-tenant-id-on-operational-tables`

## Added

- `tests/org/test_sql_channel_groups.py` (~680 lines, 18 tests).
- `docs/pulls/2026-05-20-pr30-channel-groups-tenant-tests-report.md` (this PR's report artifact).
- `docs/pulls/2026-05-20-pr30-channel-groups-tenant-tests-changelog.md` (this file).
- `docs/pulls/2026-05-20-pr30-channel-groups-tenant-tests-handoff.md` (handoff artifact).

Note: the original PR objective and first push covered 15 tests / 522 total
pytest tests. After CodeRabbit review follow-up, this head intentionally carries
18 tests / 525 total pytest tests; those are the authoritative validated numbers
for the current branch head.

## Changed

### Source semantics — none

No `backend/ums_smart_revenue/**` file is touched. No business logic, finance calculation, tenant scoping, authorization rule, audit behavior, migration semantics, API contract, or Neo4j read-only projection changed. `org/sql_channel_groups.py` is unchanged.

### Lint / format — none

No Python source file is modified outside of the new test file. The new test file passes `ruff check` and `ruff format --check`. The 652 pre-existing ruff errors and 102 pre-existing `ruff format` unclean files on the base branch are documented but **not modified** by this PR — that work is owned by the still-open PR #27.

### Symbol renames — none

### Enum migration — none

### Generic syntax — none

### Cypher whitespace — none

### Alembic — none

### SQL — none

### Tests — added 18

Direct registry-layer tenant-isolation tests for `SqlAlchemyChannelGroupRegistry`:

| Category | Tests added | Coverage |
|---|---|---|
| `create_group` writes | 5 | Default (no context), explicit constructor, `TENANT_CTX`-by-default, explicit overrides `TENANT_CTX`, empty channel list |
| `_channel_rows_by_external_ids` rejection | 1 | Cross-tenant channel external id → `KeyError` |
| `list_groups` reads | 3 | Filters to bound tenant, excludes inactive, `TENANT_CTX`-by-default |
| `get_group` reads (IDOR) | 2 | Cross-tenant `group_id` returns `None`; malformed UUID returns `None` |
| `add_members` | 4 | Cross-tenant group_id raises `KeyError`; stamps bound tenant on inserts; empty list no-op; duplicate-member `IntegrityError` recovery path |
| `remove_member` | 1 | Cross-tenant group_id raises `KeyError` and does not delete the foreign row |
| `update_group` | 1 | Cross-tenant group_id raises `KeyError` and does not mutate the foreign row |
| `_channel_ids_by_group` (read-layer dual filter) | 1 | Empty result for another tenant's group_id and exclusion of a mismatched-tenant member row on the same group id |
| **Total** | **18** | |

The tests use in-memory SQLite with `PRAGMA foreign_keys=ON` enabled in a `@event.listens_for(engine, "connect")` hook, mirroring `tests/org/test_sql_channel_registry.py` (the registry test PR #25 established). One read-filter test temporarily disables SQLite FK checks only to insert deliberately inconsistent fixture data that proves the member-row tenant predicate.

## Removed

- Nothing removed.

## Behavior changes

- **Source semantics: none.** Pytest count: 507 → 525 (+18).
- **Regression guard added:** if a future PR accidentally removes a `tenant_id` filter or stamp from `SqlAlchemyChannelGroupRegistry`, or accidentally restores `session.get(ChannelGroupORM, group_id)` in `_get_group_row` (the IDOR vector closed by PR #25), the corresponding test in this file will fail.

## Test surface change

- Pytest total: 507 → 525 (+18).
- 1 new test file: `tests/org/test_sql_channel_groups.py`.
- 18 new test functions, all `test_*` discoverable by pytest's default collection.
- No existing test file, fixture, or conftest is modified.
- The shared `test_org_sql_repositories_validate_tenant_id_constructor_input` in `tests/org/test_sql_channel_registry.py` already exercises `SqlAlchemyChannelGroupRegistry`'s constructor `tenant_id` validation, so this file does **not** re-add that test (avoids duplicate-test redundancy).

## Documentation changes

- 3 new artifacts under `docs/pulls/` (report + changelog + handoff). No edits to existing `Docs/*.md` architecture or API specs.

## Schema / data

- **No** Prisma/Alembic migration. **No** DB column, index, constraint, enum, status, or JSON-shape change.

## Configuration / runtime

- No `.env`, `pyproject.toml`, `alembic.ini`, Docker, CI, or operational contract change.

## Pattern compatibility

- Mirrors `tests/org/test_sql_channel_registry.py` for the session-builder, seed shape, and naming convention.
- Uses fresh canonical tenant ids for this surface:
  - `DEFAULT_TENANT_ID = UUID(UMS_TENANT_ID)`
  - `OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-000000051999")` (`5xxxx` range to avoid collision with other test files' canonical ids — AdSense uses `3xxxx`, bank reconciliation uses `4xxxx`).
- Uses the same `_tenant(id, slug=...)` helper shape and the same `TENANT_CTX.set/.reset` token pattern as the finance tenant-scope tests.

## Compatibility with origin/main

- This PR is purely additive on the S2.4b stack. When the stack rebases onto / merges with `origin/main`, the new test file ships unchanged. `org/sql_channel_groups.py` on main is API-compatible with the stack version (the tenant wiring that PR #25 introduced is part of S2.4b which is what this stack delivers; main has not yet received it).
