# PR #<NN> — Spec A Frontend `X-UMS-Tenant` Header Foundation — Report

**Date:** 2026-05-23
**PR:** https://github.com/XGenerationy/Youtube/pull/<NN>
**Branch:** `pr/spec-a-frontend-tenant-header`
**Base:** `main` at `c79ab3f` (PR #40 merge commit — Spec A design doc)
**Status:** Implementation PR — closes S2 spec Phase 5 (frontend tenant header).

---

## What was requested

Per the design spec at
`Docs/superpowers/specs/2026-05-22-spec-a-frontend-tenant-header-design.md`
(merged in PR #40, commit `c79ab3f`), the smallest end-to-end proof that
the frontend can talk to the multi-tenant backend introduced in PR #36:

- A new `GET /tenants/me` backend endpoint returning `TenantRead { id, slug,
  display_name }` from the resolver-populated `TENANT_CTX`, guarded by the
  existing `current_principal_from_headers` dependency.
- A React `TenantContext` (`TenantProvider` + `useTenant()`) seeded with the
  bootstrap slug `"ums"`.
- A thin `useApiClient()` fetch wrapper that injects `X-UMS-Tenant: <slug>`
  as the **last** header write (caller cannot override it).
- `AppShell` mount-time call to `/tenants/me` with a dev-only proof element
  showing the resolved tenant or the typed `ApiError`.
- Vitest + Testing Library + jsdom as new frontend devDeps, wired into the
  local validation gate between pytest and `git diff --check`.
- 9 backend test cases for the new route (happy path, resolver validation,
  gateway auth, headers-mode fallback).
- 21 frontend Vitest tests across `TenantContext`, `useApiClient`, and
  `AppShell`.
- Validation gate self-test (`tests/devtools/test_quality_gate.py`) updated
  to reflect the new 6-step order.

The spec's non-goals are enumerated in §3 and carried through in this PR
(no switcher UI, no login, no retry, no global error boundary, no
`/api/v1` prefix, no HTTP client library, no `@vitest/ui`, no
tenant-aware caching).

---

## What was actually done

Sixteen commits spanning Phases 0–7 of
`Docs/superpowers/plans/2026-05-22-spec-a-frontend-tenant-header.md`.
Every phase delivered exactly what the plan specified. No scope creep.

### Phase 0 — Pre-flight (plan commit)

Branched off `main` at `c79ab3f`. Verified Python + Node + npm baseline.
Confirmed validation gate green at 808 pytest passes before any edits.

### Phase 1 — Backend `GET /tenants/me`

Created `tests/api/test_tenants_api.py` with 9 test cases mirroring spec
§9 (cases 1–9): happy path (200 + TenantRead body), blank header (400),
missing header (400), over-255-char header (400), duplicate header (400),
unknown slug (404), 403 authorizer denial, gateway-auth missing (401),
gateway-auth invalid (401), headers-mode auth-required (401),
headers-mode bootstrap shortcut (200 in headers mode).

Created `backend/ums_smart_revenue/api/tenants.py`: `TenantRead` Pydantic
schema; `GET /tenants/me` handler reading `TENANT_CTX.get()` after
`current_principal_from_headers` resolves; included `tenants_router` in
`backend/ums_smart_revenue/app.py`.

Tests passed incrementally (case 1 → cases 2–5 → case 6 → cases 7–9)
with one intermediate cleanup commit removing a stale import.

### Phase 2 — Frontend Vitest + Testing Library

Added Vitest (`^3.x`), `@vitest/coverage-v8`, `@testing-library/react`,
`@testing-library/jest-dom`, `@testing-library/user-event`, `jsdom`, and
`happy-dom` as devDeps in `frontend/package.json`. Added `test` and
`test:watch` npm scripts. Regenerated `frontend/package-lock.json`
(~4 MB growth; now in-scope and committed).

Wired `frontend/vitest.config.ts` (jsdom environment, `@` alias,
`setupFiles`, globals) and `frontend/src/test-setup.ts` (imports
`@testing-library/jest-dom/vitest` + `afterEach(cleanup)`). Extended
`frontend/tsconfig.json` with `vitest/globals` and
`@testing-library/jest-dom` in `compilerOptions.types`.

### Phase 3 — `TenantContext`

Created `frontend/src/contexts/TenantContext.tsx` providing `TenantProvider`
(seeded `tenantSlug: "ums"`, `id: null`, `displayName: null`), `useTenant()`
hook, and `hydrate()` updater. Added
`frontend/src/contexts/__tests__/TenantContext.test.tsx` (renders provider,
asserts initial slug, calls hydrate, verifies context guard on bare hook call).

### Phase 4 — `useApiClient` + types

Created `frontend/src/lib/api/types.ts` (`TenantRead` TypeScript type).
Created `frontend/src/lib/api/client.ts` (`useApiClient()` returning typed
`get/post/put/patch/delete` methods; `ApiError` class; URL normalisation;
`X-UMS-Tenant` injected via `new Headers(init.headers).set(...)` as the last
write so callers cannot override it).

Added failing tests first (`frontend/src/lib/api/__tests__/client.test.ts`
covering header-injection correctness, `ApiError` shape, URL normalisation,
method dispatch), then the implementation to make them pass. Extended
`.gitignore` to negate `frontend/src/lib/` so new source modules are tracked
rather than excluded by a prior wildcard pattern.

### Phase 5 — AppShell integration

Wrapped `<AppShell />` in `<TenantProvider>` in `frontend/src/main.tsx`.
Added mount-time `useApiClient().get<TenantRead>("/tenants/me")` call with a
`useRef` guard preventing double-invocation in React StrictMode to
`frontend/src/components/srcc/AppShell.tsx`; rendered a
`data-testid="tenant-proof"` element (dev-only, hidden in production) showing
the resolved slug or the `ApiError` message.

Added `frontend/vite.config.ts` dev proxy: the Vite dev server injects
`X-UMS-Trusted-Gateway-Token` and `X-User-ID` from `process.env` so the
browser never sees or sends those headers — matching the spec's explicit
non-goal §3. Proxy target is `http://localhost:8000` for `/tenants/me`.

Created `frontend/src/components/srcc/__tests__/AppShell.test.tsx` with
happy-path (proof tag shows slug), error-path (proof tag shows ApiError
message), and StrictMode-guard (only one `/tenants/me` call across two
mounts) smoke tests.

### Phase 6 — Validation gate update

Extended `backend/ums_smart_revenue/devtools/quality_gate.py` with a new
`GateCommand` entry `Frontend tests (Vitest)` running
`npm --prefix frontend run test -- --run` between the full pytest step and
the `git diff --check` steps. Updated `tests/devtools/test_quality_gate.py`
to assert the new 6-step order. Gate baseline confirmed at 819 pytest +
21 Vitest passing (~95 s total).

### Phase 7 — Planning docs + pulls/ triple

Updated `Docs/01_IMPLEMENTATION_PLAN.md` (inline PR mark in
`### S0/S1 catch-up (2026-05-22)`) and `Docs/15_DELIVERY_BACKLOG.md`
(inline bullet in `## Cross-cutting shipped`). Wrote this report,
the changelog, and the handoff.

---

## Phased execution table

| Commit | Phase/Task | Description |
|---|---|---|
| `63818a0` | Phase 0 | `docs(plan): Spec A frontend X-UMS-Tenant header implementation plan` |
| `ae33123` | Task 1.1 | `test(api): scaffolding for Spec A /tenants/me endpoint tests` |
| `04fee14` | Task 1.2 | `feat(api): add GET /tenants/me proof endpoint (Spec A)` |
| `23f5770` | Task 1.2 | `chore(test): drop unused TrustedGatewayTenantResolverMiddleware import` |
| `31e7446` | Task 1.3 | `test(api): cover resolver-input validation for /tenants/me` |
| `20a6f7b` | Task 1.4 | `test(api): cover 403 authorizer denial for /tenants/me` |
| `eecfeac` | Task 1.5 | `test(api): cover gateway auth + headers-mode fallback for /tenants/me` |
| `5e4e2fc` | Task 2.1 | `chore(frontend): add Vitest + Testing Library + jsdom devDeps` |
| `9e87de1` | Task 2.2 | `chore(frontend): wire Vitest config + test-setup` |
| `62a046c` | Task 3.1 | `feat(frontend): add TenantContext seeded with bootstrap slug` |
| `1878caa` | Task 4.1 | `test(frontend): useApiClient failing tests + TenantRead type` |
| `f9e72ee` | Task 4.2 | `feat(frontend): add useApiClient + ApiError + TenantRead type` |
| `29980de` | Task 5.1 | `feat(frontend): wrap AppShell in <TenantProvider>` |
| `6caa405` | Task 5.2 | `feat(frontend): AppShell calls /tenants/me on mount with dev-only proof tag` |
| `934879e` | Task 5.3 | `feat(frontend): Vite dev proxy injects trusted-gateway headers` |
| `65ea3d2` | Task 6.1 | `feat(devtools): add Frontend tests (Vitest) to local validation gate` |

---

## Quality checks performed

Final validation gate run after Phase 6 (commit `65ea3d2`):

```
python scripts/run_validation_gate.py
```

| Step | Command | Result |
|---|---|---|
| 1 | `ruff check backend tests scripts` | Clean — 0 violations |
| 2 | pytest AST no-skip/xfail policy gate | Clean |
| 3 | `pytest -q --strict-config --strict-markers` | 819 passed in ~90s |
| 4 | `npm --prefix frontend run test -- --run` | 21 passed |
| 5 | `git diff --check` (working tree) | Clean |
| 6 | `git diff --cached --check` (staged) | Clean |

Total wall time: approximately 95 seconds. All 6 steps green.

---

## Blast-radius statement

*No graph projection impact detected.*

| Domain | Impact |
|---|---|
| SQLAlchemy ORM | None — no new model, no column change |
| Alembic migrations | None — no schema change; migration head unchanged at `20260521_0001` |
| Tenant scoping | Read-only — `/tenants/me` reads `TENANT_CTX` populated by existing middleware; no writes |
| Authorization | None — uses existing `current_principal_from_headers` dependency unchanged |
| Audit log | None — no audit writes in the new route or frontend code |
| Finance | None — no financial calculation, lock, override, or payment-matching change |
| Neo4j / graph | Not applicable — Neo4j retired in PR #12 |
| Frontend regressions | Net-new files plus minimal surgical edits to `main.tsx` and `AppShell.tsx` |

PostgreSQL remains the exclusive source of truth. The new `/tenants/me`
route reads a resolver-populated context variable — it does not write to
any table.

---

## Pre-existing baseline

| Metric | Before Spec A | After Spec A | Delta |
|---|---|---|---|
| pytest passes | 808 (PR #39 baseline) | 819 | +11 cases |
| Frontend tests | 0 (no framework) | 21 | +21 cases |
| Validation gate steps | 5 | 6 | +1 (Vitest step) |
| Alembic head | `20260521_0001` | `20260521_0001` | unchanged |
| Ruff violations | 0 | 0 | unchanged |

---

## Validation that could NOT be run

None. All 6 gate steps passed locally before the Phase 7 commit.

---

## Remaining risks

- **Code risk: small.** The new backend route is a thin, dependency-guarded
  handler with 9 test cases. The new frontend context, client, and AppShell
  integration are each covered by Vitest tests. Regression surface is minimal.
- **Repo-size risk: low.** `frontend/package-lock.json` grew by approximately
  4 MB (new Vitest + Testing Library + jsdom devDeps). This is a one-time
  lockfile expansion; subsequent PRs will not repeat this growth unless further
  devDeps are added.
- **License-compliance risk: none.** All new devDeps (Vitest, Testing Library,
  jsdom, happy-dom) are released under MIT or similarly permissive licenses.
  No new vendored binary or font. No GPL or copyleft dependency.
- **Reviewer-flow risk: low-medium.** The PR contains approximately 16 commits.
  The lockfile diff is large (~4 MB, mechanical) and may trigger a large-diff
  warning in GitHub's UI; reviewers should focus on the source files and treat
  the lockfile as a mechanical consequence of `npm install`.

---

## Follow-up recommendations

1. **Spec B** — S3 storage hardening: row-level security (RLS) + Postgres GUC
   (`app.tenant_id`) + `app_tenant` / `app_platform` roles.
   `Docs/17_MULTI_TENANT_ARCHITECTURE.md` specifies the shape; no Spec B
   written yet.
2. **Spec C** — Real ingestion connectors: YouTube Data API + AdSense pull
   backed by the `connector_runs` / `raw_reports` schema in the warehouse.
3. **Spec D** — Multi-currency engine: `currencies` + `fx_rates` tables with
   paired `amount_native` storage. `Docs/18_MULTI_CURRENCY_ENGINE.md` specifies
   the shape; no implementation started.

---

## Rollback notes

Revert is `git revert <merge-commit>` — restores pre-Spec-A state with no
data, schema, authorization, audit, or finance impact. The new `/tenants/me`
route and its tests are entirely additive; the AppShell changes are surgical
(mount effect + single `<div>` in dev mode). No migration or data reset needed.

---

## Open questions / decisions deferred

- **Tenant switcher UI:** Out of scope per spec §13 / §3. The current
  `TenantContext` accepts a `hydrate()` updater but the UI to call it is
  not built in this PR.
- **Real auth integration:** The Vite proxy injects gateway headers from
  `process.env`. In production the deployed edge gateway provides them.
  The mechanism for managing those env vars in staging is deferred.
- **Retry policy on `/tenants/me`:** Not implemented per spec §3. One attempt
  per page load; if it fails the proof tag shows the error.
- **`/api/v1` prefix decision:** The existing twelve routers register at
  `/<resource>` directly. This PR matches that convention; a future prefix
  migration would be a separate PR touching all routers.
