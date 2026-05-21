# PR #27 — Full Repo Ruff Cleanup — Handoff

**Date:** 2026-05-20
**PR:** https://github.com/XGenerationy/Youtube/pull/27
**Branch:** `pr/s2-4b-repo-ruff-cleanup`
**Base:** `pr/s2-4a-tenant-id-on-operational-tables`
**Status at handoff:** Open. Cleanup commit `89bcbe8` pushed. Docs commit lands as a second commit on this branch.

## Scope

Drive the whole-tree ruff gate from 652 errors → 0 across the UMS Smart Revenue repo. The cleanup is a single PR (this one), independent of and disjoint from PR #26 (AdSense tenant tests). The trigger was the user's correction after PR #26's gate analysis: I had framed the 652 errors as "pre-existing debt — not blocking", which they explicitly rejected.

## Non-goals

- No business-logic behavior change; one representational string change is documented below. Pytest count is identical (490 baseline → 490 head).
- No tenant-scoping, finance, auth, audit, route, service, repository, DI provider, schema, or migration semantics changed.
- No new dependencies. No `pyproject.toml`, `alembic.ini`, Docker, CI, or env config change.
- No `Docs/*` architecture or API-spec updates (no behavioral contract change to document).
- Not bundling this with PR #26 — the two are deliberately disjoint and can land in either order.

## Files changed

Commit `89bcbe8` (cleanup): 108 files changed, +5041 / -3527. Broken down by category in the changelog. Major buckets:

- `backend/ums_smart_revenue/api/*.py` (multiple files): formatting + 4 error-message line breaks.
- `backend/ums_smart_revenue/auth/api_guards.py`: rename `AccessDenied → AccessDeniedError`, remove `TypeVar`, convert `guarded_call` to PEP 695.
- `backend/ums_smart_revenue/auth/{permissions,roles,scopes}.py`: `(str, Enum)` → `StrEnum` for 3 enums.
- `backend/ums_smart_revenue/graph/{cypher.py, readonly_service.py}`: Cypher whitespace + rename `GraphAccessDenied → GraphAccessDeniedError`.
- `backend/ums_smart_revenue/db/alembic/versions/*`: 8 files with split downgrade docstrings, 4 files with split SQL CHECK constraint strings.
- `backend/ums_smart_revenue/db/tenant_models.py`, `finance/reconciliation.py`, `finance/revenue_summary.py`: split long error-message strings.
- `tests/**/*.py`: matching test updates + formatting + concatenated string breaks for long literals.

Second commit (this artifact set):

- `docs/pulls/2026-05-20-pr27-repo-ruff-cleanup-report.md` (new).
- `docs/pulls/2026-05-20-pr27-repo-ruff-cleanup-changelog.md` (new).
- `docs/pulls/2026-05-20-pr27-repo-ruff-cleanup-handoff.md` (new).

## Behavior changes

- **At runtime: none.** Same 490 tests pass.
- **One visible behavior change for downstream code**: `str(Permission.VIEW_ANALYTICS)` now returns `"analytics.view"` instead of `"Permission.VIEW_ANALYTICS"`. Same for `RoleKey` and `ScopeType`. Equality, `.value`, and JSON serialization are unchanged. No tests rely on the old form.

## Tests run

- `python -m ruff check backend tests` — **All checks passed!**
- `python -m ruff check backend tests --statistics` — empty (0 errors in any category)
- `python -m ruff format --check backend tests` — 176 files already formatted
- `python -m pytest -q` (full suite) — **490 passed, 7 warnings in 29s**. Baseline before this PR: 490 passed. Identical.
- `python -m pytest -q` per area (finance, db, auth, api, org, graph, reports, tenancy) — each green individually.
- `git diff --check` — clean (exit 0). Pre-fix this gate was flagging trailing whitespace on every line of 35 CRLF files.
- Conflict-marker scan (`git grep -nE '^(<{7}|={7}|>{7})( |$)'`) — clean for both tracked files and working-tree `.py` files.
- Import smoke (`PYTHONPATH=backend python -c "from ums_smart_revenue.app import app"`) — ok.
- Renamed-symbol import smoke (`from ums_smart_revenue.auth.api_guards import AccessDeniedError, guarded_call, require_permission`) — ok.
- StrEnum smoke (`str(Permission.VIEW_ANALYTICS)` returns `analytics.view` as expected) — ok.
- `alembic heads` — single linear head `20260518_0001` on the historical PR #27 branch. Integrated branches after PR #36 use merge head `20260521_0001`.

## Failures / skipped gates

