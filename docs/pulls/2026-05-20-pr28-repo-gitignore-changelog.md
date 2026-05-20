# PR #28 — Backport `.gitignore` from main — Changelog

**Date:** 2026-05-20
**PR:** https://github.com/XGenerationy/Youtube/pull/28
**Branch:** `pr/s2-4b-repo-gitignore`
**Base:** `pr/s2-4a-tenant-id-on-operational-tables`

## Added

- `.gitignore` (227 lines, root of repo). Byte-identical content to `origin/main:.gitignore`.
- `docs/pulls/2026-05-20-pr28-repo-gitignore-report.md` (this PR's report artifact).
- `docs/pulls/2026-05-20-pr28-repo-gitignore-changelog.md` (this file).
- `docs/pulls/2026-05-20-pr28-repo-gitignore-handoff.md` (handoff artifact).

## Changed

### Source semantics — none

No business logic, finance calculation, tenant scoping, authorization rule, audit behavior, migration semantics, API contract, or Neo4j read-only projection changed.

### Lint / format — none

No Python source file is touched. The 652 pre-existing ruff errors and 102 pre-existing `ruff format` unclean files are documented but **not modified** by this PR — that work is owned by the still-open PR #27.

### Symbol renames — none

### Enum migration — none

### Generic syntax — none

### Cypher whitespace — none

### Alembic — none

### SQL — none

### Tests — none

No test was added, modified, removed, or skipped. Pytest total: 507 → 507 unchanged.

## Removed

- Nothing removed.

## Behavior changes

- **Source semantics: none.** Pytest count unchanged: 507 → 507.
- **`git status` output:** future invocations on this stack branch will no longer list `__pycache__/`, `.pytest_cache/`, `.ruff_cache/`, `.coverage(.*)`, `htmlcov/`, `.venv/`, `venv/`, `*.egg-info/`, `*.egg`, `dist/`, `build/`, `*.log`, IDE/OS noise, etc. as untracked. This is the intended improvement.
- **`git add backend tests`** will no longer accidentally stage `*.pyc` files alongside source.

## Test surface change

- Pytest total: 507 → 507 (unchanged).
- No test file added, modified, removed, or renamed.
- No fixture, conftest, or markers change.

## Documentation changes

- 3 new artifacts under `docs/pulls/` (report + changelog + handoff). No edits to existing `Docs/*.md` architecture or API specs.

## Schema / data

- **No** Prisma/Alembic migration. **No** DB column, index, constraint, enum, status, or JSON-shape change.

## Configuration / runtime

- No `.env`, `pyproject.toml`, `alembic.ini`, Docker, CI, or operational contract change.
- One working-tree hygiene file (`.gitignore`) added at the repo root.

## Compatibility with origin/main

- `origin/main:.gitignore` is the source of the content. The two are byte-identical at the time of this PR.
- When the stack eventually rebases onto or merges with `origin/main`, this file is already in sync. No divergence introduced.

## Patterns the new `.gitignore` covers (summary)

- Python byte-compile (`__pycache__/`, `*.py[cod]`, `*$py.class`, `*.so`).
- Distribution/packaging artifacts (`build/`, `dist/`, `*.egg-info/`, `*.egg`, `wheels/`, etc.).
- Tooling caches (`.pytest_cache/`, `.ruff_cache/`, `.mypy_cache/`, `.tox/`, `.nox/`).
- Coverage outputs (`.coverage`, `.coverage.*`, `htmlcov/`, `coverage.xml`).
- Virtual environments (`.venv/`, `venv/`, `env/`, `ENV/`).
- OS noise (`.DS_Store`, `Thumbs.db`, `Spotlight-V100`, etc.).
- IDE noise (`.idea/`, `.vscode/`, `*.swp`, `*~`).
- Node/frontend (`node_modules/`, `.next/`, `out/`, `build/`, `.turbo/`).
- Playwright (`test-results/`, `playwright-report/`).
- Storybook (`storybook-static/`).
- Local secrets/dumps (`*.pem`, `*.key`, `*.crt`, `*.dump`, `*.sql.gz`).
- Docker overrides (`docker-compose.override.yml`).
- Helm chart caches (`deploy/helm/*/charts/`).
- Terraform state (`*.tfstate*`, `.terraform/`).
- Local agent/tool runtime (`.claude/`, `CLAUDE.md`).
- Generated reports / logs / scratch.

## Patterns explicitly verified NOT to match (audit)

- `AGENTS.md` — NOT matched by any line. (User-owned local file in some worktrees.)
- `docs/AGENT_VALIDATION_PLAYBOOK.md` — NOT matched by any line. (User-owned local file.)
- All currently-tracked files — `git ls-files | check-ignore` sweep returned empty.
