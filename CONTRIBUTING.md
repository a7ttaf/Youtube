# Contributing to UMS Smart Revenue Control Center

Thanks for the interest. This is a finance command center — confidentiality, integrity, and auditability come before everything else. The contribution flow is designed to keep that posture.

## TL;DR

```powershell
git checkout -b feat/<short-slug>             # one logical change per branch
uv sync --extra dev --extra test --extra lint
uv run ruff check . && uv run mypy backend && uv run pytest -q
git commit -s -m "feat(scope): short summary"
# push, open a PR, let CodeRabbit + CODEOWNERS review
```

## Before you start

1. **Read the spec** for the area you're touching: [Docs/](Docs/). Decisions are documented, not folklore.
2. **Check [Docs/16_OPEN_DECISIONS.md](Docs/16_OPEN_DECISIONS.md)** — your change may depend on a decision that hasn't been made. Surface it before coding.
3. **Open an issue first** for non-trivial work (new endpoint, schema change, new third-party dependency). Save yourself a rejected PR.

## Branching

| Branch | Source | Used for |
|---|---|---|
| `main` | — | Always green; release source |
| `feat/<slug>` | `main` | New features |
| `fix/<slug>` | `main` | Bug fixes |
| `chore/<slug>` | `main` | Tooling, docs, refactors |
| `codex/<slug>` | `main` | Codex-generated work; reviewed identically |

Squash on merge. Branch names stay lowercase, kebab-case, ≤ 40 chars.

## Commit messages

Conventional Commits:

```
<type>(<scope>): <imperative summary, ≤ 72 chars>

<body — what changed and *why*, not how. Reference issues/PRs.>

Co-Authored-By: <name> <email>   (when paired)
Signed-off-by: <name> <email>     (DCO; use `git commit -s`)
```

Types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `build`, `ci`, `chore`, `revert`.

Scopes (common): `auth`, `revenue`, `finance-close`, `connectors`, `exports`, `db`, `tenancy`, `fx`, `frontend`, `ci`, `helm`.

## Definition of done

A PR is mergeable when **all** of the following are true:

- [ ] **Project-declared validation is green** (`uv run pytest`, `uv run ruff check`, `uv run mypy backend`; plus CI workflows once they exist).
- [ ] **CodeRabbit review** has no unresolved blocking comments.
- [ ] **Tests cover the change** — unit + integration where boundaries are involved. Money math gets property-based coverage via `hypothesis`.
- [ ] **`mypy` is strict-clean** on touched modules.
- [ ] **Public API changes** are reflected in the OpenAPI snapshot and `Docs/12_BACKEND_API_SPEC.md`.
- [ ] **DB schema changes** ship with a reversible Alembic migration + a model parity test + a forward→down→up CI step.
- [ ] **Auth-impacting changes** include a negative-permission test (`test_*_denies_*`).
- [ ] **Money-impacting changes** preserve invariants — `Decimal` arithmetic only; sum-to-total holds; `deduction_pct` is `0.0000` when gross is `0`.
- [ ] **Tenant isolation** — any new query goes through the tenant-aware repository; tenant-A can never see tenant-B data.
- [ ] **`CHANGELOG.md`** has an `Unreleased` entry under the right heading.
- [ ] **`CODEOWNERS` for the touched paths** have approved.

## Style

| Topic | Rule |
|---|---|
| Lint | `ruff` (config in `pyproject.toml`); no warnings ignored without inline justification. |
| Types | `mypy --strict` on `backend/ums_smart_revenue/finance/`, `auth/`, `tenancy/`, `db/`. |
| SQL | Prefer Postgres-compatible SQL; add a declared SQL linter before making it a required gate. |
| Logging | `structlog` with bound context (`request_id`, `tenant_id`, `user_id`); never `print`. |
| Errors | Raise domain-specific exceptions; never swallow without log + reraise. |
| Comments | Only where the **why** is non-obvious; describe invariants, not what the code does. |
| Tests | One assertion focus per test; arrange / act / assert is fine. Name `test_<unit>_<behaviour>_when_<context>`. |
| Money | `decimal.Decimal` for storage and computation. `float` is a bug. |

## Review SLA

| Severity | First response |
|---|---|
| Security fix | 1 business day |
| Bug fix on production path | 2 business days |
| Feature work | 5 business days |
| Docs / chore | best effort |

## How not to break production

- **Never** edit an already-merged Alembic migration. Add a follow-up.
- **Never** introduce a flag/feature/code path you don't intend to ship — no dead code, no half-stubs, no `pass # TODO`.
- **Never** widen a permission decorator without a paired test that proves the negative.
- **Never** commit a file containing a real token, key, or password. Run an available secret scanner before pushing until repository-managed hooks are added.

If a secret scanner flags something committed earlier in the branch, the answer is rotate the secret in upstream systems first, then rewrite history. Open a security incident, do not just `git push --force`.

## Reporting security issues

See [SECURITY.md](SECURITY.md). Do not open a public issue.

## Code of conduct

See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
