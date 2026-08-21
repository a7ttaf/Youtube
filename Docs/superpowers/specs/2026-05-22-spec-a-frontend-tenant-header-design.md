# Spec A — Frontend `X-UMS-Tenant` Header Foundation — Design Spec

**Date:** 2026-05-22
**Owner:** Director Software Architect / Operator
**Status:** Design — awaiting user approval before implementation plan
**Closes:** S2 spec Phase 5 (frontend tenant header)
**Predecessor PRs:** #36 (S2 multi-tenant stack), #38 (validation gate + agent rules), #39 (mockup catch-up)

---

## 1. Problem statement

The S2 multi-tenant stack landed in PR #36 — every operational table now
carries a `tenant_id`, the `TenantResolverMiddleware` validates an
`X-UMS-Tenant` slug on every non-bypass request, and bootstrap tenant `ums`
(id `00000000-0000-0000-0000-000000000001`) is the deterministic seed. The
frontend, however, ships zero `X-UMS-Tenant` awareness — it has no HTTP
client, no test framework, and no tenant context. In
`authz_source="database"` or any resolver-enabled runtime, any future call
from `frontend/src/components/srcc/AppShell.tsx` to a real non-bypass backend
route would be rejected with `400 "Tenant slug must not be blank"` by the
resolver; headers-mode bootstrap behavior is called out separately below.

Spec A defines the smallest end-to-end proof that the frontend can talk to a
multi-tenant backend: a tenant React context seeded with the bootstrap slug,
a thin `fetch` wrapper that injects the header on every request, a new
backend `GET /tenants/me` endpoint that returns the resolved tenant only
after gateway/principal validation succeeds, an `AppShell` mount-time call
that proves the wire works through the same-origin gateway/dev shim, and a
Vitest test framework wired into the local validation gate.

## 2. Goals

- `frontend/src/contexts/TenantContext.tsx` exposes `useTenant()` returning
  `{ tenantSlug, id, displayName, hydrate }`, seeded with `tenantSlug: "ums"`.
- `frontend/src/lib/api/client.ts` exposes `useApiClient()` returning typed
  `{ get, post, put, patch, delete }` methods that wrap `fetch()` and inject
  `X-UMS-Tenant: <tenantSlug>` such that the header **cannot be overridden**
  by caller-supplied headers.
- `backend/ums_smart_revenue/api/tenants.py` exposes `GET /tenants/me`
  returning `TenantRead { id, slug, display_name }` from the existing
  resolver-populated `TENANT_CTX` after `current_principal_from_headers`
  succeeds. In database mode, the app factory override loads the principal
  from SQL.
- `AppShell` calls `/tenants/me` on mount through the same-origin
  gateway/dev-shim path and renders a dev-only proof element showing the
  resolved tenant or the typed `ApiError`.
- Vitest, Testing Library, and jsdom land as new devDeps in the
  implementation PR; that PR updates the local validation gate to run
  `npm --prefix frontend run test` between pytest and `git diff --check`.
- The implementation PR updates the validation gate's own self-test
  (`tests/devtools/test_quality_gate.py`) to reflect the new step order.

## 3. Non-goals

- No frontend tenant switcher UI.
- No login screen, no auth integration, no real principal binding from the
  browser — the trusted-gateway headers (`X-User-ID`,
  `X-UMS-Trusted-Gateway-Token`) are **never shipped from the browser**.
  Those headers are the responsibility of the deployed gateway / dev shim
  that fronts the FastAPI app.
- No retry policy on `/tenants/me`. One attempt per page load.
- No global error boundary, toast surface, or Sentry hook. The dev-only
  proof tag is the entire failure UI for Spec A.
- No suspended-tenant (423) or archived-tenant (410) coverage in the
  endpoint tests — full resolver-status-tree coverage already lives under
  `tests/tenancy/test_resolver.py`.
- No `/api/v1` URL prefix on the new route. The existing twelve routers
  register at `/<resource>` directly (`api/channels.py:41`,
  `api/audit.py:24`, etc.); `/tenants/me` matches that convention.
- No HTTP client library (axios, tanstack-query, ky). Native `fetch` only.
- No `@vitest/ui` devDep, hence no `test:ui` script.
- No tenant-aware caching, no `If-None-Match`, no `ETag`.

## 4. Approach

**One implementation PR after this docs PR.** Backend endpoint + frontend
provider/client/tests + live AppShell call + validation gate update all land
together in that implementation PR. Splitting loses the end-to-end-wired
property that closes S2 Phase 5.

Branch: `pr/spec-a-frontend-tenant-header`. Base: `main` at the merge commit
of PR #39.

## 5. Architecture

