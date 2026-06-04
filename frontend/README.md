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
# repo-root .env
UMS_TRUSTED_GATEWAY_TOKEN=local-dev-token
UMS_AUTHZ_SOURCE=headers
```

`UMS_TRUSTED_GATEWAY_TOKEN` is a NON-`VITE_` name on purpose: `VITE_*` values
are baked into the browser bundle, so the secret must never gain a `VITE_` alias.
The Vite dev proxy reads it in Node and injects it server-side.

### 2. Seed one demo month

Seeds a fully-populated, **LOCKED** demo month (3 channels, an account-level
deduction, a committed allocation snapshot) — idempotent, safe to re-run:

```bash
# From the repo root. Default DB is UMS_DATABASE_URL; for a throwaway SQLite:
python scripts/seed_demo_month.py \
  --database-url "sqlite+pysqlite:///./demo.db" \
  --create-schema --month 2026-03
```

The seed prints the demo principal headers and the committed-allocation summary.
The default demo month is `2026-03` (the month every screen's selector defaults
to).

### 3. Run the backend

Point the backend at the same database you seeded and set the same token:

```bash
# From the repo root (PowerShell)
$env:UMS_DATABASE_URL = "sqlite+pysqlite:///./demo.db"
$env:UMS_TRUSTED_GATEWAY_TOKEN = "local-dev-token"
python -m uvicorn ums_smart_revenue.app:app --app-dir backend --port 8000
```

```bash
# From the repo root (bash)
UMS_DATABASE_URL="sqlite+pysqlite:///./demo.db" \
UMS_TRUSTED_GATEWAY_TOKEN="local-dev-token" \
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

There is also an in-shell role preview (top-left role switcher) when running in
`DEV`, so you can flip between `finance` / `assistant` / `company` to demo the
permission gating without restarting.

## Which screen shows what

| Screen (nav)        | Backend endpoint(s)                                              |
| ------------------- | --------------------------------------------------------------- |
| Command Center      | `GET /revenue/months/{m}/net-revenue` (status strip, channel table, explain rail) + `GET /revenue/months/{m}/smart-alerts` (problem panel) |
| Month Close         | `GET /finance-close/{m}` + `GET /finance-close/{m}/readiness`; `POST /finance-close/{m}/lock` and `/unlock` |
| Trace / Explain     | `POST /revenue/channels/{ch}/months/{m}/explain?metric=...` (channel list reused from net-revenue) |
| Exports             | `GET /exports` (job list) + `POST /exports` (request); COMPLETED jobs download the binary over the proxied path |
| Connectors          | `GET /connectors/credentials` + `GET /adsense/payments`; `POST /connectors/jobs` and `POST /adsense/sync-payments` |
| Registry / Audit    | Mock data only (not wired to the API yet — clearly labelled in-app) |

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
