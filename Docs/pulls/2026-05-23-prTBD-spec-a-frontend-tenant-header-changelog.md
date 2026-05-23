# PR #<NN> — Spec A Frontend `X-UMS-Tenant` Header Foundation — Changelog

**Date:** 2026-05-23
**PR:** https://github.com/XGenerationy/Youtube/pull/<NN>
**Branch:** `pr/spec-a-frontend-tenant-header`
**Base:** `main` at `c79ab3f` (PR #40 merge commit)

---

## Added

### Backend

- `backend/ums_smart_revenue/api/tenants.py` — New router module.
  Contains `TenantRead` Pydantic response schema (`id: UUID`, `slug: str`,
  `display_name: str`) and `GET /tenants/me` handler. The handler reads
  `TENANT_CTX.get()` after `current_principal_from_headers` resolves;
  returns 200 + `TenantRead` on success. Included in `app.py` via
  `app.include_router(tenants_router)`.

### Backend tests

- `tests/api/test_tenants_api.py` — 9 test cases for the new route:
  1. Happy path: 200 + correct `TenantRead` body (slug, id, display_name).
  2. Blank `X-UMS-Tenant` header: 400.
  3. Missing `X-UMS-Tenant` header: 400.
  4. Over-255-character slug: 400.
  5. Duplicate `X-UMS-Tenant` header: 400.
  6. Unknown slug (no row): 404.
  7. 403 authorizer denial (principal has no read permission).
  8. Missing gateway token: 401.
  9. Invalid gateway token: 401.
  Also covers headers-mode auth-required (401) and headers-mode
  bootstrap shortcut (200 in `authz_source="headers"` mode).

### Frontend test framework

- `frontend/vitest.config.ts` — Vitest configuration: jsdom environment,
  `@` alias resolving to `frontend/src`, `setupFiles: ["./src/test-setup.ts"]`,
  `globals: true`.
- `frontend/src/test-setup.ts` — Test setup: imports
  `@testing-library/jest-dom/vitest` for DOM matchers; calls
  `afterEach(cleanup)` for post-test DOM cleanup.

### Frontend source

- `frontend/src/contexts/TenantContext.tsx` — `TenantProvider` component
  (initial state: `tenantSlug: "ums"`, `id: null`, `displayName: null`);
  `useTenant()` hook; `hydrate()` updater to populate `id` and
  `displayName` after `/tenants/me` resolves.
- `frontend/src/lib/api/types.ts` — `TenantRead` TypeScript type
  (`id: string`, `slug: string`, `display_name: string`).
- `frontend/src/lib/api/client.ts` — `useApiClient()` returning typed
  `{ get, post, put, patch, delete }` methods wrapping native `fetch()`.
  `ApiError` class carrying `status: number` and `body: unknown`.
  URL normalisation (strips trailing slashes). `X-UMS-Tenant` injected
  via `new Headers(init.headers).set("X-UMS-Tenant", slug)` as the last
  write so caller-supplied headers cannot override it.

### Frontend tests

- `frontend/src/contexts/__tests__/TenantContext.test.tsx` — Renders
  `TenantProvider` and asserts initial slug; calls `hydrate()` and verifies
  context update; asserts `useTenant()` throws when called outside a provider.
- `frontend/src/lib/api/__tests__/client.test.ts` — Header-injection
  correctness (caller cannot override `X-UMS-Tenant`); `ApiError` shape
  (status + body); URL normalisation (trailing slash stripped); method
  dispatch (`GET`, `POST`, `PUT`, `PATCH`, `DELETE`).
- `frontend/src/components/srcc/__tests__/AppShell.test.tsx` — Happy-path
  smoke (proof tag shows resolved slug); error-path smoke (proof tag shows
  `ApiError` message); StrictMode guard (only one `/tenants/me` fetch across
  two mounts).

### Planning docs (Phase 7)

- `Docs/superpowers/plans/2026-05-22-spec-a-frontend-tenant-header.md` —
  Full implementation plan (committed in Phase 0 of this PR; 16 tasks
  across Phases 0–7).

### Per-PR documentation

- `Docs/pulls/2026-05-23-prTBD-spec-a-frontend-tenant-header-report.md`
  (this PR's report — rename `prTBD` → `pr<NN>` after PR open).
- `Docs/pulls/2026-05-23-prTBD-spec-a-frontend-tenant-header-changelog.md`
  (this file — rename similarly).
- `Docs/pulls/2026-05-23-prTBD-spec-a-frontend-tenant-header-handoff.md`
  (rename similarly).

---

## Changed

### Backend app factory

- `backend/ums_smart_revenue/app.py` — Added `from ...api.tenants import
  tenants_router` import and `app.include_router(tenants_router)` call.
  No other behavior change.

### Validation gate

- `backend/ums_smart_revenue/devtools/quality_gate.py` — Added new
  `GateCommand` entry `Frontend tests (Vitest)` running
  `npm --prefix frontend run test -- --run` between the full pytest step and
  the first `git diff --check` step. Gate now runs 6 steps total (was 5).

### Validation gate test

- `tests/devtools/test_quality_gate.py` — Updated the step-order / step-label
  assertions to reflect the new 6-step sequence (Vitest step inserted at
  position 4, before the two diff-check steps).

### Frontend source files

- `frontend/src/main.tsx` — Wrapped `<AppShell />` in
  `<TenantProvider>` (imported from `./contexts/TenantContext`). Minimal
  surgical edit; no other behavior change.
- `frontend/src/components/srcc/AppShell.tsx` — Added mount-time
  `useApiClient().get<TenantRead>("/tenants/me")` call guarded by a
  `useRef` to prevent double-invocation in React StrictMode. Renders a
  `data-testid="tenant-proof"` `<div>` in dev mode (`import.meta.env.DEV`)
  showing the resolved slug or `ApiError` message.

### Frontend configuration

- `frontend/vite.config.ts` — Added `server.proxy` block for `/tenants/me`
  targeting `http://localhost:8000`. Proxy injects
  `X-UMS-Trusted-Gateway-Token` and `X-User-ID` from `process.env.VITE_GATEWAY_TOKEN`
  and `process.env.VITE_USER_ID` respectively so the browser never sends
  those headers directly.
- `frontend/package.json` — Added devDeps: `vitest`, `@vitest/coverage-v8`,
  `@testing-library/react`, `@testing-library/jest-dom`,
  `@testing-library/user-event`, `jsdom`, `happy-dom`. Added npm scripts:
  `"test": "vitest --run"`, `"test:watch": "vitest"`.
- `frontend/package-lock.json` — Regenerated by `npm install`. Approximately
  4 MB growth. Now in scope for this PR (lockfile became in-scope when
  `package.json` was modified).
- `frontend/tsconfig.json` — Added `"vitest/globals"` and
  `"@testing-library/jest-dom"` to `compilerOptions.types`.

### Root configuration

- `.gitignore` — Added negation rule for `frontend/src/lib/` so the newly
  created `client.ts`, `types.ts`, and their `__tests__/` directory are
  tracked by git rather than excluded by a prior wildcard.

### Planning docs (inline PR marks)

- `Docs/01_IMPLEMENTATION_PLAN.md` — One new bullet appended to the existing
  `### S0/S1 catch-up (2026-05-22)` subsection:
  `✅ PR #<NN> — Spec A frontend X-UMS-Tenant header foundation …`.
- `Docs/15_DELIVERY_BACKLOG.md` — One new bullet appended to
  `## Cross-cutting shipped`:
  `✅ Frontend tenant-header foundation: TenantContext, useApiClient, GET /tenants/me …`.

---

## Removed

- Nothing removed. The PR is purely additive except for the two surgical edits
  to `main.tsx` and `AppShell.tsx` (each adds lines; no lines deleted).

---

## Behavior changes

- **Backend:** New route `GET /tenants/me` accessible at `http://host/tenants/me`.
  No existing route signature changes. No existing middleware changes.
- **Frontend (dev only):** AppShell now makes one HTTP call on mount; renders
  a hidden-in-prod `[tenant-proof]` element.
- **Frontend (prod):** No visible behavior change — the proof element is
  `import.meta.env.DEV`-gated and the Vite build strips it.
- **Validation gate:** Now runs 6 steps (~95 s total); previously 5 steps.
  CI consumers that parse the gate output should account for the new step.
- **pytest count:** 808 → 819 (+11 cases from `test_tenants_api.py`).
- **Frontend test count:** 0 → 21 (new framework + 3 test files).

---

## Schema / data

- No Alembic migration. No DB column, index, constraint, enum, status, or
  JSON-shape change. Migration head unchanged at `20260521_0001`.
- No seed data change. The bootstrap tenant `ums` used by the new endpoint
  was already seeded by `20260516_0001_tenants_foundation.py`.

---

## Configuration / runtime

- `pyproject.toml` — unchanged.
- `alembic.ini` — unchanged.
- `Docker` / `docker-compose.yml` — unchanged.
- CI workflows — unchanged (gate invocation unchanged; extra step is internal
  to `quality_gate.py`).

---

## Pattern compatibility

- Mirrors the existing twelve routers (`api/channels.py:41`,
  `api/audit.py:24`, etc.) — registration at `/<resource>` directly,
  no `/api/v1` prefix.
- Mirrors the `TrustedGatewayTenantResolverMiddleware` pattern for principal
  resolution — depends on `current_principal_from_headers`; does not bypass
  or weaken it.
- Mirrors the `Docs/pulls/2026-05-22-pr39-*` template structure for the
  report / changelog / handoff triple.
- Mirrors the `Docs/01_IMPLEMENTATION_PLAN.md` inline-mark convention
  (`✅ PR #N`).

---

## Compatibility with origin/main

- Purely additive on top of `main` at `c79ab3f`. No file conflict.
- No backport / cherry-pick concern.
- `frontend/package-lock.json` was a working-tree standing exclusion prior
  to this PR (it appeared in `M frontend/package-lock.json` in `git status`).
  This PR brings it in scope by modifying `package.json`; the regenerated
  lockfile is committed as a deliberate addition.
