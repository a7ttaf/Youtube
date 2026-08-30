# UMS Smart Revenue Control Center

> Numbers-first internal revenue control plane for YouTube channel portfolios. Built for **UMS** as tenant #1, designed to onboard **Rotana Holding** and other tenants without redeployment.

This service ingests YouTube + AdSense data, reconciles it against bank movements, applies allocation rules, and exposes every monetary value with **source + formula + confidence + audit trail**. Every export is logged. Every locked month is immutable. Every override needs an approver.

---

## At a glance

| Aspect | Value |
|---|---|
| Backend | Python 3.14 · FastAPI · SQLAlchemy 2 · Alembic |
| Frontend | Vite 8 · React 19 · TypeScript 6 (shipped) |
| Storage | PostgreSQL 18 (single source of truth) · ephemeral local file store for export artifacts |
| Background jobs | In-process `ThreadPoolExecutor` (bounded queue; off by default; compose does not forward the executor setting on this branch) |
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
> is a 3–6 week programme, not a configuration switch — see
> [`Docs/16_OPEN_DECISIONS.md`](Docs/16_OPEN_DECISIONS.md). The beta planning
> documents are maintained outside this branch.

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
# Keep the direct local API on loopback. Expose a non-loopback address only
# behind a real trusted gateway and TLS termination.
uv run uvicorn ums_smart_revenue.app:app --reload --host 127.0.0.1 --port 8000
```

The quickstart intentionally binds the direct API to loopback. A non-loopback
deployment requires a real trusted gateway in front of UMS and TLS; the local
Vite proxy likewise accepts only loopback targets over HTTP, or explicitly
allowlisted HTTPS origins.

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

> ⚠️ **One thing `cp .env.example .env` gets wrong today.** The template ships
> `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` **uncommented**, set to the public
> placeholder `00000000-0000-0000-0000-0000000000bb`. The loader blocks above export
> every non-comment line, so a local run picks that value up — and the runtime check
> refuses only when the variable is *unset*, accepting any syntactically valid UUID.
> The result is a connector audit trail attributed to an id published in a public
> template rather than a run that refuses to start. **Comment that line out in your
> `.env`** unless it holds a real service-principal UUID you provisioned. Fixing the
> template is plan item P0.3; see the environment-variable notes below.

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
| `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` | required for Google connector runs — **but not currently forwarded by compose**, see below | none | UUID used as the connector service principal for audit events. Optional at process boot so non-connector workloads can start; connector execution fails closed at runtime when unset, and malformed values fail settings load. |
| `VITE_DEV_BACKEND_URL` | no (dev) | `http://127.0.0.1:8000` | Backend origin the frontend dev proxy forwards the listed tenant-scoped routes to. Verified loopback (`localhost`, `127.0.0.0/8`, `::1`) may use HTTP; every non-loopback origin must be HTTPS and listed in `UMS_DEV_TRUSTED_BACKEND_ORIGINS`. |
| `UMS_DEV_TRUSTED_BACKEND_ORIGINS` | no (dev) | none | Comma-separated exact origins allowed for a non-loopback `VITE_DEV_BACKEND_URL`; non-loopback entries must be HTTPS (HTTP is permitted only for verified loopback), read by Node-side Vite config, and never bundled. |
| `VITE_DEV_GATEWAY_USER_ID` | no (dev) | `00000000-0000-0000-0000-0000000000aa` | Dev `X-User-ID` injected by the Vite proxy on tenant-scoped routes. Non-secret. |
| `VITE_DEV_GATEWAY_USER_EMAIL` | no (dev) | `dev@ums.local` | Dev `X-User-Email` injected by the Vite proxy. Required by `current_principal_from_headers` in default `headers` auth mode. Non-secret. |
| `VITE_DEV_GATEWAY_ROLE` | no (dev) | `assistant_analyst` | Dev `X-Role` injected by the Vite proxy. Non-secret. **Change this before you judge the product** — see the note below. |
| `VITE_DEV_GATEWAY_SCOPE_TYPE` | no (dev) | `global` | Dev `X-Scope-Type` injected by the Vite proxy. Non-secret. |

