# Frontend P1 integration handoff

Date: 2026-08-31

Branch: `codex/frontend-p1-integration-20260831`

Base: `41b4953939b39b55345d3d7a168eeaf57c8e2b90`

## Scope

This branch manually consolidates the safe, current-main portions of draft PRs
#212, #214, and #215. It preserves PR #211's rolling month behavior while:

- removing fabricated shell counts, fixed reporting copy, the workflow rail,
  inert shell controls, and seven unsupported data panels;
- containing view render failures with fixed operator copy, an allowlisted error
  category, and an opaque correlation ID while keeping root diagnostics free of
  thrown values and component stacks;
- retaining the shell-wide, tab-local write-in-flight latch outside recoverable
  view boundaries so a render crash cannot permit a duplicate write; separate
  cross-document import admission remains protected by `UnsettledImportContext`
  through Web Locks with localStorage-backed unsettled state;
- disabling recovery reload while that unabortable write remains active, then
  reconciling through a full-document reload instead of retrying the crashed
  write-capable subtree in place;
- failing closed when authorized revenue scopes are pending, empty, or failed;
- enforcing selected-month payment/bank grants for bank reconciliation and
  Smart Alerts reads;
- naming each 403 denial for its actual surface (net revenue, Smart Alerts,
  rankings, outside-CMS coverage, channel issues, month-close status/readiness,
  and export list/create) through fixed copy that does not reflect backend
  authorization detail;
- withholding month-close lock/unlock actions until their status/readiness
  inputs are trustworthy; and
- keeping official finance values server-derived. The frontend does not
  calculate an official result.

## Non-goals

- PR #229 and its `snapshotPanels` fixtures are not replayed or imported.
- Router introduction, query/cache architecture changes, and navigation-state
  redesign are excluded.
- Design-system replacement or visual retheming is excluded.
- PR #215's authenticated Blob download is excluded. Existing plain export
  anchors remain unchanged; this branch does not invent a streaming or bounded
  artifact-download architecture.
- Backend APIs, database models, migrations, seed data, and authorization
  contracts are unchanged.

## Files and behavior

- Shell and safety: `AppShell.tsx`, `ErrorBoundary.tsx`, `main.tsx`,
  `WriteInFlightContext.tsx`, `ActionStepper.tsx`, `icons.tsx`, `shared.tsx`, and
  `styles.css` own the de-mocked chrome, sanitized category/reference fallback,
  focus transfer, persistent write latch, and reconciliation-only recovery.
- Wired views: `CommandView.tsx`, `CloseView.tsx`, `ExportsView.tsx`, and
  `RegistryView.tsx` remove unsupported panels, enforce truthful
  authorization/readiness states, and show domain-correct fixed 403 detail.
- Reads and month source: `useRevenueScopes.ts`, `useSmartAlerts.ts`,
  `months.ts`, and `mock/data.ts` provide fail-closed reads, exact selected-month
  behavior, and removal of obsolete fixtures.
- Tests under `frontend/tests/` cover root/view error privacy, category/reference
  allowlisting, fallback focus, crash-during-write reload withholding and
  post-settlement recovery, authorized-scope 403/empty/pending/malformed
  responses, month-grant mismatch, close-read failures, domain-specific 403
  copy, removed mock surfaces, rolling-month behavior, and the view-owned Month
  selector's selected-month net-revenue URL.
- Planning/spec updates mark the superseded fabricated UI contract and record
  this integration without changing backend contracts.

## Validation

Final branch validation:

- `bun install --frozen-lockfile` — passed.
- `bunx vitest run tests/components/srcc/AppShell.test.tsx --reporter=dot` —
  passed, 1 file / 36 tests.
- Focused boundary command: `bunx vitest run`
  `tests/components/srcc/ErrorBoundary.test.tsx`
  `tests/components/srcc/AppShellErrorBoundary.test.tsx tests/main.test.ts`
  `--reporter=dot` — passed, 3 files / 17 tests.
- Focused domain-403 command: `bunx vitest run`
  `tests/components/srcc/views/CommandView.test.tsx`
  `tests/components/srcc/views/SmartAlertsPanel.test.tsx`
  `tests/components/srcc/views/RankingsPanel.test.tsx`
  `tests/components/srcc/views/OutsideCmsPanel.test.tsx`
  `tests/components/srcc/views/CloseView.test.tsx`
  `tests/components/srcc/views/ExportsView.test.tsx --reporter=dot` — passed,
  6 files / 68 tests.
- `bunx vitest run --reporter=dot` — passed, 47 files / 558 tests. The run
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
- Blocked follow-up: define a bounded or streamed authenticated artifact
  download contract before replacing the plain anchors. The current
  `GET /exports/{export_id}/analytics-summary.csv` path builds/returns the full
  artifact bytes without an output-size limit
  (`backend/ums_smart_revenue/api/exports.py` and
  `backend/ums_smart_revenue/reports/analytics_summary_csv.py`). Porting PR
  #215's `response.blob()` path would therefore add an uncapped SPA-memory
  buffer. This branch deliberately leaves that risk unresolved and does not
  claim authenticated direct-origin downloads are solved.
- Roll back by reverting this branch's frontend integration commits newest to
  oldest and re-running the frontend gates. No database rollback, reset, seed,
  or backfill is required.
- Next: re-run the full Python suite with the disposable PostgreSQL test URL,
  define the separate bounded/streamed artifact-download contract, and schedule
  excluded router/query/design work only under separate contracts.

`No migration/backfill required.` The branch changes no ORM model, table,
column, constraint, index, enum, migration, repository query, or backend API.
