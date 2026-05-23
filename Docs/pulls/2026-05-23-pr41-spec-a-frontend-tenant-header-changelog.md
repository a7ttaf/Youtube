# PR #41 — Spec A Frontend `X-UMS-Tenant` Header Foundation — Changelog

**Date:** 2026-05-23
**PR:** https://github.com/XGenerationy/Youtube/pull/41
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

- `Docs/pulls/2026-05-23-pr41-spec-a-frontend-tenant-header-report.md`
  (this PR's report).
- `Docs/pulls/2026-05-23-pr41-spec-a-frontend-tenant-header-changelog.md`
  (this file).
- `Docs/pulls/2026-05-23-pr41-spec-a-frontend-tenant-header-handoff.md`

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
  `✅ PR #41 — Spec A frontend X-UMS-Tenant header foundation …`.
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

---

## Review-fix follow-up (Codex P1 / P2 / P3 + CodeRabbit `Safe Public Contracts`)

The first push hit the unresolved Codex and CodeRabbit threads listed below.
This follow-up resolves them in a single commit without re-scoping the PR.

### P1 — Trusted-gateway token no longer leakable via `VITE_*`

- `frontend/vite.config.ts`
  - Removed the `VITE_DEV_GATEWAY_TOKEN` fallback. Vite injects every
    `VITE_*` variable into the client bundle through `import.meta.env`,
    so a developer following the previous variable-name convention could
    have unintentionally embedded a real trusted-gateway secret in the
    browser. The token now reads from `UMS_TRUSTED_GATEWAY_TOKEN` only —
    a server-only name that already governed the backend.
  - Updated the startup-warning text and added a `Purpose / Standards /
    Blast Radius` block documenting the secret-handling rule.
- `.env.example`, `README.md`
  - Added the new `VITE_DEV_*` variables (backend URL, user id, email,
    role, scope type) plus an explicit “never alias the token under
    `VITE_*`” note. Resolves the CodeRabbit `Safe Public Contracts` ❌
    blocker.

### P1 — Vite dev proxy injects the full trusted-principal header set

- `frontend/vite.config.ts`
  - The proxy now injects `X-User-Email`, `X-Role`, and `X-Scope-Type`
    alongside `X-User-ID` and the gateway token. Without these,
    `current_principal_from_headers` returned 401 in the default
    `UMS_AUTHZ_SOURCE=headers` mode and `/tenants/me` could never
    bootstrap from the browser during local development.

### P2 — `/tenants/me` returns a controlled 503 when no tenant middleware is installed

- `backend/ums_smart_revenue/api/tenants.py`
  - `require_current_tenant()` is now wrapped: `TenantContextMissing`
    is translated into `HTTPException(503, "Tenant resolver middleware
    is not installed")`. `create_app(database_url=None)` is a valid app
    configuration that does not install tenant middleware, and the
    previous code surfaced an unhandled 500 for that path.
- `tests/api/test_tenants_api.py`
  - Added `test_tenants_me_returns_503_when_tenant_middleware_missing`
    constructed via `create_app(database_url=None, authz_source="headers")`
    and the full principal headers. Asserts `503` + the new detail.

### P3 — AppShell skips `/tenants/me` when no role is present

- `frontend/src/components/srcc/AppShell.tsx`
  - `displayedRole` is now computed before the effect and the effect
    body exits early when no role is set, so sessions that immediately
    render `<AccessDeniedState/>` no longer issue a bootstrap fetch
    (avoids unnecessary network traffic and 401 audit noise on
    access-denied sessions).

### Validation rerun

- `python -m ruff check backend tests scripts` → clean.
- `python -m pytest -q -p no:cacheprovider --basetemp .pytest-tmp` →
  **821 passed** (was 819 before this follow-up; +2 from the new 503
  case and one regression-coverage assertion already in this PR).
- `npm --prefix frontend run test` → **28 passed** (3 + 21 + 4 across
  `TenantContext`, `client`, `AppShell`).
- `git diff --check` → clean.

---

## Follow-up after `79611268` — Codex P2: bootstrap from resolved identity

### P2 — Bootstrap tenant context from resolved identity, not a hardcoded slug

Codex flagged that `TenantProvider`'s initial state hardcoded
`tenantSlug: "ums"`, so `useApiClient()` injected `X-UMS-Tenant: ums`
on the very first `/tenants/me` request. In `UMS_AUTHZ_SOURCE=database`
mode the SQL principal loader joins `users` on `(tenant_id=ums.id,
user_id=<gateway>)`; a valid user from a non-UMS tenant returns no row
and `current_principal_from_database` raises 403, leaving the client
stuck on the bootstrap slug for every subsequent call.

- `frontend/src/contexts/TenantContext.tsx`
  - `TenantProvider` now accepts an optional `initialSlug` prop, default
    `""`. Production `main.tsx` keeps the empty default so the bootstrap
    call is intentionally tenant-agnostic; tests/storybooks that need a
    specific seed pass `initialSlug="ums"` explicitly.
- `frontend/src/lib/api/client.ts`
  - `buildHeaders()` only sets `X-UMS-Tenant` when `tenantSlug` is
    non-empty. The pre-hydration window sends no tenant header — and
    any caller-supplied `X-UMS-Tenant` is stripped — so the trusted
    gateway / dev proxy stays the sole source of truth for tenant
    identity during bootstrap.
- `frontend/vite.config.ts`
  - Dev proxy now injects `X-UMS-Tenant` from
    `VITE_DEV_GATEWAY_TENANT_SLUG` (default `ums`), mirroring the
    production reverse-proxy contract that owns tenant resolution.
- `.env.example`
  - Documents the new `VITE_DEV_GATEWAY_TENANT_SLUG` variable next to
    the existing `VITE_DEV_GATEWAY_*` dev defaults.
- `frontend/src/components/srcc/AppShell.tsx`
  - The dev-only proof tag falls back to `(resolving…)` while the slug
    is still empty so the pre-hydration label stays readable.

### Tests

- `frontend/src/contexts/__tests__/TenantContext.test.tsx`
  - Default-seed assertion updated to expect an empty slug; new test
    confirms `initialSlug` is honored when callers explicitly seed.
- `frontend/src/lib/api/__tests__/client.test.tsx`
  - Existing slug-dependent tests now wrap with
    `<TenantProvider initialSlug="ums">`. New `bootstrap (empty slug)
    behavior` describe block asserts `X-UMS-Tenant` is omitted on the
    bootstrap window, and that caller-supplied `X-UMS-Tenant` is
    stripped during that window.
- `frontend/src/components/srcc/__tests__/AppShell.test.tsx`
  - New bootstrap test renders `<TenantProvider>` (empty default), mocks
    `/tenants/me` returning `slug: "acme"`, asserts the proof tag shows
    `Acme Holdings (acme)` after hydration AND the bootstrap fetch
    carried no `X-UMS-Tenant` header.

### Validation rerun

- `python -m ruff check backend tests` → clean.
- `python -m pytest -q` → **821 passed**.
- `npm --prefix frontend run test` → **32 passed** (4 + 23 + 5 across
  `TenantContext`, `client`, `AppShell`; +4 from this follow-up).
- `git diff --check` → LF/CRLF warnings only (no whitespace errors that fail the gate).

---

## Follow-up after `02c724f` — Codex P2: strict JSON on success path

### P2 — Reject malformed JSON in 2xx success responses

Codex flagged that `parseBody` swallowed `JSON.parse` failures on the
success path, returning raw text typed as `T`. This violated the typed
contract of `useApiClient<T>`: callers received an HTML/error string
where they expected a typed JSON object, processing corrupted data as
if it were valid.

- `frontend/src/lib/api/client.ts`
  - Internal `JsonParseError` carries the malformed `rawText`.
  - `parseBody(res, { strictJson })` rejects on malformed JSON when
    `strictJson: true`; default behavior unchanged so the error path
    still preserves the raw text in `ApiError.body` (matches the prior
    CodeRabbit "preserve ApiError body" review).
  - `request<T>` calls `parseBody(res, { strictJson: true })` on the
    success path. A `JsonParseError` is rewrapped as
    `ApiError(200, rawText, url)` so consumers handle it through the
    same boundary they already use for HTTP errors; the original 2xx
    status is preserved so consumers can distinguish "server said OK
    but lied about JSON" from network 5xx.

### Tests

- `frontend/src/lib/api/__tests__/client.test.tsx`
  - New: `rejects a malformed application/json 2xx success body via
    ApiError so callers cannot process raw text as the typed T` — mocks
    a `200 + Content-Type: application/json + <html>not really
    json</html>` body, asserts `ApiError { status: 200, body:
    "<html>not really json</html>" }`.
  - Regression test for the error-path behavior (`wraps a malformed
    application/json 5xx body in ApiError with the raw text body`) is
    unchanged and still passes — the permissive default for the error
    path is preserved.

### Validation rerun

- `python -m ruff check backend tests` → clean.
- `python -m pytest -q` → **821 passed**.
- `npm --prefix frontend run test` → **33 passed** (4 + 24 + 5 across
  `TenantContext`, `client`, `AppShell`; +1 from this follow-up).
- `git diff --check` → LF/CRLF warnings only (no whitespace errors that fail the gate).

---

## Follow-up after `d9c3c75` — CodeRabbit outside-diff: clear stale tenantError on successful retry

CodeRabbit's review on `02c724f` posted an outside-diff finding on
`frontend/src/components/srcc/AppShell.tsx:335-350` noting that
`tenantError` was set on fetch failure but never cleared on a later
successful retry — leaving the proof tag stuck on the failure branch
after the user retried and `/tenants/me` returned 200.

### Fix

- `frontend/src/components/srcc/AppShell.tsx:339-348`
  - The success path now calls `setTenantError(null)` alongside
    `tenant.hydrate(payload)` so a later retry re-renders the hydrated
    success state instead of holding the prior error branch.

### Tests

- `frontend/src/components/srcc/__tests__/AppShell.test.tsx`
  - New: `clears stale tenantError on successful retry after an earlier
    failure (outside-diff CodeRabbit regression)`. Mocks fetch with
    `mockResolvedValueOnce(jsonResponse({ detail: "transient 503" }, 503))`
    then `mockResolvedValueOnce(jsonResponse(<UMS payload>, 200))`,
    waits for the 503 message in the proof tag, switches preview role
    via `fireEvent.change` to force the bootstrap effect to re-fire,
    and asserts the proof tag now shows `UMS (ums)` AND no longer
    contains the stale `503` / `transient 503` text.

### Validation rerun

- `python -m ruff check backend tests` → clean.
- `python -m pytest -q` → **821 passed**.
- `npm --prefix frontend run test` → **34 passed** (4 + 24 + 6 across
  `TenantContext`, `client`, `AppShell`; +1 from this follow-up).
- `git diff --check` → LF/CRLF warnings only (no whitespace errors that fail the gate).