```
Browser
  ├── <StrictMode>
  │     <TenantProvider>                                ── React context, seeded slug = "ums"
  │       <AppShell>                                    ── existing root component
  │         useApiClient().get<TenantRead>("/tenants/me") on mount (one-shot, ref-guarded)
  │         render dev-only proof element
  │       </AppShell>
  │     </TenantProvider>
  │   </StrictMode>
  │
  └── useApiClient()                                    ── thin fetch wrapper
        normalises HeadersInit via new Headers(init.headers)
        .set("X-UMS-Tenant", tenantSlug)               ── set LAST, cannot be overridden
        no Content-Type header for GET / FormData; "application/json" only when serialising JSON body
        non-2xx → typed ApiError; fetch rejection → propagated raw TypeError

[Deployed gateway / Vite dev proxy]                    ── same-origin hop; injects trusted-gateway identity headers

Backend (existing tenancy stack from PR #36)
  ├── TrustedGatewayTenantResolverMiddleware            ── validates gateway headers, delegates
  ├── TenantResolverMiddleware                          ── validates X-UMS-Tenant slug, sets TENANT_CTX
  └── GET /tenants/me                                   ── NEW — depends on principal + existing resolver
        _principal = current_principal_from_headers()
        tenant = require_current_tenant()
        return TenantRead(
          id=tenant.id,
          slug=tenant.slug,
          display_name=tenant.display_name,
        )
```

### Trust boundary

The browser is **not** trusted. The browser supplies an `X-UMS-Tenant`
selector. The resolver is the trust authority. Today the selector is
hardcoded to `"ums"` in `TenantProvider`; future specs swap that for a
login-derived value but the architecture does not change.

### Multi-mode reality

`create_app(authz_source="database")` wires `TrustedGatewayTenantResolverMiddleware`
and overrides `current_principal_from_headers` with
`current_principal_from_database` — resolver-backed, validates
`X-UMS-Tenant`, loads an enabled SQL principal for the resolved tenant, and
keeps the full error tree from `tenancy/resolver.py`. **This is the mode
Spec A's end-to-end correctness claims are scoped to.**

`create_app(database_url=..., authz_source="headers")` wires
`DefaultTenantMiddleware` when a session factory is configured — binds the
bootstrap tenant via `_bootstrap_tenant()` **without** validating the tenant
header. The `/tenants/me` route still depends on
`current_principal_from_headers`, so unauthenticated requests must fail closed
before the bootstrap tenant can be returned. Spec A documents this with one
explicit endpoint test
(`test_tenants_me_headers_mode_requires_gateway_auth`) so future readers
do not read `/tenants/me` as an unauthenticated tenant-discovery route.

## 6. Components

### 6.1 Backend — new

**`backend/ums_smart_revenue/api/tenants.py`**

```python
from typing import Annotated
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from uuid import UUID

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.tenancy.context import require_current_tenant

router = APIRouter(prefix="/tenants", tags=["tenants"])


class TenantRead(BaseModel):
    id: UUID
    slug: str
    display_name: str


# ============================================================================
# Purpose: Return the tenant resolved by TenantResolverMiddleware after the
#          gateway/principal dependency has authenticated the caller.
# Database/ORM: No SQL from this handler; current_principal_from_headers is
#               overridden to SQL principal loading in database auth mode.
# Standards: Thin route; dependency-owned auth; explicit field construction
#            because Tenant is a domain dataclass, not a Pydantic model.
# Blast Radius: Authorization dependency required; no write path,
#               no finance impact. No graph projection impact detected.
# Connections:
#   - File: backend/ums_smart_revenue/api/dependencies.py -> auth dependency
#   - File: backend/ums_smart_revenue/tenancy/resolver.py -> sets TENANT_CTX
#   - File: backend/ums_smart_revenue/tenancy/context.py -> require_current_tenant
#   - File: backend/ums_smart_revenue/app.py -> include_router wiring
# ============================================================================
@router.get("/me", response_model=TenantRead)
def get_current_tenant_endpoint(
    _principal: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
) -> TenantRead:
    tenant = require_current_tenant()
    return TenantRead(
        id=tenant.id,
        slug=tenant.slug,
        display_name=tenant.display_name,
    )
```

### 6.2 Backend — modified

**`backend/ums_smart_revenue/app.py`** — register the new router next to
the existing twelve:

```python
from ums_smart_revenue.api.tenants import router as tenants_router
# ...
app.include_router(tenants_router)
```

### 6.3 Backend — tests

**`tests/api/test_tenants_api.py`** — see Section 9 for the full case
matrix. Fixtures follow the `test_database_principals.py:21,42-45`
pattern: SQLite-backed engine, `TenantBase.metadata.create_all`, seed one
bootstrap tenant row, `create_app(database_url=..., authz_source="database")`,
`TestClient(app)`. Trusted-gateway headers come from `conftest.py`'s
`UMS_TRUSTED_GATEWAY_TOKEN` sentinel.

### 6.4 Frontend — new