> ⚠️ **`UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` is deliberately NOT forwarded by
> `docker-compose.yml`**. Setting it
> in `.env` therefore has **no effect on the compose `app` service**, and connector
> runs there will report it as unset even though you set it. That is intentional:
> `.env.example` currently ships the variable uncommented as a public placeholder UUID,
> and the runtime check refuses only on *unset* — any syntactically valid UUID is
> accepted. Forwarding it would mean every operator who ran `cp .env.example .env`
> attributed their connector audit trail to one well-known id from a public template.
> A refused connector run is recoverable; a mis-attributed audit trail is not.
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
> [`Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md`](Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md);
> the durable placeholder rejection is a separate dependency and is not included
> in this branch.

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

**Frontend env-var safety:** Vite exposes every `VITE_*` variable to client code via `import.meta.env` at build time. The trusted-gateway secret therefore lives under the non-`VITE_*` `UMS_TRUSTED_GATEWAY_TOKEN` name only; the Vite dev proxy reads it in Node and never includes it in the browser bundle. `VITE_DEV_BACKEND_URL` defaults to loopback. HTTP is accepted only for verified loopback; a non-loopback target must use HTTPS and its exact origin must appear in `UMS_DEV_TRUSTED_BACKEND_ORIGINS`, which prevents the token from being sent to an accidental or cleartext target.

---

## Running with `docker compose`

`docker-compose.yml` is the local single-box stack for development and smoke work: one
operator, one box, every published port bound to `127.0.0.1`. It is not the completed
deployment-readiness package for a beta.

```powershell
docker compose --env-file .env config   # renders the stack; fails loudly on anything missing
docker compose up -d                    # postgres + redis + migrate + app
docker compose logs -f app
docker compose down                     # stop + remove containers, KEEP the data volumes
```

> ⚠️ **`docker compose down` removes the application container and its local files.**
> This branch declares only `postgres-data` and `redis-data`; it does **not** declare
> an `app-data` volume or a backup/restore runbook. Export artifacts default to the
> container's temporary directory and local connector blobs default under the app
> working directory, so both are disposable when the app container is recreated.
> Do not treat a successful export as durable evidence until an operator-managed
> persistent store is supplied by the storage prerequisite.
>
> ⚠️ **`docker compose down -v` is not an ordinary teardown.** `-v` deletes the named
> `postgres-data` and `redis-data` volumes. The database volume contains revenue facts,
> audit rows, tenants, and role grants; Redis is not the financial source of truth. No
> prompt, no undo. Do not use `-v` unless you have an approved database backup and
> restore procedure outside this branch and intentionally accept a fresh database.

> ⚠️ **`.env.example` is not yet a complete template for compose.** It predates the
> database variables, so `docker compose --env-file .env.example config` exits 1 on
> `UMS_DB_USER`. The compose file's own header — the paragraph beginning *"Until it
> does, this file is the authoritative list"* — enumerates the five variables that have
> no default. Completing the template is plan item P0.3.

> **Compose environment boundary:** `docker-compose.yml` passes the database, auth,
> CORS, rate-limit, forwarding, and Redis settings shown in its `x-app-env` block.
> The connector executor, group-sync scheduler, and service-actor settings are **not
> forwarded by this branch**, so setting those names in `.env` alone does not change
> the long-running `app` container. Run the backend directly with `uv run uvicorn`
> when testing those process-level settings, or use the dependency that adds the
> corresponding Compose pass-throughs. `UMS_LOG_LEVEL` is not read by this branch and
> has no effect.

This branch does not include deployment-readiness, backup/restore, or structured-log
planning documents. Until those prerequisites land, keep the compose ports bound to
`127.0.0.1`, do not expose the stack to a LAN, tunnel, or Tailscale address, and treat
the local application files as disposable.

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
scripts/                  Operational CLIs (credential checks, connector runs, seeds)
tests/                    api / auth / db / finance / org / reports / scripts
docker-compose.yml        The local single-box stack
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
| [Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md](Docs/19_GOOGLE_CREDENTIAL_SETUP_SMOKE.md) | Google credential reference and smoke procedure |
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
