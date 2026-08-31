# Frontend P1 integration handoff

Date: 2026-08-31

Branch: `codex/frontend-p1-integration-20260831`

Base: `41b4953939b39b55345d3d7a168eeaf57c8e2b90`

## Scope

This branch manually consolidates the safe, current-main portions of draft PRs
#212, #214, and #215. It preserves PR #211's rolling month behavior while:

- removing fabricated shell counts, fixed reporting copy, the workflow rail,
  inert shell controls, and seven unsupported data panels;
- containing view render failures with fixed, payload-private operator copy and
  keeping root diagnostics free of thrown values and component stacks;
- retaining the process-wide write-in-flight latch outside recoverable view
  boundaries so a render crash cannot permit a duplicate write;
- failing closed when authorized revenue scopes are pending, empty, or failed;
- enforcing selected-month payment/bank grants for bank reconciliation and
  Smart Alerts reads;
- withholding month-close lock/unlock actions until their status/readiness
  inputs are trustworthy; and
- keeping official finance values server-derived. The frontend does not
  calculate an official result.

## Non-goals

- PR #229 and its `snapshotPanels` fixtures are not replayed or imported.
- Router introduction, query/cache architecture changes, and navigation-state
  redesign are excluded.
- Design-system replacement or visual retheming is excluded.
- Backend APIs, database models, migrations, seed data, and authorization
  contracts are unchanged.

## Files and behavior

- Shell and safety: `AppShell.tsx`, `ErrorBoundary.tsx`, `main.tsx`,
  `WriteInFlightContext.tsx`, `ActionStepper.tsx`, `icons.tsx`, `shared.tsx`, and
  `styles.css` own the de-mocked chrome, fixed error copy, persistent write
  latch, and presentation contracts.
- Wired views: `CommandView.tsx`, `CloseView.tsx`, `ExportsView.tsx`, and
  `RegistryView.tsx` remove unsupported panels and enforce truthful
  authorization/readiness states.
- Reads and month source: `useRevenueScopes.ts`, `useSmartAlerts.ts`,
  `months.ts`, and `mock/data.ts` provide fail-closed reads, exact selected-month
  behavior, and removal of obsolete fixtures.
- Tests under `frontend/tests/` cover root/view error privacy, crash-during-write
  recovery, scope storage failures, month-grant mismatch, close-read failures,
  removed mock surfaces, rolling-month behavior, and the view-owned Month
  selector's selected-month net-revenue URL.
- Planning/spec updates mark the superseded fabricated UI contract and record
  this integration without changing backend contracts.

## Validation

Final branch validation:

- `bun install --frozen-lockfile` — passed.
- `bunx vitest run tests/components/srcc/AppShell.test.tsx --reporter=dot` —
  passed, 1 file / 36 tests.
- `bunx vitest run --reporter=dot` — passed, 47 files / 552 tests. The run
  retained the suite's existing jsdom navigation and React `act(...)` stderr
  warnings; no test failed.
- `bun run typecheck` — passed (`tsc --noEmit`).
- `bun run build` — passed (`vite build`, 71 modules transformed).
- `uv run ruff check backend tests scripts` — passed.
- `uv run pytest -x` — blocked at the first PostgreSQL-backed fixture because
  `UMS_TEST_DATABASE_URL` was not configured; 122 tests passed before that
  environment gate. This is not a product-test pass. Re-run `uv run pytest -q`
  with the required disposable PostgreSQL test URL.
- `git diff --check` — passed after the final documentation edit.

## Risk, rollback, and next work

- Fail-closed scope behavior deliberately trades availability for preventing
  an unauthorized global finance read when scope storage is unavailable.
- Removed panels remain absent until source-backed API contracts exist; bringing
  back static snapshots would regress the truthfulness requirement.
- The rolling month option set is a module-load snapshot. A tab held across a
  calendar-month boundary needs a reload to receive the new window.
- Roll back by reverting this branch's frontend integration commits newest to
  oldest and re-running the frontend gates. No database rollback, reset, seed,
  or backfill is required.
- Next: complete independent review, re-run the full Python suite with the
  disposable PostgreSQL test URL, and schedule excluded router/query/design
  work only under separate contracts.

`No migration/backfill required.` The branch changes no ORM model, table,
column, constraint, index, enum, migration, repository query, or backend API.