**`frontend/src/contexts/TenantContext.tsx`**

```tsx
type TenantState = {
  tenantSlug: string;        // header value — seeded "ums", never null
  id: string | null;         // hydrated from /tenants/me
  displayName: string | null;
};

type TenantContextValue = TenantState & {
  hydrate: (payload: { id: string; slug: string; display_name: string }) => void;
};

const TenantContext = createContext<TenantContextValue | null>(null);

export function TenantProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<TenantState>({
    tenantSlug: "ums",
    id: null,
    displayName: null,
  });
  const hydrate = useCallback(
    (payload: { id: string; slug: string; display_name: string }) => {
      setState((s) => ({
        ...s,
        id: payload.id,
        displayName: payload.display_name,
        // tenantSlug intentionally NOT overwritten — it is the input selector
      }));
    },
    [],
  );
  const value = useMemo(() => ({ ...state, hydrate }), [state, hydrate]);
  return <TenantContext.Provider value={value}>{children}</TenantContext.Provider>;
}

export function useTenant(): TenantContextValue {
  const ctx = useContext(TenantContext);
  if (ctx === null) throw new Error("useTenant must be used within <TenantProvider>");
  return ctx;
}
```

**`frontend/src/lib/api/client.ts`**

```ts
export class ApiError extends Error {
  readonly name = "ApiError";
  constructor(
    message: string,
    public readonly status: number,
    public readonly body: unknown,
    public readonly url: string,
  ) {
    super(message);
  }
}

function resolveUrl(path: string): string {
  const raw = import.meta.env.VITE_API_BASE_URL ?? "";
  const base = raw.replace(/\/+$/, "");           // trim trailing slashes
  const normalisedPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${normalisedPath}`;              // base "" → relative URL "/tenants/me"
}

