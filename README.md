# UMS Smart Revenue Control Center

> Numbers-first internal revenue control plane for YouTube channel portfolios. Built for **UMS** as tenant #1, designed to onboard **Rotana Holding** and other tenants without redeployment.

This service ingests YouTube + AdSense data, reconciles it against bank movements, applies allocation rules, and exposes every monetary value with **source + formula + confidence + audit trail**. Every export is logged. Every locked month is immutable. Every override needs an approver.

---

## At a glance

| Aspect | Value |
|---|---|
| Backend | Python 3.14 · FastAPI · SQLAlchemy 2 · Alembic |
| Frontend | Vite 8 · React 19 · TypeScript 6 (shipped) |
| Storage | PostgreSQL 18 (single source of truth) · ephemeral local files for export artifacts and default connector blobs |
| Background jobs | In-process `ThreadPoolExecutor` (bounded queue; off by default; Compose does not forward its enable flag in this snapshot) |
| Multi-tenant | Postgres Row-Level Security with `FORCE ROW LEVEL SECURITY` on 26 tenant-scoped tables (`db/rls.py`, `TENANT_SCOPED_TABLES`; shipped PR #106) |
| Currency | **USD only** on the finance path today. All math in `Decimal`. See the note below. |
| Auth modes | `headers` (dev / bootstrap) · `database` (production; SQL-backed principal) |
| License | See [LICENSE](LICENSE) |

> **Currency, plainly.** This row previously read *"AED · USD · EUR · GBP · SAR · EGP —
> extensible"*, which overstated what is built. There is a `currencies` lookup table
> and non-USD values are accepted at the edges, but the finance path is USD-only: there
> is no currency column on it, over 1,100 `*_usd` identifier occurrences in `backend/`
> alone, non-USD source rows and deductions are skipped, non-USD exports hard-fail,
> and the USD-only property is deliberately **test-locked** by
> `tests/finance/test_finance_no_fx_dependency.py:40-53`. Representing EGP end to end
> is a programme, not a configuration switch — see
> [`Docs/16_OPEN_DECISIONS.md`](Docs/16_OPEN_DECISIONS.md).

For the long-form vision, read [PRODUCT.md](PRODUCT.md) and [DESIGN.md](DESIGN.md). For the spec pack, see [Docs/](Docs/).

---

## Quickstart

> Requires Python 3.14, PostgreSQL 18, and [uv](https://docs.astral.sh/uv/) installed locally. Start PostgreSQL with your local service manager before launching the API.

```powershell
# 1) Install Python deps with uv
uv sync --extra dev --extra test --extra lint

# 2) Start PostgreSQL outside this repo.
#    Verify the database accepts connections before running migrations.

# 3) Configure environment (see below for the full env-var matrix).
#    .env is the one place the dev gateway secret should live, because the
#    dashboard's Vite dev proxy reads that same file from its own terminal.
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
#    Write a fresh secret OVER the placeholder .env.example ships, in the file
#    itself. Merely printing it would leave that public placeholder in .env —
#    the backend and the dev proxy would both keep using it and this whole step
#    would be decorative.
#    `uv run` so the generator uses the interpreter step 1 provisioned, the
#    same way steps 4 and 5 invoke alembic and uvicorn.
$fresh = uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
#    Replace the line when .env already has the key, APPEND it when it does not.
#    An in-place replace alone silently no-ops on an existing .env that never
#    carried the key (or carries it commented out), and the failure then lands
#    far from its cause. In THIS block the loader below sources the token from
#    .env, so a no-op leaves the backend unconfigured and
#    _require_trusted_gateway_token returns 503 on protected routes before it
#    ever inspects the request header — a 401 is the different, configured-but-
#    mismatched case. Writing the whole line also sidesteps the `$1`-vs-`${1}`
#    backreference parsing rule.
$line  = "UMS_TRUSTED_GATEWAY_TOKEN=$fresh"
$lines = @(Get-Content .env)
$lines = if ($lines -match '^UMS_TRUSTED_GATEWAY_TOKEN=') {
  $lines -replace '^UMS_TRUSTED_GATEWAY_TOKEN=.*', $line
} else { $lines + $line }
#    WriteAllLines writes UTF-8 with no BOM on both Windows PowerShell 5.1 and
#    PowerShell 7+. Set-Content's default encoding differs between them (ANSI on
#    5.1), and a silently re-encoded .env is one Vite's loader can mis-parse —
#    which shows up as the same 401 as a wrong token.
[System.IO.File]::WriteAllLines((Get-Item .env).FullName, [string[]]$lines)
Get-Content .env | Where-Object { $_ -notmatch '^\s*(#|$)' } | ForEach-Object {
  $name, $value = $_ -split '=', 2
  # Strip a matching pair of surrounding quotes; a quoted .env value would
  # otherwise reach the backend with the quotes still attached.
  $value = $value -replace '^"(.*)"$', '$1' -replace "^'(.*)'$", '$1'
  Set-Item -Path "env:$name" -Value $value
}
$env:PYTHONPATH = (Resolve-Path "backend").Path
$env:UMS_DATABASE_URL = "postgresql+psycopg://ums:ums@localhost:5432/ums_smart_revenue"
$env:UMS_AUTHZ_SOURCE = "headers"

# 4) Run migrations
uv run alembic upgrade head

# 5) Run the API
uv run uvicorn ums_smart_revenue.app:app --reload --host 0.0.0.0 --port 8000
```

On Linux/macOS, run step 3 in bash instead — steps 1, 2, 4, and 5 are the same
commands in either shell:

```bash
# 3) Configure environment. Same effect as the PowerShell block above: seed .env
#    from the template, then write a fresh secret OVER the placeholder
#    .env.example ships so the backend and the dev proxy agree on one value.
[ -f .env ] || cp .env.example .env
#    `uv run` so the generator uses the interpreter step 1 provisioned, the
#    same way steps 4 and 5 invoke alembic and uvicorn. A bare `python` aborts
#    this step on the many Linux distros that expose only `python3`.
fresh=$(uv run python -c "import secrets; print(secrets.token_urlsafe(32))")
#    The env var this step manages, named once so the presence test, the
#    rewrite, and the read-back below cannot drift apart.
var=UMS_TRUSTED_GATEWAY_TOKEN
#    Replace the line when .env already has the key, APPEND it when it does not —
#    an in-place replace alone silently no-ops on an existing .env that never
#    carried it, the export below would then pick up nothing, and the backend
#    would 503 on protected routes rather than reporting anything about .env.
#    `-i.bak` + `rm` is the in-place form that works on both GNU and BSD sed.
if grep -q "^$var=" .env; then
  sed -i.bak "s|^$var=.*|$var=$fresh|" .env
  rm -f .env.bak
else
  #  Add the newline first if .env's last line lacks one, or the append would
  #  land on the end of that line instead of on a line of its own.
  if [ -n "$(tail -c1 .env)" ]; then echo >> .env; fi
  echo "$var=$fresh" >> .env
fi
#    Read the value back out of .env rather than exporting $fresh directly, so
#    this shell provably holds the same value the dev proxy will read from the
#    file in its own terminal — the write above is confirmed, not assumed.
#    `tr -d '\r'` drops the CR a CRLF-saved .env would leave on the value.
#    `export "$var=..."` so the exported name comes from $var too — hardcoding
#    it here would let a future edit to $var export a name the backend does not
#    read, leaving it unconfigured with nothing in the output to say so.
export "$var=$(sed -n "s/^$var=//p" .env | head -n1 | tr -d '\r')"
export PYTHONPATH="$PWD/backend"
export UMS_DATABASE_URL="postgresql+psycopg://ums:ums@localhost:5432/ums_smart_revenue"
export UMS_AUTHZ_SOURCE=headers
```

> `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` is intentionally commented out in
> `.env.example`. Provision a real service account through the audited user APIs
> only after registering the connector credential reference; never substitute a
> public placeholder. Compose does not forward this variable, so use the explicit
> untracked override described in the environment-variable notes below.

### Run the tests

```powershell
# Full suite
uv run pytest -q -p no:cacheprovider --basetemp .pytest-tmp

# Single domain (faster feedback)
uv run pytest -q tests/finance
uv run pytest -q tests/api
```

> **Windows note:** the default `%TEMP%` directory is permission-blocked on some machines. `--basetemp .pytest-tmp` keeps temp files inside the repo and is already in `.gitignore`.

---

## Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `UMS_DATABASE_URL` | yes (prod) | none | SQLAlchemy URL for PostgreSQL. Use `postgresql+psycopg://…` (psycopg3 binary driver). Update `.env.example` to match. |
| `UMS_AUTHZ_SOURCE` | no | `headers` | `headers` for dev/bootstrap, `database` for production (loads principal + roles from SQL). |
| `UMS_TRUSTED_GATEWAY_TOKEN` | yes for protected routes | none | Shared secret asserted by the upstream identity gateway. Required for both `headers` bootstrap auth and `database` auth. Also read by `frontend/vite.config.ts` in Node to inject the dev proxy `X-UMS-Trusted-Gateway-Token` header. Keep the value in the repo-root `.env` and load it from there: the API and the dashboard normally run in separate terminals, so a value exported in one shell alone makes the two disagree and every protected route 401s. Note that `.env` is the lowest-precedence source Vite reads — `loadEnv` also picks up `.env.local`, `.env.[mode]`, and `.env.[mode].local`, then overlays the dashboard shell's own environment, in that increasing order — so clear a stale token from those rather than re-editing `.env`. **Never use a `VITE_*` alias** — any `VITE_*` env is embedded in the client bundle. |
| `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` | required for Google connector runs — **but not currently forwarded by compose**, see below | none | UUID stamped onto connector audit events. Optional at process boot so non-connector workloads can start; connector execution fails closed when unset, and malformed values fail settings load. The runtime currently validates UUID syntax only and fabricates the connector permission on the in-memory service principal; it does not prove that the UUID maps to an active SQL service account. |
| `VITE_DEV_BACKEND_URL` | no (dev) | `http://127.0.0.1:8000` | Exact backend origin for the development proxy. Loopback may use HTTP; a non-loopback origin must use HTTPS and also appear in `UMS_DEV_TRUSTED_BACKEND_ORIGINS` before it can receive the gateway token. |
| `UMS_DEV_TRUSTED_BACKEND_ORIGINS` | no (dev) | none | Comma-separated exact origins trusted to receive the dev gateway token when `VITE_DEV_BACKEND_URL` is not loopback. Every non-loopback entry must use HTTPS. Node-side only; never use a `VITE_*` name for this allowlist. |
| `VITE_DEV_GATEWAY_USER_ID` | no (dev) | `00000000-0000-0000-0000-0000000000aa` | Dev `X-User-ID` injected by the Vite proxy on tenant-scoped routes. Non-secret. |
| `VITE_DEV_GATEWAY_USER_EMAIL` | no (dev) | `dev@ums.local` | Dev `X-User-Email` injected by the Vite proxy. Required by `current_principal_from_headers` in default `headers` auth mode. Non-secret. |
| `VITE_DEV_GATEWAY_ROLE` | no (dev) | `assistant_analyst` | Dev `X-Role` injected by the Vite proxy. Non-secret. **Change this before you judge the product** — see the note below. |
| `VITE_DEV_GATEWAY_SCOPE_TYPE` | no (dev) | `global` | Dev `X-Scope-Type` injected by the Vite proxy. Non-secret. |
| `VITE_DEV_GATEWAY_SCOPE_ID` | required for non-global dev scope | none | Dev `X-Scope-ID`. Vite refuses to start if this is blank for a non-global scope, or non-blank for `global`. |

> ⚠️ **`UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` is not forwarded by
> `docker-compose.yml`.** Setting it in `.env` therefore has **no effect on the
> compose `app` service**, and connector runs there report it as unset. Do not infer
> intent from that omission: it is a deployment gap. `.env.example` keeps the
> variable commented and supplies no UUID; use only the service actor created
> through the audited operator flow.
>
> To run Google connectors under compose in the meantime, add a
> `docker-compose.override.yml` beside `docker-compose.yml` setting the variable on
> the `app` service to **your own provisioned** service-principal UUID. Verified
> against this repo's compose file with no `-f` flags: compose picks the override up
> by filename alone and the value lands in `app`'s environment. It lands **only** on
> the service the override names, so add a matching `app-dev:` block if you use the
> dev profile. Confirm either way with
> `docker compose config | Select-String SERVICE_ACTOR` — if that prints nothing,
> connector runs will refuse no matter what `.env` says. Note that
> `docker compose run -e …` is **not** a substitute — it affects a one-off
> container, not the long-running `app` service. Running the backend directly with
> `uv run uvicorn` reads the variable normally. The full operator runbook for this
> is in
> [`Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md`](Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md).

> ⚠️ **The default dev role sees almost nothing.** `assistant_analyst` holds **2 of
> the 26** permissions in `auth/permissions.py` — `analytics.view` and
> `analytics.view_confidence`. Every write action and most reads are denied before
> they reach any logic, so the UI looks like an unwired mockup out of the box. Set
> `VITE_DEV_GATEWAY_ROLE=finance_admin` (15 permissions) in the **repo-root** `.env`
> before evaluating anything. `frontend/vite.config.ts` pins `envDir` to the repo
> root — `frontend/.env` is **not** read.

**Never commit `.env` files.** Use the `.env.example` template and copy it locally.
There is no cluster secrets layer in this repository: no `deploy/` directory and no
Helm chart exist, and none ever has. If a clustered deployment is wanted, it has to be
built first.

**Frontend env-var safety:** Vite exposes every `VITE_*` variable to client code via `import.meta.env` at build time. The trusted-gateway secret therefore lives under the non-`VITE_*` `UMS_TRUSTED_GATEWAY_TOKEN` name only; the Vite dev proxy reads it in Node and never includes it in the browser bundle. The proxy starts only for the development server — never build or preview, including `vite preview --mode development` — fails fast on a blank token or incomplete scope, and refuses a non-loopback backend unless it uses HTTPS and its exact origin is explicitly trusted.

---

## Running with `docker compose`

`docker-compose.yml` is a local single-box development/smoke stack: one operator, one
box, every published port bound to `127.0.0.1`. It is not a completed beta deployment.

```powershell
docker compose --env-file .env config   # renders the stack; fails loudly on anything missing
docker compose up -d                    # postgres + redis + migrate + app
docker compose logs -f app
docker compose down                     # stop + remove containers, KEEP the data volumes
```

> ⚠️ **Application files are already ephemeral in this Compose file.** Only
> `postgres-data` and `redis-data` are declared. Export artifacts default to
> `/tmp/ums-smart-revenue-export-artifacts` inside the app container, and the default
> connector blob store resolves under the app working directory. Neither path has a
> volume, so recreating/removing the app container can discard those bytes even when
> PostgreSQL metadata still points at them. Configure and mount durable storage before
> treating generated artifacts or connector blobs as retained evidence.
>
> **`docker compose down -v` is destructive.** It additionally deletes the real named
> volumes: `postgres-data` (revenue facts, audit rows, tenants, roles, and grants) and
> `redis-data`. There is no repository backup/restore runbook in this snapshot. Do not
> use `-v` unless the database reset is intentional and recoverability is handled
> outside these docs.

> ⚠️ **`.env.example` is not yet a complete template for compose.** It predates the
> database variables, so `docker compose --env-file .env.example config` exits 1 on
> `UMS_DB_USER`. The compose file's own header — the paragraph beginning *"Until it
> does, this file is the authoritative list"* — enumerates the five variables that have
> no default. Completing the template is plan item P0.3.

> **Logging contract in this snapshot:** there is no `UMS_LOG_LEVEL`, no
> `config/logging_config.py`, and no Compose `logging:` rotation block. Uvicorn's
> normal logging is what `docker compose logs app` shows. Do not set invented
> `UMS_LOG_*` variables and assume retention or redaction changed; configure the
> process/container logging explicitly in a separate deployment change.

UMS has no login of its own: identity arrives as gateway-asserted headers, and the
Compose stack ships no gateway. The loopback binding is the only network boundary in
this stack. Do not expose it to a LAN, tunnel, or Tailscale address.

---

## Project layout

```text
backend/ums_smart_revenue/
├── app.py                FastAPI factory + router wiring + middleware
├── api/                  Routers: adsense, audit, channels, connectors, exports,
│                         finance_close, groups, reports, revenue, security, users
├── auth/                 RBAC: roles, permissions, scopes, policy, audit, principals
├── config/               Settings + pinned version baseline
├── connectors/           Connector credential storage (real clients land in Phase 2)
├── db/                   SQLAlchemy models + Alembic migrations
├── finance/              Revenue facts, reconciliation, overrides, alerts, exports
├── org/                  Channel registry + groups + access index
└── reports/              XLSX / PDF / PPTX generators

Docs/                     Design docs + security pack + Codex implementation notes
frontend/                 Vite + React SPA (TypeScript); tests in frontend/tests/
mockups/                  Static HTML mockup + QA screenshots
scripts/                  Operational CLIs (backup, restore, connector runs, seeds)
tests/                    api / auth / db / finance / org / reports / scripts
docker-compose.yml        The single-box deployment (first beta)
```

> **Correction:** this tree previously listed `deploy/helm/  Helm chart for Kubernetes
> (in progress)`. There is no such directory, and there never has been —
> `git log --all -- deploy` returns zero commits. The line was aspirational text that
> was never backed by a file. `Dockerfile` and `docker-compose.yml` are the only
> deployment assets in this repository.

---

## How the auth model works (one-paragraph version)

A `Principal` has a user id, email, role assignments, and direct permission grants. Role assignments and direct grants carry an access scope (global, company, sector, channel, finance month, or connector). Tenant isolation is enforced at the database layer via Postgres Row-Level Security with `FORCE ROW LEVEL SECURITY` on 26 tenant-scoped tables (`db/rls.py`, `TENANT_SCOPED_TABLES`; shipped PR #106). Each protected route declares the permission it needs (`require_permission(Permission.LOCK_FINANCE_MONTH)`) plus, often, a scope predicate (`can_view_channel_revenue(principal, channel_id)`). The `auth/policy.py` module is the single source of truth for that decision. Every sensitive read or write writes an `AuditLogEntry` with the actor, scope, sensitive flag, and (for writes) a non-blank reason. Production runs `UMS_AUTHZ_SOURCE=database` so headers cannot be spoofed.

> *Correction: the table count above and in "At a glance" read **25** until 2026-08-25.
> `TENANT_SCOPED_TABLES` in `backend/ums_smart_revenue/db/rls.py` holds **26** — the
> allowlist grew after PR #106 and the README did not. The name is now cited so the
> next reader can re-count rather than trust a number.*

For the full role/permission matrix, see [Docs/security/PERMISSION_MATRIX.md](Docs/security/PERMISSION_MATRIX.md).

---

## Documentation map

| Path | Purpose |
|---|---|
| [PRODUCT.md](PRODUCT.md) | Product vision |
| [DESIGN.md](DESIGN.md) | High-level design decisions |
| [Docs/01_IMPLEMENTATION_PLAN.md](Docs/01_IMPLEMENTATION_PLAN.md) | Phased build plan |
| [Docs/02_TARGET_ARCHITECTURE.md](Docs/02_TARGET_ARCHITECTURE.md) | System architecture |
| [Docs/11_ACCESS_CONTROL_SECURITY.md](Docs/11_ACCESS_CONTROL_SECURITY.md) | RBAC + audit policy |
| [Docs/12_BACKEND_API_SPEC.md](Docs/12_BACKEND_API_SPEC.md) | API contract |
| [Docs/15_DELIVERY_BACKLOG.md](Docs/15_DELIVERY_BACKLOG.md) | P0/P1/P2/P3 backlog |
| [Docs/16_OPEN_DECISIONS.md](Docs/16_OPEN_DECISIONS.md) | Unresolved questions |
| [SECURITY.md](SECURITY.md) | Reporting vulnerabilities |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Contribution flow |
| [CHANGELOG.md](CHANGELOG.md) | Notable changes |

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). The short version: branch from `main`, pass `uv run pytest`, `uv run ruff check`, `uv run mypy backend`, run `gitleaks detect --source . --redact` before pushing, run `bandit -r backend/ums_smart_revenue` and `sqlfluff lint` when those local tools are installed or declared by the touched area, get a CodeRabbit pass, and request review from a `CODEOWNERS` reviewer.

## Security

If you believe you've found a vulnerability, **please do not open a public issue**. Follow the disclosure process in [SECURITY.md](SECURITY.md).

## License

See [LICENSE](LICENSE).
