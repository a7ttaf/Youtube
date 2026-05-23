# PR #41 — Spec A Frontend `X-UMS-Tenant` Header Foundation — Handoff

**Date:** 2026-05-23
**PR:** https://github.com/XGenerationy/Youtube/pull/41
**Branch:** `pr/spec-a-frontend-tenant-header`
**Base:** `main` at `c79ab3f` (PR #40 merge commit — Spec A design doc)
**Status at handoff:** Local validation gate green (819 pytest + 21 Vitest
in ~95 s); Phase 7 docs committed; push pending operator authorization
(Task 7.4).

---

## Scope

Spec A closes S2 spec Phase 5 (frontend tenant header). This PR lands the
smallest end-to-end proof that the frontend can talk to the multi-tenant
backend introduced in PR #36:

- New `GET /tenants/me` backend endpoint (thin handler, guarded by
  `current_principal_from_headers`).
- 9 backend test cases in `tests/api/test_tenants_api.py`.
- React `TenantContext` (`TenantProvider` + `useTenant()` + `hydrate()`),
  seeded with bootstrap slug `"ums"`.
- `useApiClient()` thin fetch wrapper — `X-UMS-Tenant` set last, caller
  cannot override.
- AppShell mount-time `/tenants/me` call + dev-only `[tenant-proof]` element.
- Vite dev proxy: injects trusted-gateway headers from Node env; browser
  never sees or sends them.
- Vitest + Testing Library + jsdom: 21 frontend tests across 3 files.
- Validation gate: extended to 6 steps (Vitest step added between pytest and
  `git diff --check`).
- Gate self-test (`tests/devtools/test_quality_gate.py`) updated for 6-step
  order.
- Planning doc inline marks (`Docs/01`, `Docs/15`) and this pulls/ triple.

---

## Non-goals

Per spec §3:

- No frontend tenant switcher UI.
- No login screen, no auth integration, no real principal binding from the
  browser. Trusted-gateway headers (`X-User-ID`,
  `X-UMS-Trusted-Gateway-Token`) are the gateway's responsibility — never
  shipped from the browser.
- No retry policy on `/tenants/me`. One attempt per page load.
- No global error boundary, toast surface, or Sentry hook.
- No suspended-tenant (423) or archived-tenant (410) coverage in the endpoint
  tests.
- No `/api/v1` URL prefix on the new route.
- No HTTP client library (axios, tanstack-query, ky). Native `fetch` only.
- No `@vitest/ui` devDep, hence no `test:ui` script.
- No tenant-aware caching, no `If-None-Match`, no `ETag`.

---

## Files changed

| Category | File | Change |
|---|---|---|
| Backend new | `backend/ums_smart_revenue/api/tenants.py` | New router + TenantRead schema + GET /tenants/me handler |
| Backend modified | `backend/ums_smart_revenue/app.py` | `include_router(tenants_router)` |
| Backend modified | `backend/ums_smart_revenue/devtools/quality_gate.py` | Added Vitest GateCommand (6th step) |
| Backend tests new | `tests/api/test_tenants_api.py` | 9 test cases |
| Backend tests modified | `tests/devtools/test_quality_gate.py` | Updated 6-step-order assertion |
| Frontend test config new | `frontend/vitest.config.ts` | Vitest config |
| Frontend test config new | `frontend/src/test-setup.ts` | jest-dom import + cleanup |
| Frontend source new | `frontend/src/contexts/TenantContext.tsx` | TenantProvider + useTenant() + hydrate() |
| Frontend source new | `frontend/src/lib/api/types.ts` | TenantRead TypeScript type |
| Frontend source new | `frontend/src/lib/api/client.ts` | useApiClient() + ApiError |
| Frontend tests new | `frontend/src/contexts/__tests__/TenantContext.test.tsx` | 5 cases |
| Frontend tests new | `frontend/src/lib/api/__tests__/client.test.ts` | 12 cases |
| Frontend tests new | `frontend/src/components/srcc/__tests__/AppShell.test.tsx` | 4 cases |
| Frontend modified | `frontend/src/main.tsx` | Wrap AppShell in TenantProvider |
| Frontend modified | `frontend/src/components/srcc/AppShell.tsx` | Mount call + proof tag |
| Frontend modified | `frontend/vite.config.ts` | Dev proxy for /tenants/me |
| Frontend modified | `frontend/package.json` | devDeps + test scripts |
| Frontend modified | `frontend/package-lock.json` | Regenerated (~4 MB growth) |
| Frontend modified | `frontend/tsconfig.json` | vitest/globals + jest-dom types |
| Root modified | `.gitignore` | Negate frontend/src/lib/ exclusion |
| Docs modified | `Docs/01_IMPLEMENTATION_PLAN.md` | Inline PR mark |
| Docs modified | `Docs/15_DELIVERY_BACKLOG.md` | Inline PR mark |
| Docs new | `Docs/superpowers/plans/2026-05-22-spec-a-frontend-tenant-header.md` | Implementation plan |
| Docs new | `Docs/pulls/2026-05-23-pr41-spec-a-frontend-tenant-header-report.md` | This PR's report |
| Docs new | `Docs/pulls/2026-05-23-pr41-spec-a-frontend-tenant-header-changelog.md` | This PR's changelog |
| Docs new | `Docs/pulls/2026-05-23-pr41-spec-a-frontend-tenant-header-handoff.md` | This file |

Total: 26 files created or modified.

---

## Files explicitly NOT in this PR

- No `backend/ums_smart_revenue/db/alembic/**` files — no migration.
- No `backend/ums_smart_revenue/tenancy/**` files — existing resolver
  and middleware are unchanged.
- No `backend/ums_smart_revenue/auth/**` files — existing principal
  dependency is unchanged.
- No `backend/ums_smart_revenue/finance/**`, `connectors/**`,
  `reports/**`, `graph/**` files — unrelated to this spec.
- No `mockups/**` files — unchanged.
- The `Docs/superpowers/specs/2026-05-22-spec-a-frontend-tenant-header-design.md`
  spec is locked source-of-truth (committed in PR #40). Not modified.
- `DESIGN.md`, `PRODUCT.md`, `docker-compose.yml`, `pyproject.toml`,
  `alembic.ini` — all unchanged.

---

## Behavior changes

- **Backend — new route:** `GET /tenants/me` is now accessible. No existing
  route is modified.
- **Frontend (dev mode):** AppShell makes one HTTP call on mount; a
  `data-testid="tenant-proof"` element appears in the DOM.
- **Frontend (production build):** No visible behavior change. The proof
  element is `import.meta.env.DEV`-gated; the Vite build strips it.
- **Validation gate:** 5 steps → 6 steps. The new Vitest step runs between
  pytest and `git diff --check`.
- **Pytest count:** 808 → 819.
- **Frontend test count:** 0 → 21.

---

## Tests run

Final local validation (`65ea3d2` HEAD, before Phase 7 docs commit):

```
python scripts/run_validation_gate.py
```

| Step | Result |
|---|---|
| ruff check | Clean — 0 violations |
| pytest AST policy | Clean |
| pytest full | 819 passed (~90 s) |
| npm test (Vitest) | 21 passed |
| git diff --check | Clean |
| git diff --cached --check | Clean |

All 6 steps green. Total wall time approximately 95 s.

After Phase 7 doc edits:

```
python scripts/run_validation_gate.py
```

Expected: same result (doc-only changes do not affect ruff, pytest, or
Vitest; `git diff --check` passes because all markdown files were written
without trailing whitespace).

---

## Failures / skipped gates

None remaining. The remote merge gate remains blocked until GitHub review
threads and checks clear.

---

## Risks

- **Code risk: low.** The new backend route is a thin handler with 9 test
  cases and no writes. The frontend additions are well-tested (21 Vitest
  cases). Regression surface limited to `main.tsx` and `AppShell.tsx`
  surgical edits.
- **Repo-size risk: low.** `frontend/package-lock.json` grew by approximately
  4 MB. One-time expansion; no binaries or fonts added.
- **License-compliance risk: none.** All new devDeps are MIT or similarly
  permissive. No vendored binary or copyleft dependency.
- **Reviewer-flow risk: low-medium.** Approximately 16 commits; lockfile diff
  is the bulk of byte count but is mechanical. Reviewers should focus on the
  source files.

---

## Rollback / operational notes

- Revert is `git revert <merge-commit>` — restores pre-Spec-A state with no
  data, schema, authorization, audit, or finance impact. The new route and
  frontend additions are purely additive.
- No migration, data reset, reseed, or irreversible-change note required.
- *No graph projection impact detected.*

---

## Next session / next PR recommendations

1. **Spec B** — S3 storage hardening: row-level security (RLS) + Postgres
   GUC (`app.tenant_id`) + `app_tenant` / `app_platform` Postgres roles.
   `Docs/17_MULTI_TENANT_ARCHITECTURE.md` specifies the shape. Spec B document
   not yet written; brainstorm → spec → plan → implement cycle.
2. **Spec C** — Real ingestion connectors: YouTube Data API + AdSense pull
   backed by `connector_runs` / `raw_reports` schema.
3. **Spec D** — Multi-currency engine: `currencies` + `fx_rates` tables.
   `Docs/18_MULTI_CURRENCY_ENGINE.md` specifies the shape.
4. **Done** — PR #41 placeholders replaced and files renamed via Task 7.5.
   All five docs updated (`Docs/01`, `Docs/15`, and the three pulls/ files).

---

## Open questions / decisions deferred

- **Tenant switcher UI:** `TenantContext.hydrate()` is ready; the UI is not
  built in this PR (spec §3 non-goal).
- **Staging gateway env vars:** The Vite proxy reads `process.env.VITE_GATEWAY_TOKEN`
  and `process.env.VITE_USER_ID`. The mechanism for managing those in a
  staging environment is deferred.
- **Retry policy on `/tenants/me`:** Not implemented per spec §3. Future PR
  can add a retry/backoff without touching the backend.
- **`/api/v1` prefix:** The twelve existing routers register without a prefix.
  A future prefix migration would be a separate PR.

---

## Validation a future maintainer can rerun

```bash
# Check out the branch:
git checkout pr/spec-a-frontend-tenant-header

# Full local validation gate (6 steps):
python scripts/run_validation_gate.py
# Expected: ruff clean, AST policy clean, 819 pytest passed,
#           21 Vitest passed, both diff checks clean.

# Backend tests only:
pytest tests/api/test_tenants_api.py -v

# Frontend tests only:
npm --prefix frontend run test -- --run

# Confirm migration head unchanged:
alembic heads
# Expected: 20260521_0001 (head)
```

Rerun target: validation gate green, pytest 819+, Vitest 21, ruff 0
violations, migration head `20260521_0001`.