- None.
- Note on environment: this branch was developed in a git worktree at `/home/mahmoud/work/youtube-ums-cleanup` (created via `git worktree add` off `pr/s2-4a-tenant-id-on-operational-tables`). The PR #26 worktree at `/home/mahmoud/work/youtube-ums` was left **completely untouched** during this PR's work. Both worktrees share the same `.git` directory and venv tooling.
- No remote CI run; UMS repo has no `.github/workflows/`, no `Makefile`, no `scripts/`, and no `ci/` directory.

## Risks

- **Code risk: low.** 490 tests pass, including every call site of the renamed exceptions and migrated enums. The PEP 695 generic syntax requires Python 3.12+; the project pins Python 3.14, so this is in range.
- **Reviewer-flow risk: medium.** This is a wide-diff PR (108 files, +5041/-3527). Most of the line count comes from `ruff format` line-breaking and CRLF→LF normalization. A reviewer who looks at the file count and balks would be acting on a misleading signal — semantically, almost nothing changed. I recommend reviewers focus on the changelog's "Symbol renames", "Enum migration", and "Generic syntax" sections; the rest is mechanical formatting.
- **Downstream-consumer risk: low**, with one caveat. Anything outside this repo that imports `AccessDenied`, `GraphAccessDenied`, or relies on `str(Permission.VIEW_ANALYTICS)` returning the legacy `"Permission.VIEW_ANALYTICS"` form will break. There are no such downstream consumers in this repo (verified by `grep -r`). For sibling repos (OPUS), I have not searched; the user should verify if OPUS or any other consumer imports from these UMS modules.
- **Cross-repo risk: stated explicitly.** OPUS CLAUDE.md says "Cross-repo assumptions must be stated explicitly instead of copied blindly between projects." I am NOT making the assumption that OPUS has no UMS imports — I am asking the user to verify before merging.

## Rollback / operational notes

- Single squash-merge unit. Revert is `git revert <merge-commit>` — touches all 108 files in one revert commit.
- No data, schema, runtime state, or downstream consumer migration needed.
- If the docs commit is rejected at review, drop just that commit (`git reset --hard 89bcbe8`, force-push) — but coordinate with the operator first; force-push is destructive per CLAUDE.md.
- *No graph projection impact detected.* PostgreSQL remains the source of truth; Neo4j read-only projections are not touched. The Cypher template whitespace change cannot affect projection semantics because Cypher is whitespace-tolerant.

## Next session / next PR recommendations

1. **Verify cross-repo impact**: search OPUS and any other sibling repo for imports of `AccessDenied`, `GraphAccessDenied`, or any code that does `str()` on `Permission`/`RoleKey`/`ScopeType`. If found, ship a coordinated change. (User-owned task; I cannot search across user-owned repos that aren't in this working directory.)
2. **PR B (the originally queued one)**: direct tenant-isolation tests for `SqlAlchemyBankReconciliationRepository`. Independent of this PR — base off the same rolling integration branch.
3. **Repo hygiene PR (separate, lower priority)**: add a top-level `.gitignore` covering `__pycache__/`, `*.pyc`, `.venv/`, `.coverage`, and `*.egg-info/`. The lack of one tripped me up during this PR's staging step (I had to use `find ... -print0 | xargs -0 git add` instead of `git add backend tests`).
4. **PR C–E (queued from PR #26's handoff)** in any order: `finance/explanations.py`, `org/sql_channel_groups.py` registry-layer direct tests, thin-coverage expansion on `finance/manual_overrides.py` and `finance/revenue_facts.py`.

## Open questions / decisions deferred

- Whether to add an explicit `[tool.ruff.format]` section to `pyproject.toml`. Currently using defaults; that's fine, but explicit configuration documents intent.
- Whether the project line-length policy should bump from 88 to 100 or 120. I left it at the de-facto 88 (defaulted from ruff) because the user's instruction was "fix everything" — adjusting the rule to make the gate green is the opposite of that.
- Whether to promote the `docs/pulls/` convention into the UMS `CLAUDE.md` as an explicit project rule. Currently the convention comes from OPUS CLAUDE.md applied cross-project per a 2026-05-20 user directive.

## Validation a future maintainer can rerun

```bash
# Run from the repository root.
git checkout pr/s2-4b-repo-ruff-cleanup
python -m ruff check backend tests
python -m ruff format --check backend tests
python -m pytest -q
git diff --check
git grep -nE '^(<{7}|={7}|>{7})( |$)' -- ':!docs/pulls/' ':!*.md' ; test $? -eq 1
PYTHONPATH=backend python -c "from ums_smart_revenue.app import app; print('ok')"
PYTHONPATH=backend python -m alembic -c alembic.ini heads
```

Expected: ruff clean, format clean, 490 passed, diff clean, no conflict markers, app imports, single alembic head.
