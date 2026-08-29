# UMS Smart Revenue — Dashboard (frontend)

Vite + React 19 + TypeScript + Tailwind dashboard shell, wired to the real
backend API across six screens. This is the operator runbook for seeding a demo
month, running the backend, and demoing the dashboard locally.

## Stack

- Vite 8, React 19, TypeScript 6, Tailwind 4, Vitest 3.
- The `@/` import alias maps to `src/` (see `tsconfig.json` `paths`).
- `src/vite-env.d.ts` provides the Vite ambient client types (`import.meta.env`,
  the `*.css` side-effect import). Keep it — without it `tsc --noEmit` fails.

## Quick commands

This workspace is on **bun** (`packageManager` in `package.json`, `bun.lock` is
the only lockfile). Use `bun`, not `npm`/`npx` — see the trap below.

`package.json` declares the toolchain (`engines.node`, `packageManager`) and
`ci/checks/node.sh` enforces both **before** it installs or runs anything: an
undeclared Node or a package-manager version other than the pin exits
`FAIL_INFRA` rather than producing a result the declared toolchain does not
vouch for. If the lane stops there, install the declared versions — it is
reporting the environment, not the code.

```bash
cd frontend
bun install --frozen-lockfile   # first time only
bun run dev                     # Vite dev server on http://127.0.0.1:5173 (proxy -> backend)
bun run build                   # production build (tsc is not part of build; run it separately)
bun run test                    # Vitest (run mode)
bun run typecheck               # tsc --noEmit — must exit 0
```

> **Do not use `npm install` or `npx` here.**
>
> `npm install` writes a `package-lock.json` alongside `bun.lock`, and
> `ci/checks/node.sh` refuses to run with two lockfiles present
> ("Multiple lockfiles detected", `FAIL_INFRA`).
>
> `npx <tool>` is worse because it fails *quietly*. bun writes Windows shims
> (`vitest.exe`, `vitest.bunx`) into `node_modules/.bin`, and npx does not
> recognise those names, so it walks **up out of the workspace** and runs
> whatever it finds in a parent directory. In this checkout that is a different
> major version of Vitest against a different Vite, which reports on the order
> of 160 phantom test failures that look exactly like real breakage. `bun run`
> always resolves the workspace-local binary. The CI lanes now do the same by
> invoking `node_modules/.bin/` directly.

## Test layout

Every automated test lives under `frontend/tests/`, mirroring `src/` without a
`__tests__` segment:

| Source | Test |
| --- | --- |
| `src/lib/api/useGroups.ts` | `tests/lib/api/useGroups.test.tsx` |
| `src/components/srcc/views/TraceView.tsx` | `tests/components/srcc/views/TraceView.test.tsx` |

Rules:

- Name test files `*.test.ts` / `*.test.tsx`. `.spec.*` is **not** collected.
- Import the code under test through the `@/` alias, never a relative path.
  The alias is what makes the tests movable and the layout cheap to change.
- Do not create `__tests__/` directories. That convention is retired.

The layout is declared by `test.include` in `vitest.config.ts` and enforced by
`ci/checks/test-layout.sh`, which the gate runs as a blocking lane in `quick`
(pre-commit), `full`, and `ship` (pre-push): the modes code lands through. It is
not in `debt`, the known-debt ratchet invoked by `make`, which runs only
`git-safety` and `debt`. The guard exists because `test.include` on
its own *hides* mistakes: a test file outside the glob is silently not
collected, so it passes by never running. The guard fails the build instead. It
scans all of `frontend/` outside `tests/` — not just `src/` — because a test in
any other subdirectory is just as invisible to `include`, and it also fails if
the `include` stops being live config, a commented-out one included.

Run it directly:

```bash
bash ci/checks/test-layout.sh
```

## End-to-end demo (seed → backend → dashboard)

The dashboard reads live data. To demo it you need (1) a seeded month, (2) the
backend running, (3) the dev proxy injecting trusted-gateway headers.

### 1. Pick a trusted-gateway token

