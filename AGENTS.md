# UMS Smart Revenue Control Center

Codex operates in this repository as the director software architect,
backend reviewer, and validation owner for the UMS Smart Revenue project.

The goal is not only to make code work. The goal is to keep revenue numbers,
authorization, audit trails, database state, and delivery docs correct before
any files are uploaded or pushed.

## System Context

UMS Smart Revenue is an internal numbers-first system for YouTube operations.
It answers what each channel, company, sector, and group generated; what was
finalized; what reached AdSense or payment; what was deducted; and why payment
amounts differ.

Repository shape:

```txt
backend/       Python FastAPI application, SQLAlchemy, Alembic, services
frontend/      Vite + React SPA (TypeScript)
Docs/          Architecture, data model, workflows, security, API contracts
mockups/       Product and UI planning assets
tests/         API, auth, finance, and repository tests
pyproject.toml Runtime and tooling contract
alembic.ini    Migration entrypoint
```

Architecture:

```txt
Frontend SPA (Vite + React)
  -> FastAPI routes
  -> service and repository layers
  -> PostgreSQL operational database (source of truth)
```

PostgreSQL is the sole source of truth for finance, audit, tenancy, and
authorization state. SQL-backed read models serve hierarchy, reconciliation,
explanation, and analytics views.

## Non-Negotiable Engineering Rules

1. Keep FastAPI routes thin. Routes may parse input, resolve dependencies,
   enforce boundary permission checks, call services, and convert typed errors.
2. Keep business logic in services and finance/auth/org modules, not in route
   handlers or UI-facing serializers.
3. Keep database access in repositories/adapters. Do not scatter SQLAlchemy
   queries across route handlers.
4. Preserve every financial number's source, formula, confidence, and export
   value.
5. Never calculate official finance results directly in the UI.
6. Never weaken authorization, trusted-gateway validation, database-backed
   principal loading, scope checks, or fail-closed behavior to pass a test.
7. Never skip, xfail, delete, or loosen tests to make CI pass.
8. Never push, upload, open a PR, or claim review readiness before personally
   running the required local validation for the changed scope.
9. Never treat remote CI, CodeRabbit, another assistant, or a previous run as a
   substitute for current local verification.
10. Never hide a discovered bug in the touched scope. Fix it directly or record
    the exact file, risk, and next PR recommendation.
11. Never remove planning docs or mockups unless the operator explicitly asks
    for that deletion.

## Required Workflow

### Understand

Before modifying code, inspect the relevant files and identify:

- The purpose and architectural layer of each file.
- Related routes, services, repositories, models, migrations, docs, and tests.
- Whether the change touches finance numbers, authz, audit, SQLAlchemy,
  Alembic, PostgreSQL, connectors, exports, or docs.
- Whether database rows, enum/status values, JSON shapes, API responses, or
  authorization behavior change.
- Which tests and docs must change with the implementation.

Do not guess when the repository contains the answer.

### Plan

Before implementation, determine:

- What files should change.
- What files must not change.
- Whether logic belongs in API routes, services, repositories, auth, finance,
  reports, config, or tests.
- Whether the change affects the OPUS repo, this UMS repo, or both.
- Which exact validation commands will be run before upload or push.
- Whether a migration, data reset, seed, backfill, rollback note, or docs update
  is required.

### Implement

When writing code:

- Make the smallest high-quality change that solves the task.
- Use explicit types and Pydantic models at API boundaries.
- Use SQLAlchemy parameterization and repository methods for database access.
- Use transactions for dependent multi-step writes.
- Guard null, empty, malformed, duplicate, stale, unauthorized, disabled,
  archived, locked, and race-condition scenarios.
- Keep route handlers readable and narrow.
- Preserve compatibility unless a breaking change is explicitly requested and
  documented.

## Validation and Upload Gate

Codex must run validation locally before uploading files, pushing commits,
opening PRs, or claiming the branch is ready.

Required baseline for code changes:

- `python -m ruff check backend tests scripts`
- `pytest -q`
- `git diff --check`

Changed-scope additions:

- API, service, auth, finance, connector, graph, or report changes require the
  targeted pytest files for that behavior.
- Database model or migration changes require Alembic review and the relevant
  migration test or command for the disposable/local database state.
- Authorization changes require tests for missing auth, invalid gateway token,
  disabled or unknown user, insufficient permission, scoped access, and
  fail-closed storage errors where practical.
- Finance changes require tests for source, formula, confidence, locks,
  overrides, duplicate records, missing data, rounding, and export/API shape.
- Documentation-only changes require doc diff hygiene, accurate commands,
  accurate dates/statuses, and `git diff --check`.

If a validation gate cannot run, record:

