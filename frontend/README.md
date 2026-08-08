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

```bash
cd frontend
npm install        # first time only
npm run dev        # Vite dev server on http://127.0.0.1:5173 (proxy -> backend)
npm run build      # production build (tsc is not part of build; run it separately)
npm run test       # Vitest (run mode)
npx tsc --noEmit   # type-check only — must exit 0
```

## End-to-end demo (seed → backend → dashboard)

The dashboard reads live data. To demo it you need (1) a seeded month, (2) the
backend running, (3) the dev proxy injecting trusted-gateway headers.

### 1. Pick a trusted-gateway token

The backend's header-auth path requires `UMS_TRUSTED_GATEWAY_TOKEN` to be set
and to match the token the dev proxy injects. Copy `.env.example` to `.env` in
the repo root and set a local value:

```dotenv
# repo-root .env — copy from .env.example and fill in your values
UMS_AUTHZ_SOURCE=headers
```

`UMS_TRUSTED_GATEWAY_TOKEN` is a NON-`VITE_` name on purpose: `VITE_*` values
are baked into the browser bundle, so the secret must never gain a `VITE_` alias.
The Vite dev proxy reads it in Node and injects it server-side.

### 2. Seed one demo month

Seeds a fully-populated demo month (3 channels, an account-level deduction, a
committed allocation snapshot) — idempotent, safe to re-run:

```bash
# From the repo root. Default DB is UMS_DATABASE_URL; for a throwaway SQLite:
python scripts/seed_demo_month.py \
  --database-url "sqlite+pysqlite:///./demo.db" \
  --create-schema --month 2026-03
```

**Lock behavior.** By default the seed asks the production lock service to close
the month. A minimal demo month has single-source channels, so the real
lock-time readiness recheck legitimately refuses (an `INSUFFICIENT_SOURCES`
reconciliation blocker); the seed then leaves the month **OPEN**, prints the
blocker types, and still exits 0 — it never forges a LOCKED status the
production gate would reject. To force the demo LOCKED state on a disposable
database, add `--demo-lock-bypass`:

```bash
python scripts/seed_demo_month.py \
  --database-url "sqlite+pysqlite:///./demo.db" \
  --create-schema --month 2026-03 --demo-lock-bypass
```

That flip writes **no audit event** and bypasses the production readiness gate —
demo databases only. Trying the lock from the UI on this demo data therefore
demonstrates the real readiness refusal: `POST /finance-close/{m}/lock` returns
**409** with the blockers, which is exactly what the Month Close screen surfaces.

The seed prints the demo principal headers and the committed-allocation summary.
The default demo month is `2026-03` (the month every screen's selector defaults
to).

### 3. Run the backend

Point the backend at the same database you seeded and set the same token:

```bash
# From the repo root (PowerShell)
$env:UMS_DATABASE_URL = "sqlite+pysqlite:///./demo.db"
# Generate a random token once; the dev proxy (repo-root .env) must send the same value.
$env:UMS_TRUSTED_GATEWAY_TOKEN = [guid]::NewGuid().ToString()
python -m uvicorn ums_smart_revenue.app:app --app-dir backend --port 8000
```

```bash
# From the repo root (bash)
# Generate a random token once; the dev proxy (repo-root .env) must send the same value.
export UMS_TRUSTED_GATEWAY_TOKEN=$(openssl rand -hex 16)
UMS_DATABASE_URL="sqlite+pysqlite:///./demo.db" \
python -m uvicorn ums_smart_revenue.app:app --app-dir backend --port 8000
```

### 4. Run the dashboard

```bash
cd frontend
npm run dev
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
python scripts/smoke_mvp.py            # default month 2026-03
python scripts/smoke_mvp.py --month 2026-03
```

It seeds a throwaway SQLite db, asserts HTTP 200 + the key contract fields for
each MVP screen's endpoint, prints a PASS/FAIL table, exits non-zero on any
failure, and cleans up the db.