The backend's header-auth path requires `UMS_TRUSTED_GATEWAY_TOKEN` to be set
and to match the token the dev proxy injects. Create `.env` with a fresh secret
using the [root README's Quickstart step 3](../README.md#quickstart) — that
snippet is the canonical copy and it **writes** the generated value over the
placeholder `.env.example` ships, rather than printing it for a manual edit that
is easy to skip. Step 3 is given twice there, once in PowerShell and once in
bash, so the Linux/macOS backend path in step 3 below has a shell-native way to
create `.env` and persist the token.

Keep that value in `.env` and let both steps below read it from there. Steps 3
and 4 run in separate terminals, so a token exported in the backend's shell alone
leaves the dashboard sending the shipped placeholder and every protected route
returns 401.

`.env` is where the value belongs, but it is not the only source Vite consults.
`vite.config.ts` calls `loadEnv(mode, REPO_ROOT, "")`, which reads these repo-root
files in **increasing** order of precedence, then overlays the dashboard shell's
own environment on top of all of them:

1. `.env`
2. `.env.local`
3. `.env.development` (the `mode`, so `.env.[mode]` in general)
4. `.env.development.local` (`.env.[mode].local`)
5. the dashboard terminal's exported environment — highest

If a protected route still 401s after following these steps, a stale
`UMS_TRUSTED_GATEWAY_TOKEN` in one of those higher-precedence sources is why;
clear it there rather than editing `.env` again.

```dotenv
# repo-root .env — copy from .env.example and fill in your values
UMS_AUTHZ_SOURCE=headers
```

`UMS_TRUSTED_GATEWAY_TOKEN` is a NON-`VITE_` name on purpose: `VITE_*` values
are baked into the browser bundle, so the secret must never gain a `VITE_` alias.
The Vite dev proxy reads it in Node and injects it server-side.

### 2. Seed one demo month

Seeds a fully-populated demo month (3 channels, an account-level deduction, a
committed allocation snapshot) — idempotent, safe to re-run.

**Which month to seed.** Every screen's month selector is derived from the clock
and defaults to the **current calendar month** (local date, not a frozen
literal). Seed that month, or the dashboard opens on one with no data. The seed
already defaults to it, so plain `--create-schema` with no `--month` is enough;
the explicit forms below just make the month visible (and are what you edit to
seed a different one):

**The Connectors screen is the write-side exception.** Its selector opens on
the **last complete calendar month**, not the current one: connector pulls
address a whole calendar month and the backend validates only the month's
format, so pulling the in-progress month would ingest a partial month as if it
were final. The current month is still in the dropdown for deliberate use.
A manually synced payment is not tied to that default at all — it files under
the month of its **payment date**, matching the automated AdSense mapping, and
the form shows that derived month next to the date field. Seeding the current
month therefore leaves the Connectors write default on the previous month;
that is intended behavior, not drift.

```bash
# From the repo root (bash). Default DB is UMS_DATABASE_URL; for a throwaway SQLite:
uv run python scripts/seed_demo_month.py \
  --database-url "sqlite+pysqlite:///./demo.db" \
  --create-schema --month "$(date +%Y-%m)"
```

```powershell
# From the repo root (PowerShell) — same seed, same locally-computed month:
uv run python scripts/seed_demo_month.py `
  --database-url "sqlite+pysqlite:///./demo.db" `
  --create-schema --month (Get-Date -Format 'yyyy-MM')
```

**Lock behavior.** By default the seed asks the production lock service to close
the month. A minimal demo month has single-source channels, so the real
lock-time readiness recheck legitimately refuses (an `INSUFFICIENT_SOURCES`
reconciliation blocker); the seed then leaves the month **OPEN**, prints the
blocker types, and still exits 0 — it never forges a LOCKED status the
production gate would reject. To force the demo LOCKED state on a disposable
database, add `--demo-lock-bypass`:

```bash
uv run python scripts/seed_demo_month.py \
  --database-url "sqlite+pysqlite:///./demo.db" \
  --create-schema --month "$(date +%Y-%m)" --demo-lock-bypass
```

```powershell
uv run python scripts/seed_demo_month.py `
  --database-url "sqlite+pysqlite:///./demo.db" `
  --create-schema --month (Get-Date -Format 'yyyy-MM') --demo-lock-bypass
```

That flip writes **no audit event** and bypasses the production readiness gate —
demo databases only. Trying the lock from the UI on this demo data therefore
demonstrates the real readiness refusal: `POST /finance-close/{m}/lock` returns
**409** with the blockers, which is exactly what the Month Close screen surfaces.

The seed prints the demo principal headers and the committed-allocation summary.
With no `--month` it targets the current calendar month, computed at run time —
the same month every screen's selector defaults to. A month that has never been
closed simply has no close record, and the Month Close screen says so
(status **OPEN**, "No close record yet") rather than reporting an error.

### 3. Run the backend

Point the backend at the same database you seeded and set the same token:

```powershell
# From the repo root (PowerShell). First load the .env from step 1 with the
# loader in the root README's Quickstart step 3 — that snippet is the single
# canonical copy (see ../README.md#quickstart), kept in one place so a future
# fix to it cannot land in only one of the two runbooks. Then point the backend
# at the database you seeded, overriding the URL that .env just set:
$env:UMS_DATABASE_URL = "sqlite+pysqlite:///./demo.db"
uv run python -m uvicorn ums_smart_revenue.app:app --app-dir backend --port 8000
```

```bash
# From the repo root (bash) — same .env, same token. Read only the token line:
# `source`-ing the file would run it as shell, and .env.example's
# postgresql+psycopg://<user>:<password>@... placeholder makes `<user>` an input
# redirection, which both errors and truncates the value.
# `tr -d '\r'` drops the CR a CRLF-saved .env leaves on the value, and the sed
# strips a matching pair of surrounding quotes. Without both, the exported token
# silently differs from what the proxy injects and every protected route 401s.
export UMS_TRUSTED_GATEWAY_TOKEN=$(
  sed -n 's/^UMS_TRUSTED_GATEWAY_TOKEN=//p' .env \
    | head -n1 | tr -d '\r' | sed -e 's/^\(["'\'']\)\(.*\)\1$/\2/'
)
# UMS_AUTHZ_SOURCE is pinned here too: the backend reads os.environ directly, so
# a `database` value already exported in this shell would otherwise win over the
# `headers` line in .env, and the seed provisions no database principal for the
# proxy's default user id — the demo would 401 on every request.
UMS_AUTHZ_SOURCE=headers \
UMS_DATABASE_URL="sqlite+pysqlite:///./demo.db" \
uv run python -m uvicorn ums_smart_revenue.app:app --app-dir backend --port 8000
```

### 4. Run the dashboard

```bash
cd frontend
bun run dev
```

The dev proxy (`vite.config.ts`) forwards every tenant-scoped route
(`/revenue`, `/finance-close`, `/exports`, `/connectors`, `/adsense`,
`/channels`, `/tenants`) to the backend and injects the full trusted-principal
header set (`X-User-ID`, `X-User-Email`, `X-Role`, `X-Scope-Type`,
`X-UMS-Trusted-Gateway-Token`, `X-UMS-Tenant`). The browser bundle never holds
the gateway secret.

### Demo headers the dev proxy injects

Defaults live in `vite.config.ts` and are overridable via repo-root `.env`
(`VITE_DEV_*`). To see finance money cells you MUST use a finance role — the
default `assistant_analyst` has no finance visibility:

```dotenv
# repo-root .env — for a finance demo
VITE_DEV_BACKEND_URL=http://127.0.0.1:8000
VITE_DEV_GATEWAY_USER_ID=00000000-0000-0000-0000-0000000000aa
VITE_DEV_GATEWAY_USER_EMAIL=dev@ums.local
VITE_DEV_GATEWAY_ROLE=finance_admin
VITE_DEV_GATEWAY_SCOPE_TYPE=global
VITE_DEV_GATEWAY_TENANT_SLUG=ums
```

`finance_admin` covers net-revenue, smart-alerts, finance-close + readiness,
explain, exports, and AdSense payments. The Connectors **credentials** list
additionally needs `MANAGE_CONNECTORS` (carried by `super_owner` /
`connector_admin`), so under `finance_admin` that one table reports a no-permission
state — expected, and shown honestly by the screen.

The Connectors **job-sync** and **AdSense-sync** actions need `RUN_CONNECTOR_JOBS`.
No in-shell preview role maps to a backend role that holds it (`finance_admin`
would 403), so those two controls render **disabled for every preview role** with
the hint "Requires a connector-operations role." — the read surface still loads;
only the write actions are gated.

There is also an in-shell role preview (top-left role switcher) when running in
`DEV`, so you can flip between `finance` / `assistant` / `company` to demo the
permission gating without restarting. This switcher is **presentation-only** — it
changes the UI's permission modelling but not backend authorization, which always
comes from the fixed dev-gateway role (`VITE_DEV_GATEWAY_ROLE`, injected
server-side at proxy start); the switcher renders a hint saying exactly this.

## Production session hydration

On bootstrap the SPA calls `GET /session/me` (`useSessionBootstrap` →
`SessionContext`) to hydrate the **authenticated principal's capabilities** —
camelCase booleans (`canViewRevenue`, `canExportRevenue`, `canRunConnectorJobs`,
…) the backend derives from the principal's permissions at **global scope**. The
AppShell renders the dashboard gated by those capabilities, so a **production
build** now serves the real surface for an authorized principal instead of a
permanent access-denied screen — **no preview role is needed**.

- The shell shows a loading state until `/session/me` settles, then renders
  the dashboard gated by capabilities.
- **Failed hydration fails closed**: any rejection (401 / 403 / network) or a
  `disabled` principal renders `AccessDenied` — the dashboard is never shown
  before the principal is known, and a transient error never leaves a stale
  principal's capabilities live.
- **Connector controls require `canRunConnectorJobs`**: the job-sync and
  AdSense-sync write actions stay disabled unless the principal's capability is
  true.
- The in-shell **role preview** (DEV only) is **presentation-only** — it relabels
  the UI's permission modelling but does not change the hydrated capabilities,
  which are always backend-derived from `/session/me`.

The dev demo flow above still applies: `/session/me` rides the same
trusted-gateway/dev-proxy headers (the proxy now also forwards `/session`), so
the capabilities the SPA hydrates reflect the dev-gateway role
(`VITE_DEV_GATEWAY_ROLE`) injected server-side. Capabilities are **global-scope**
only.

## Which screen shows what

| Screen (nav)        | Backend endpoint(s)                                              |
| ------------------- | --------------------------------------------------------------- |
| Command Center      | `GET /revenue/months/{m}/net-revenue` (status strip, channel table, explain rail) + `GET /revenue/months/{m}/smart-alerts` (problem panel) |
| Month Close         | `GET /finance-close/{m}` + `GET /finance-close/{m}/readiness`; `POST /finance-close/{m}/lock` and `/unlock` (inline **Reason (required, audited)** input + arm/confirm two-step — no browser prompts) |
| Trace / Explain     | `POST /revenue/channels/{ch}/months/{m}/explain?metric=...` (channel list reused from net-revenue) |
| Exports             | `GET /exports` (job list) + `POST /exports` (request); QUEUED jobs show a **Generate** link that triggers on-demand generation via the `GET` download route, COMPLETED jobs re-serve the persisted artifact over that same route. `ANALYTICS_SUMMARY_CSV` has no binary route (computed inline, no Generate/download link) |
| Connectors          | `GET /connectors/credentials` + `GET /adsense/payments`; `POST /connectors/jobs` and `POST /adsense/sync-payments`. The job-sync and AdSense-sync controls render **disabled for every preview role** (hint: "Requires a connector-operations role.") and use an inline reason field — no browser prompts |
| Audit               | `GET /audit/events` (cursor-paginated timeline; server-driven sensitive-payload redaction; fail-closed — a non-audit viewer sees a restricted placeholder and fires no fetch). **First page only** (no Load More via `next_cursor` yet). Summary tiles, coverage panel, and the severity-filter / Download controls stay static/disabled placeholders |
| Registry            | Mock data only (not wired to the API yet — clearly labelled in-app) |

Money values are backend decimal **strings** and are formatted for display only
(no float math); finance cells are permission-gated to a `Restricted` sentinel
when the viewer's role can't see money.

## One-command end-to-end smoke

To prove a seeded month flows through every wired endpoint without a live
server (in-process FastAPI `TestClient` behind the real trusted-gateway header
auth), run from the repo root:

```bash
uv run python scripts/smoke_mvp.py                        # current calendar month
uv run python scripts/smoke_mvp.py --month "$(date +%Y-%m)"
```

```powershell
uv run python scripts/smoke_mvp.py                        # current calendar month
uv run python scripts/smoke_mvp.py --month (Get-Date -Format 'yyyy-MM')
```

It seeds a throwaway SQLite db, asserts HTTP 200 + the key contract fields for
each MVP screen's endpoint, prints a PASS/FAIL table, exits non-zero on any
failure, and cleans up the db.
