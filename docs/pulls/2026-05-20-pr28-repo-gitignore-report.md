# PR #28 — Backport `.gitignore` from main — Report

**Date:** 2026-05-20
**PR:** https://github.com/XGenerationy/Youtube/pull/28
**Branch:** `pr/s2-4b-repo-gitignore`
**Base:** `pr/s2-4a-tenant-id-on-operational-tables` (rolling integration head `5c53593`)
**Head commit:** `d59e429` (first commit; this report and the changelog/handoff land in a second commit)
**Status:** Open, all gates green locally, awaiting review.

## What was requested

The user picked "Add top-level `.gitignore`" as the next PR after PR #27 shipped. The need was concrete: the absence of a `.gitignore` on the `pr/s2-4a-tenant-id-on-operational-tables` stack kept tripping the `git add backend tests` step during PR #27, because that command was picking up the entire `__pycache__/` tree that pytest had created. The user's chosen scope was direct: add the `.gitignore` so future stack PRs do not have to fight that.

During implementation I discovered an important fact that reshaped the scope: **`origin/main` already has a 227-line `.gitignore`.** The S2.4b feature stack is far behind main on this file (and on many other infrastructure files: `Makefile`, `Dockerfile`, `README.md`, `ci/` lane, `frontend/`, `Docs/17_MULTI_TENANT_ARCHITECTURE.md`, `Docs/18_MULTI_CURRENCY_ENGINE.md`, exchange-rate features, etc). I reported this to the user; they confirmed the right scope is to **backport `origin/main:.gitignore` byte-identically into the stack**, so when the stack eventually merges with main this file is already in sync.

## What was actually done

A single commit, `d59e429`, adds **byte-identical** content from `origin/main:.gitignore` to the stack head:

- 227 lines.
- File content: identical to `origin/main:.gitignore` (verified with `git show origin/main:.gitignore | cmp -s - .gitignore`).
- No other repo file touched in commit `d59e429`.

## Phased execution

| Phase | Action | Pytest after | Notes |
|---|---|---|---|
| Baseline | Inspect base (no `.gitignore` on stack) | 507 passed | 652 ruff errors, 102 unformatted files — all pre-existing |
| 1 | `git show origin/main:.gitignore \| cmp -s - .gitignore` | 507 passed | Verified byte-identical content |
| 2 | `git ls-files \| git check-ignore --stdin` sweep | n/a | **Empty** — no tracked file becomes ignored |
| 3 | Pattern audit against `AGENTS.md` and `docs/AGENT_VALIDATION_PLAYBOOK.md` | n/a | Neither matched |
| 4 | Full pre-push gate (ruff/format/pytest/diff/markers/import/alembic) | 507 passed | Baseline preserved |
| 5 | Commit `d59e429`, push, open PR #28 | — | — |

## Quality checks performed

- `python -m ruff check backend tests` — 652 errors (pre-existing on `pr/s2-4a`; this PR adds 0; resolved by the still-open PR #27).
- `python -m ruff check backend tests --statistics` — 582 E501, 52 I001, 8 UP037, 3 N818, 3 UP042, 2 UP035, 1 UP045, 1 UP047 (identical to base).
- `python -m ruff format --check backend tests` — 102 files would be reformatted (identical to base; addressed by PR #27).
- `python -m pytest -q` — **507 passed, 7 warnings in 30s** (baseline preserved exactly).
- `git diff --check` — clean (exit 0).
- Conflict-marker scan (`git grep -nE '^(<{7}|={7}|>{7})( |$)' -- ':!docs/pulls/' ':!*.md'`) — clean.
- Conflict-marker scan over working tree — clean.
- Import smoke (`from ums_smart_revenue.app import app`) — ok.
- Alembic linear history — single head `20260518_0001`.

PR-specific:
- `git ls-files \| git check-ignore --stdin` — **empty** (no tracked file becomes ignored).
- Pattern audit for user-owned files: `AGENTS.md`, `docs/AGENT_VALIDATION_PLAYBOOK.md` — NOT matched.
- Staged-diff `git diff --cached --check` — clean.

## Architecture & quality posture

- **No source semantics change.** Pytest count is unchanged: 507 → 507.
- **No graph projection impact detected.** The Neo4j read-only contract is untouched; this file does not affect database access, Cypher, or projection behavior.
- **No authorization or audit behavior change.** `.gitignore` is a working-tree hygiene file; not loaded by FastAPI.
- **No finance number behavior change.** No code in `backend/ums_smart_revenue/finance/**` is touched.
- **No tenant scoping change.** No SQL filters or stamps added.
- **Security**: zero new attack surface. `.gitignore` cannot loosen anything; it only hides local development clutter from `git status`.
- **Observability**: no logging change.
- **Testability**: no test count change.

## Blast-radius statement

*No graph projection impact detected.* No SQLAlchemy ORM, no Alembic migration, no route, no service, no repository, no DI provider, no schema, no test, no Cypher. PostgreSQL/Neo4j contract is unchanged. PR #28 only adds a working-tree hygiene file.

## Pre-existing baseline (NOT introduced by this PR)

The base branch `pr/s2-4a-tenant-id-on-operational-tables` has these pre-existing flags as of `5c53593`:

| Category | Count | Source / fix path |
|---|---|---|
| `E501` line-too-long | 582 | Addressed by PR #27 (still open) |
| `I001` unsorted-imports | 52 | Addressed by PR #27 |
| `UP037` quoted-annotation | 8 | Addressed by PR #27 |
| `N818` error-suffix-on-exception-name | 3 | Addressed by PR #27 |
| `UP042` replace-str-enum | 3 | Addressed by PR #27 |
| `UP035` deprecated-import | 2 | Addressed by PR #27 |
| `UP045` non-pep604-annotation-optional | 1 | Addressed by PR #27 |
| `UP047` non-pep695-generic-function | 1 | Addressed by PR #27 |
| **Total ruff** | **652** | |
| `ruff format --check` would-reformat files | 102 | Addressed by PR #27 |

This PR explicitly does **not** address those flags — that is PR #27's scope. This PR's commit `d59e429` touches only `.gitignore`. Both PRs can land in either order.

## Validation that could NOT be run

- The `ci/` self-hosted pre-push gate referenced in OPUS CLAUDE.md does not exist on this stack branch. (It does exist on `origin/main`, but the stack has not yet picked it up.) Per UMS CLAUDE.md, the required gates are `ruff check + pytest -q + git diff --check`, augmented with the additional gates required by the user's "no by luck work" directive — all of which **were** run.
- `make codeql-triage` — not applicable; OPUS-only target.

## Remaining risks

- **Code risk: zero.** No code is touched.
- **Process risk: low.** The file is byte-identical to `origin/main:.gitignore`, so there is no divergence introduced.
- **Reviewer-flow risk: trivial.** One file, 227 added lines, 0 deletions.

## Follow-up recommendations

- After this PR merges, PR #27's reformatting work continues to address the 652 pre-existing errors. The two PRs are deliberately independent and can land in either order.
- The wider drift between `pr/s2-4a` and `origin/main` (CI lane, frontend, Makefile, Dockerfile, README.md, exchange rates, additional docs and migrations) is **out of scope** for this PR. A future operator-led rebase or merge-from-main will close that.

## Rollback notes

- Single-file PR. Revert is `git revert <merge-commit>` — touches one file (`.gitignore`).
- No data, schema, runtime state, or downstream consumer is touched; rollback is safe to apply to a running deployment.