- The exact command.
- The exact blocker.
- Whether the blocker is environment/tooling/credential/data related.
- The operator-safe command to rerun later.

Failing validation is not optional. Fix the failure, prove it is unrelated
pre-existing debt, or stop and report the blocker.

Before upload or push:

- Re-read the final diff.
- Confirm no unrelated or user-owned edits are included.
- Confirm all changed files are intentional.
- Run the required validation after the final edit.
- For review-loop work, re-check the live PR state after the last push and do
  not claim completion while current unresolved review threads or failed checks
  remain unreviewed.

## Database and Blast-Radius Rules

A blast-radius review is mandatory when changing:

- SQLAlchemy ORM models.
- Alembic migrations.
- Table names, column names, nullability, defaults, constraints, indexes, enum
  values, status values, timestamps, or JSON payloads.
- Query filters, inserts, updates, deletes, locks, reconciliation behavior,
  audit writes, principal loading, direct permission grants, or scope logic.
- Any field that contributes to finance totals, confidence, or exports.

For database-related changes, explicitly answer:

- Which tables and ORM models are affected?
- Is PostgreSQL still the source of truth?
- Could existing migrations, tests, seed data, or docs break?
- Could authorization or audit behavior become more permissive?
- Could finance results, month locks, manual overrides, or payment matching
  change?
- Is the migration backward-compatible, or intentionally destructive because
  the data is disposable at this phase?
- Is a rollback, reset, reseed, or irreversible-change note required?

State one of:

- `No migration/backfill required.`
- `Potential migration impact: review <file/table/field>.`
- `Confirmed migration required: <file/function/query>.`
- `Unsafe without migration/backfill.`
- `Disposable pre-alpha data reset accepted: reset/reseed required.`

The statement must be backed by file paths or search evidence.

## Error Handling and Logging

Use typed domain exceptions in services and repositories, then translate them at
the route boundary into `HTTPException` responses with safe messages.

Rules:

- Do not use bare `except:`.
- Do not swallow errors silently.
- Do not leak secrets, tokens, private user data, sensitive SQL values, or
  internal infrastructure details in error responses.
- Use Python logging where runtime diagnostics are needed. Do not leave
  temporary print debugging in backend code.
- Preserve fail-closed behavior for auth, permissions, trusted gateway checks,
  and database-backed principals.

## Professional Commenting Standard

Do not add decorative comments above trivial code. Add a concise contract block
directly above meaningful new or modified blocks: API endpoints, service entry
points, repository methods, database writes, transactions, authorization logic,
finance calculations, reconciliation, exports, and non-obvious fixes.

Use this Python format exactly:

```py
# ============================================================================
# Purpose: Clear, concise explanation of what this block does.
# Database/ORM: Related SQLAlchemy models/tables, or "None".
# Standards: Logging, error handling, typed boundaries, and layer ownership.
# Blast Radius: Authorization, finance, audit, exports, or "None detected".
# Connections:
#   - File: backend/ums_smart_revenue/config/settings.py -> Explain dependency.
#   - File: Docs/12_BACKEND_API_SPEC.md -> Explain API contract link.
# ============================================================================
```

For TypeScript or future frontend code, use the same content with `//`
comments.

When directly correcting a bug, disconnected variable, missing error path,
deprecated method, unsafe query, or contract drift, fix the issue and add a
focused inline note:

```py
# FIX: Reconnected the normalized payment identifier to the matching query;
# the previous branch validated the value but queried with the raw input.
```

`FIX:` comments are for real corrections only. Do not use them for style-only
changes, speculative refactors, or weakened behavior.

## Review and PR Discipline

For CodeRabbit, Copilot, GitHub, or operator review feedback:

- Inspect current live PR state before editing.
- Distinguish current unresolved threads from stale historical comments.
- Fix the contract issue, not only the visible symptom.
- Run local validation after each material fix.
- Push only intentional files.
- Re-check current review-thread state and checks after the last push.
- Do not claim clean remote review state without current evidence.

For every implementation PR, maintain clear docs or handoff notes covering
scope, non-goals, files changed, behavior changes, tests run, failures, risks,
rollback/reset notes, and next recommendations.

## Repository Working Rules

- Backend code lives in `backend/ums_smart_revenue/`.
- Tests live in `tests/`.
- Architecture and product contracts live in `Docs/`, `DESIGN.md`, and
  `PRODUCT.md`.
- Runtime and tool versions are declared in `pyproject.toml`.
- Alembic migrations live under `backend/ums_smart_revenue/db/alembic/`.
- PostgreSQL is the sole source of truth.
- Mockups and Markdown planning files are active project assets, not disposable
  clutter.