function buildHeaders(init: HeadersInit | undefined, tenantSlug: string, hasJsonBody: boolean): Headers {
  const headers = new Headers(init);              // normalises any HeadersInit shape
  if (hasJsonBody && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("X-UMS-Tenant", tenantSlug);        // set LAST — wins over caller
  return headers;
}

async function parseBody(res: Response): Promise<unknown> {
  if (res.status === 204) return undefined;
  const contentType = res.headers.get("Content-Type") ?? "";
  if (contentType.includes("application/json")) return res.json();
  return res.text();
}

export function useApiClient() {
  const { tenantSlug } = useTenant();

  return useMemo(() => {
    async function request<T>(
      method: string,
      path: string,
      init: RequestInit & { bodyIsJson?: boolean } = {},
    ): Promise<T> {
      const url = resolveUrl(path);
      const { bodyIsJson = false, ...requestInit } = init;
      const headers = buildHeaders(requestInit.headers, tenantSlug, bodyIsJson);
      const res = await fetch(url, { ...requestInit, method, headers });
      if (!res.ok) {
        const body = await parseBody(res);
        throw new ApiError(`${res.status} ${res.statusText}`, res.status, body, url);
      }
      return (await parseBody(res)) as T;
    }
    function withBody(body: unknown, init: RequestInit = {}): RequestInit & { bodyIsJson?: boolean } {
      if (body === undefined) return init;
      if (
        typeof body === "string" ||
        body instanceof FormData ||
        body instanceof URLSearchParams ||
        body instanceof Blob ||
        body instanceof ArrayBuffer ||
        ArrayBuffer.isView(body)
      ) {
        return { ...init, body: body as BodyInit, bodyIsJson: false };
      }
      return { ...init, body: JSON.stringify(body), bodyIsJson: true };
    }
    return {
      get:    <T>(path: string, init?: RequestInit)                       => request<T>("GET",    path, init),
      post:   <T>(path: string, body?: unknown, init?: RequestInit)       => request<T>("POST",   path, withBody(body, init)),
      put:    <T>(path: string, body?: unknown, init?: RequestInit)       => request<T>("PUT",    path, withBody(body, init)),
      patch:  <T>(path: string, body?: unknown, init?: RequestInit)       => request<T>("PATCH",  path, withBody(body, init)),
      delete: <T>(path: string, init?: RequestInit)                       => request<T>("DELETE", path, init),
    };
  }, [tenantSlug]);                              // stable identity per slug
}
```

**`frontend/src/lib/api/types.ts`**

```ts
export type TenantRead = {
  id: string;
  slug: string;
  display_name: string;
};
```

**`frontend/vitest.config.ts`** — jsdom env, alias `@ → src`, setup file,
`globals: true`.

**`frontend/src/test-setup.ts`** — `import "@testing-library/jest-dom/vitest";`
plus `afterEach(() => cleanup())`.

**Frontend tests** — see Section 9.

### 6.5 Frontend — modified

**`frontend/src/main.tsx`** — wrap `<AppShell />` in `<TenantProvider>`.

**`frontend/src/components/srcc/AppShell.tsx`** — add one mount-time effect:

```tsx
const tenant = useTenant();
const client = useApiClient();
const hasRequestedTenantRef = useRef(false);
const [tenantError, setTenantError] = useState<ApiError | Error | null>(null);

useEffect(() => {
  if (hasRequestedTenantRef.current || tenant.id) return;
  hasRequestedTenantRef.current = true;
  client.get<TenantRead>("/tenants/me")
    .then(tenant.hydrate)
    .catch(setTenantError);
}, [client, tenant.id, tenant.hydrate]);

// Dev-only proof element. Hidden in production. Future Spec adds real
// observability surface.
{import.meta.env.DEV && (
  <small data-testid="tenant-proof" className="...">
    {tenantError
      ? `Tenant: ${tenant.tenantSlug}; /tenants/me failed: ${tenantError.message}`
      : tenant.id
        ? `Tenant: ${tenant.displayName} (${tenant.tenantSlug}) — id ${tenant.id}`
        : `Tenant: ${tenant.tenantSlug} (loading…)`}
  </small>
)}
```

**`frontend/vite.config.ts`** — add a dev proxy for the proof route so the
browser calls the Vite origin (`/tenants/me`) instead of calling FastAPI
cross-origin. The proxy is the local dev shim: it forwards to the backend
target and injects `X-User-ID` plus `X-UMS-Trusted-Gateway-Token` from the
Node process environment. The browser never receives or sends the trusted
token directly. Production must use the deployed gateway for the same
same-origin/injected-header contract; direct cross-origin
`VITE_API_BASE_URL` is for URL-normalisation tests only unless a future CORS
contract is added explicitly. The injected `X-User-ID` must be a canonical
UUID string that maps to an enabled SQL principal with a tenant-scoped role in
the resolved tenant; malformed values return 400 and unknown or disabled
principals return 403 before `/tenants/me` executes.

**`frontend/package.json`** — add devDeps `vitest`, `@testing-library/react`,
`@testing-library/jest-dom`, `@testing-library/user-event`, `jsdom`. Add
scripts `"test": "vitest run"` and `"test:watch": "vitest"`. No `test:ui`
script (would require `@vitest/ui` devDep).

**`frontend/package-lock.json`** — regenerated by `npm install` after the
`package.json` changes in the implementation PR. **This docs-only PR must
not include the lockfile.** When the implementation PR modifies
`package.json`, commit the regenerated `package-lock.json` alongside it
(added explicitly by path, not via `git add -A`). Standing "keep
`package-lock.json` out" rule was tied to PR #38/#39 catch-up only.

**`frontend/tsconfig.json`** — extend `compilerOptions.types` to include
`vitest/globals` and `@testing-library/jest-dom` so test files type-check
under the global `expect` / `describe` API.

### 6.6 Validation gate

**`backend/ums_smart_revenue/devtools/quality_gate.py`** — add a new
`GateCommand` to `build_gate_commands()` between
`*build_test_gate_commands(python=python)` and the two
`git diff --check` commands. The gate runner is `run_gate()` (no
`run_step` / `GateError` helpers exist; failures propagate via the
existing fail-fast loop). Pseudocode for the insertion:

```python
# Resolve npm robustly on Windows where the executable is npm.cmd.
def _resolve_npm() -> str:
    npm_exe = shutil.which("npm") or shutil.which("npm.cmd")
    if npm_exe is None:
        raise RuntimeError(
            "npm not found on PATH; install Node.js to run frontend tests."
        )
    return npm_exe


def build_gate_commands(*, python: str = sys.executable) -> tuple[GateCommand, ...]:
    npm_exe = _resolve_npm()
    return (
        GateCommand(label="Ruff backend, tests, and scripts", command=(...)),
        *build_test_gate_commands(python=python),
        GateCommand(                                                # ← NEW
            label="Frontend tests (Vitest)",
            command=(npm_exe, "--prefix", "frontend", "run", "test"),
        ),
        GateCommand(label="Git diff whitespace check",
                    command=("git", "diff", "--check")),
        GateCommand(label="Git staged diff whitespace check",
                    command=("git", "diff", "--cached", "--check")),
    )
```

`scripts/run_validation_gate.py` is unchanged — it already invokes
`run_gate()` which reads from `build_gate_commands()`.

**`tests/devtools/test_quality_gate.py`** — update the existing step-order
and label assertions to reflect the new sequence: `ruff → ast-policy →
pytest → vitest → git diff --check (working tree) → git diff --check
(staged)`.

## 7. Data flow (cold-start happy path)

```
1. App boot — main.tsx
   ReactDOM.createRoot(...).render(
     <StrictMode>
       <TenantProvider>            // state = { tenantSlug: "ums", id: null, displayName: null }
         <AppShell />
       </TenantProvider>
     </StrictMode>
   )

2. AppShell first render
   const tenant = useTenant()      // { tenantSlug: "ums", id: null, ... }
   const client = useApiClient()   // useMemo-stable per tenantSlug
   useEffect(() => {
     if (hasRequestedTenantRef.current || tenant.id) return;
     hasRequestedTenantRef.current = true;
     client.get<TenantRead>("/tenants/me").then(tenant.hydrate).catch(setTenantError);
   }, [client, tenant.id, tenant.hydrate]);

3. client.get("/tenants/me")
   url = resolveUrl("/tenants/me")              // VITE_API_BASE_URL trim+normalise
   headers = new Headers(init?.headers)         // empty
   headers.set("X-UMS-Tenant", "ums")           // set LAST
   fetch(url, { method: "GET", headers })

4. Browser → same-origin gateway/dev-shim → FastAPI
   Browser ships only to same-origin /tenants/me: X-UMS-Tenant: ums
   Gateway/dev shim injects: X-User-ID, X-UMS-Trusted-Gateway-Token
   FastAPI sees all three.

5. Middleware chain (AUTHZ_SOURCE_DATABASE mode)
   TrustedGatewayTenantResolverMiddleware:
     bypass? '/tenants/me' ∉ DEFAULT_BYPASS_PATHS → proceed
     _trusted_gateway_error(scope) → None
     delegate to TenantResolverMiddleware
   TenantResolverMiddleware:
     raw = Headers(scope).getlist("X-UMS-Tenant")  → ["ums"]
     normalised = _normalise_tenant_slug("ums")    → "ums"
     tenant = SqlAlchemyTenantRepository(session).get_by_slug("ums")
              → Tenant(id=UUID_BOOTSTRAP, slug="ums", display_name="UMS", status=ACTIVE)
     _is_authorized_with_timeout(scope, "ums")     → True
     TENANT_CTX.set(tenant)
     await self.app(scope, receive, send_with_tenant_vary)

6. Route dependency
   current_principal_from_headers()
     database mode override → current_principal_from_database()
     validates the enabled SQL principal for the resolved tenant

7. Route handler GET /tenants/me
   tenant = require_current_tenant()
   return TenantRead(id=tenant.id, slug=tenant.slug, display_name=tenant.display_name)

8. Response
   200 OK
   Vary: X-UMS-Tenant                          (set by _tenant_vary_send)
   Content-Type: application/json
   {"id":"00000000-0000-0000-0000-000000000001","slug":"ums","display_name":"UMS"}

9. Client resolves
   res.ok → parseBody → typed TenantRead
   AppShell .then(tenant.hydrate)              // provider state merges id, displayName
   Re-render: dev-only proof tag shows "Tenant: UMS (ums) — id 00000000…0001"
```

**StrictMode contract:** React 19 dev StrictMode double-invokes effects.
`hasRequestedTenantRef.current = true` is set **before** the fetch
promise awaits, so the second effect pass exits at the guard regardless
of whether the first fetch has resolved. The ref is **not** reset in the
`.catch` branch — one attempt per page load is the policy.

**Headers-mode contrast (documented, not the primary path):**

```
AUTHZ_SOURCE_HEADERS mode
   DefaultTenantMiddleware:
      bypass? '/tenants/me' ∉ bypass paths → proceed
      TENANT_CTX.set(_bootstrap_tenant())    // synthesises Tenant, no DB read
      await self.app(...)
   Route dependency current_principal_from_headers():
      missing gateway/principal headers → 401
   Endpoint never returns bootstrap tenant to an unauthenticated caller.
```

## 8. Error handling

### 8.1 Backend error tree

Fully enforced by `TenantResolverMiddleware` **before** the route runs.
Scoped to cases this endpoint's tests exercise:

| Trigger | Status | Detail (verbatim) | Source |
|---|---|---|---|
| Header absent | 400 | `Tenant slug must not be blank` | `resolver.py:153`, `repository.py:89` |
| Header empty / whitespace-only | 400 | `Tenant slug must not be blank` | same |
| Header > 255 chars | 400 | `Tenant slug must be at most 255 characters` | `repository.py:91-93` |
| Header sent more than once | 400 | `X-UMS-Tenant must be provided exactly once` | `resolver.py:146-152` |
| Slug not in registry | 404 | `Tenant 'foo' not found` | `resolver.py:222-229` |
| Slug found, custom authorizer denies | 403 | `Tenant access denied` | `resolver.py:203-208` |

Out-of-scope (full resolver tree, covered by `tests/tenancy/test_resolver.py`):
SUSPENDED → 423 Locked, ARCHIVED → 410 Gone, registry timeout → 503,
registry exception → 503 (`resolver.py:171-201,436-447`).

**`Vary: X-UMS-Tenant` contract:** every response produced by
`TenantResolverMiddleware`, including resolver errors (rows 1–6 above)
and `/tenants/me` success, carries `Vary: X-UMS-Tenant` via
`_tenant_vary_send`. Trusted-gateway errors raised by
`TrustedGatewayTenantResolverMiddleware` *before* delegation use
`_send_http_exception(...)` directly and do **not** carry the tenant Vary.

**Route handler raises nothing after auth.** The dependency owns
gateway/principal failures. The handler calls `require_current_tenant()` and
serialises. If `TENANT_CTX` is somehow unset, `TenantContextMissing`
propagates as a 500 — a middleware-contract violation, not a request error,
deliberately not hidden behind a defensive try/except.

### 8.2 Frontend error tree

`useApiClient` request outcomes:

1. `fetch()` rejects (network / DNS / CORS / offline) → original
   `TypeError` / `DOMException` propagated **unwrapped**. Callers may
   `instanceof TypeError` if they need to distinguish.
2. `!res.ok` → body parsed (JSON if `Content-Type` includes
   `application/json`, else raw text), then throw
   `new ApiError(${status} ${statusText}, status, body, url)`.
3. `res.ok` + 204 → resolve `undefined`.
4. `res.ok` + JSON body → return typed `T`.

`AppShell` consumer: `.catch(setTenantError)`. Dev-only proof tag renders
one of:

- `Tenant: UMS (ums) — id 00000000…0001` (success)
- `Tenant: ums; /tenants/me failed: 503 Service Unavailable` (`ApiError`)
- `Tenant: ums; /tenants/me failed: TypeError — Failed to fetch` (network)
- nothing (production, `import.meta.env.DEV === false`)

### 8.3 Degradation policy

`/tenants/me` failure does **not** block other API calls. The interceptor
reads `tenant.tenantSlug` which is seeded `"ums"` synchronously at
provider construction — available before any fetch resolves. Subsequent
requests still ship `X-UMS-Tenant: ums` whether hydration succeeded or
not. The only thing lost on failure is the hydrated `id` / `displayName`
for the dev tag.

## 9. Testing

### 9.1 Backend (`tests/api/test_tenants_api.py`)

App-construction fixture pattern (mirrors `test_database_principals.py`):

1. SQLite engine.
2. `TenantBase.metadata.create_all(engine)`.
3. Insert one `TenantORM(id=UUID(UMS_TENANT_ID), slug="ums", display_name="UMS", status=ACTIVE)`.
4. Insert one enabled SQL principal row plus tenant-scoped role assignment
   matching `_gateway_headers(TEST_USER_ID)`. `TEST_USER_ID` must be a
   canonical UUID string because `current_trusted_gateway_identity` normalises
   it with `UUID(...)` before the SQL principal lookup.
5. `create_app(database_url=engine.url, authz_source="database")` →
   `TestClient(app)`.
6. `/tenants/me` depends on `current_principal_from_headers`; in database
   mode the app override loads the SQL principal after trusted-gateway
   validation.

Trusted-gateway header helper:

```python
def _gateway_headers(user_id: str = TEST_USER_ID) -> dict[str, str]:
    return {
        "X-User-ID": user_id,
        "X-UMS-Trusted-Gateway-Token": os.environ["UMS_TRUSTED_GATEWAY_TOKEN"],
    }
```

| # | Request | Expected |
|---|---|---|
| 1 | `GET /tenants/me` with `X-UMS-Tenant: ums` + gateway headers for seeded user | 200; body `{"id":"00000000-...0001","slug":"ums","display_name":"UMS"}`; `Vary: X-UMS-Tenant` |
| 2 | gateway headers only, no `X-UMS-Tenant` | 400 `Tenant slug must not be blank`; Vary present |
| 3 | `X-UMS-Tenant: "   "` and variant `X-UMS-Tenant: "a"*256` | 400 `Tenant slug must not be blank` / `Tenant slug must be at most 255 characters` |
| 4 | Duplicate `X-UMS-Tenant` headers — `client.build_request(method, url, headers=[("X-UMS-Tenant", "ums"), ("X-UMS-Tenant", "ums"), *_gateway_headers().items()]); client.send(request)` (fallback if `TestClient(...).get(headers={...})` rejects list-style duplicates) | 400 `X-UMS-Tenant must be provided exactly once` |
| 5 | `X-UMS-Tenant: not-a-tenant` | 404 `Tenant 'not-a-tenant' not found` |
| 6 | **Separately constructed** app instance with `TrustedGatewayTenantResolverMiddleware(..., authorize_tenant=lambda *_: False)` wired manually (stock `create_app(authz_source="database")` uses `_allow_database_auth_tenant` which always returns True) | 403 `Tenant access denied` |
| 7 | Missing `X-User-ID` or missing/invalid `X-UMS-Trusted-Gateway-Token` | 401 before tenant payload is returned |
| 8 | `test_tenants_me_headers_mode_requires_gateway_auth` — `create_app(database_url=database_url, authz_source="headers")`, request has no trusted principal/gateway headers | 401; proves default headers mode cannot leak bootstrap tenant identity unauthenticated. |
| 9 | `create_app(database_url=database_url, authz_source="headers")` with full trusted principal headers and no `X-UMS-Tenant` | 200 bootstrap tenant; documents `DefaultTenantMiddleware` fallback only after gateway auth succeeds. |

Cases 8-9 require `database_url` so `create_app()` installs
`DefaultTenantMiddleware`; without that fixture the bootstrap-tenant fallback
is not wired and the test no longer exercises the documented headers-mode path.

### 9.2 Frontend test framework

DevDeps to add: `vitest`, `@testing-library/react`, `@testing-library/jest-dom`,
`@testing-library/user-event`, `jsdom`.

`frontend/vitest.config.ts`:

```ts
import { defineConfig } from "vitest/config";
import { fileURLToPath } from "node:url";

export default defineConfig({
  test: {
    environment: "jsdom",
    setupFiles: ["src/test-setup.ts"],
    globals: true,
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
});
```

`frontend/src/test-setup.ts`:

```ts
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
afterEach(() => cleanup());
```

### 9.3 Frontend tests

**`frontend/tests/contexts/TenantContext.test.tsx`**

- Default provider state has `{ tenantSlug: "ums", id: null, displayName: null }`.
- `hydrate({ id, slug, display_name })` merges `id` and `displayName` into
  state; `tenantSlug` stays `"ums"`.
- `useTenant()` called outside `<TenantProvider>` throws a clearly-worded
  error.

**`frontend/tests/lib/api/client.test.tsx`** (mocks `globalThis.fetch`)

- Caller passes no headers → request ships `X-UMS-Tenant: ums`.
- Caller passes `{ headers: { "X-UMS-Tenant": "evil" } }` → request still
  ships `ums` (set-last wins).
- Caller passes `Headers` instance, `Array<[string,string]>`, plain object
  → all three carry `X-UMS-Tenant: ums`.
- `VITE_API_BASE_URL` unset → resolved URL is `/tenants/me` (not
  `undefined/tenants/me`).
- `VITE_API_BASE_URL = "https://api.example.com/"` → resolved URL is
  `https://api.example.com/tenants/me` (no double slash).
- GET with no body → no `Content-Type` header.
- `client.post("/x", { foo: 1 })` → body is `JSON.stringify({foo:1})`,
  `Content-Type: application/json` present.
- POST with `FormData` body → no `Content-Type` injected (browser sets
  multipart boundary).
- Response 200 + JSON → returns typed `T`.
- Response 204 → resolves `undefined`.
- Response 4xx with JSON body → throws `ApiError`; `body` is parsed JSON;
  `status`, `url` correct.
- Response 5xx with text body → throws `ApiError`; `body` is raw text.
- `fetch` rejects with `TypeError("Failed to fetch")` → propagates as
  `TypeError`, **not** wrapped as `ApiError`.

**`frontend/tests/components/srcc/AppShell.test.tsx`** (smoke)

- **Happy path (no `<StrictMode>`):** render
  `<TenantProvider><AppShell /></TenantProvider>`. Mock `fetch` →
  bootstrap payload. After `await screen.findByTestId('tenant-proof')`,
  the tag text contains `"UMS (ums)"` and the bootstrap id substring.
- **Error path (no `<StrictMode>`):** render the same tree with `fetch`
  mocked to a 503 JSON response. Tag text contains the `ApiError`
  message.
- **Re-entry guard (with `<StrictMode>`):** render
  `<StrictMode><TenantProvider><AppShell /></TenantProvider></StrictMode>`
  with a fetch mock; assert `expect(fetchMock).toHaveBeenCalledTimes(1)`.
  Asserting the call count without `<StrictMode>` would not prove the
  guard, so this test must include the `<StrictMode>` wrapper
  explicitly.

### 9.4 Validation gate update

**`backend/ums_smart_revenue/devtools/quality_gate.py`** /
**`scripts/run_validation_gate.py`** — add the vitest step:

```
order: ruff → ast-policy → pytest → vitest → git diff --check (wt) → git diff --check (staged)
```

Windows resolution: `shutil.which("npm") or shutil.which("npm.cmd")`,
raise a clear gate error if neither resolves.

**`tests/devtools/test_quality_gate.py`** — update step-order and label
assertions to match the new sequence and the new
`GateCommand.label = "Frontend tests (Vitest)"` introduced in Section 6.6.

## 10. Blast-radius statement

*No graph projection impact detected.* Neo4j was retired in PR #12; this
PR does not touch projection code.

| Concern | Impact |
|---|---|
| SQLAlchemy ORM | None — no model changes |
| Alembic migrations | None — no schema delta |
| Tenant scoping | None — `/tenants/me` reads existing resolver context, writes nothing |
| Authorization | None — no new permission, no scope change, no principal change |
| Audit log | None — the route writes no audit event (no state change to audit) |
| Finance numbers | None — no finance code touched |
| Connectors / reports / exports | None |
| Frontend regressions | Net new files only; AppShell modification adds one effect and one dev-only DOM node; no existing UI behavior altered |

`PostgreSQL still source of truth.` `No graph projection impact detected.`

## 11. Validation commands (local, pre-push)

```
# Backend gate (run from repo root):
python scripts/run_validation_gate.py

# Implementation PR target sequence (expected after code lands):
#   1. ruff check backend tests scripts
#   2. AST policy gate (no skip/xfail)
#   3. pytest --strict-config --strict-markers --basetemp .pytest-tmp
#   4. npm --prefix frontend run test            ← NEW
#   5. git diff --check (working tree)
#   6. git diff --check (staged)

# Targeted backend test re-run (faster iteration):
pytest tests/api/test_tenants_api.py -q

# Targeted frontend test re-run:
cd frontend && npm test -- src/lib/api
cd frontend && npm test -- src/contexts
```

## 12. Rollback notes

Purely additive at the backend layer. Frontend modifications are minimal
(`main.tsx`, `AppShell.tsx`, two new directories). Revert is
`git revert <merge-commit>` — restores pre-Spec-A state with no data
impact, no migration impact, no auth impact.

## 13. Open questions / future specs

- **Tenant switcher UI:** out of scope. Future Spec C (real ingestion
  connectors) or a dedicated frontend nav spec.
- **Real auth integration:** out of scope. Frontend currently ships only
  the tenant selector; trusted-gateway headers are the gateway/dev shim's
  responsibility.
- **Retry policy on `/tenants/me`:** deliberately not implemented. A
  proper retry/backoff belongs in a later spec alongside production
  observability.
- **Production observability surface:** the dev-only proof tag is Spec A's
  entire failure UI. A real `<ErrorBoundary>` + toast + Sentry hook is a
  later spec.
- **TanStack Query or SWR:** deliberately not introduced. Native `fetch`
  is sufficient for Spec A's one-call surface area. Cache-layer
  introduction is a later spec when there are multiple endpoints.

## 14. Done definition

> `PR #NN` and `2026-MM-DD-prNN-…` below are placeholders — the
> implementation PR fills them in once the PR number is known and the
> merge date is set. The spec leaves them as tokens deliberately.

- [ ] `backend/ums_smart_revenue/api/tenants.py` exists with `GET /tenants/me`.
- [ ] `backend/ums_smart_revenue/app.py` registers `tenants_router`.
- [ ] `tests/api/test_tenants_api.py` covers cases 1–9 from Section 9.1
      and all pass.
- [ ] `frontend/src/contexts/TenantContext.tsx`, `frontend/src/lib/api/client.ts`,
      `frontend/src/lib/api/types.ts` exist with the contracts in Section 6.4.
- [ ] `frontend/vitest.config.ts` and `frontend/src/test-setup.ts` exist
      and the three frontend test files from Section 9.3 pass under
      `npm --prefix frontend run test`.
- [ ] `frontend/src/main.tsx` wraps `<AppShell />` in `<TenantProvider>`.
- [ ] `frontend/src/components/srcc/AppShell.tsx` calls `/tenants/me` on
      mount with the StrictMode-safe re-entry guard and renders the
      dev-only proof tag.
- [ ] `frontend/vite.config.ts` proxies `/tenants/me` to the backend in dev
      and injects the trusted gateway token plus a canonical UUID `X-User-ID`
      for a seeded enabled SQL principal.
- [ ] `frontend/package.json` carries the new devDeps and the
      `test` / `test:watch` scripts (no `test:ui`).
- [ ] `frontend/package-lock.json` regenerated and committed.
- [ ] `frontend/tsconfig.json` extends `types` for Vitest globals.
- [ ] `scripts/run_validation_gate.py` /
      `backend/ums_smart_revenue/devtools/quality_gate.py` runs vitest
      between pytest and `git diff --check`, with Windows `npm.cmd`
      fallback.
- [ ] `tests/devtools/test_quality_gate.py` reflects the new step order
      and label.
- [ ] `Docs/01_IMPLEMENTATION_PLAN.md` and `Docs/15_DELIVERY_BACKLOG.md`
      carry the `✅ PR #NN — Spec A …` inline mark per the
      `feedback-per-pr-plan-status` rule.
- [ ] `Docs/pulls/2026-MM-DD-prNN-spec-a-frontend-tenant-header-{report,changelog,handoff}.md`
      written for the implementation PR.
- [ ] Local validation gate green after the last edit, before push.
