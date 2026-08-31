# UMS Smart Revenue Control Center

> Numbers-first internal revenue control plane for YouTube channel portfolios. Built for **UMS** as tenant #1, designed to onboard **Rotana Holding** and other tenants without redeployment.

This service ingests YouTube + AdSense data, reconciles it against bank movements, applies allocation rules, and exposes every monetary value with **source + formula + confidence + audit trail**. Every export is logged. Every locked month is immutable. Every override needs an approver.

---

## At a glance

| Aspect | Value |
|---|---|
| Backend | Python 3.14 · FastAPI · SQLAlchemy 2 · Alembic |
| Frontend | Vite 8 · React 19 · TypeScript 6 (shipped) |
| Storage | PostgreSQL 18 (single source of truth) · local file store (export artifacts) |
| Background jobs | In-process `ThreadPoolExecutor` (bounded queue; off by default via `UMS_CONNECTOR_JOB_EXECUTOR_ENABLED` env var) |
| Multi-tenant | Postgres Row-Level Security with `FORCE ROW LEVEL SECURITY` on 25 tenant-scoped tables (shipped PR #106) |
| Multi-currency | AED · USD · EUR · GBP · SAR · EGP — extensible. All math in `Decimal`. |
| Auth modes | `headers` (dev / bootstrap) · `database` (production; SQL-backed principal) |
| License | See [LICENSE](LICENSE) |

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
| `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` | required for Google connector runs | none | UUID used as the connector service principal for audit events. Optional at process boot so non-connector workloads can start; connector execution fails closed at runtime when unset, and malformed values fail settings load. The well-known placeholder UUID shipped in `.env.example` is rejected at runtime use — a copied template fails closed with a named placeholder instead of attributing audit rows to a published template id. |
| `UMS_APP_DATA_HOST` | yes for Compose | none | Dedicated artifact/blob bind prepared by `scripts/compose_storage.py`; `./data/ums` is recommended. Use its `compose` wrapper for lifecycle commands: host canonical validation mounts the resolved source and supplies the receipts required by root initialization and the application startup gate. |
| `APP_UID` | no (Compose image build) | `10001` | Positive numeric uid used to build the image's non-root `app` account. Storage initialization resolves that actual account instead of duplicating the configured number. |
| `VITE_DEV_BACKEND_URL` | no (dev) | `http://127.0.0.1:8000` | Backend origin the frontend dev proxy forwards `/tenants/*` to. Dev-only; read by `frontend/vite.config.ts`. |
| `VITE_DEV_GATEWAY_USER_ID` | no (dev) | `00000000-0000-0000-0000-0000000000aa` | Dev `X-User-ID` injected by the Vite proxy on tenant-scoped routes. Non-secret. |
| `VITE_DEV_GATEWAY_USER_EMAIL` | no (dev) | `dev@ums.local` | Dev `X-User-Email` injected by the Vite proxy. Required by `current_principal_from_headers` in default `headers` auth mode. Non-secret. |
| `VITE_DEV_GATEWAY_ROLE` | no (dev) | `assistant_analyst` | Dev `X-Role` injected by the Vite proxy. Non-secret. |
| `VITE_DEV_GATEWAY_SCOPE_TYPE` | no (dev) | `global` | Dev `X-Scope-Type` injected by the Vite proxy. Non-secret. |

**Never commit `.env` files.** Use the `.env.example` template, copy locally, and let the secrets layer (Vault / External Secrets Operator) provide them in clusters.

**Frontend env-var safety:** Vite exposes every `VITE_*` variable to client code via `import.meta.env` at build time. The trusted-gateway secret therefore lives under the non-`VITE_*` `UMS_TRUSTED_GATEWAY_TOKEN` name only; the Vite dev proxy reads it in Node and never includes it in the browser bundle.

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

Docs/                     17 design docs + security pack + Codex implementation notes
mockups/                  Static HTML mockup + QA screenshots
tests/                    67 test files: api / auth / db / finance / org / reports
deploy/helm/              Helm chart for Kubernetes (in progress)
```

---

## How the auth model works (one-paragraph version)

A `Principal` has a user id, email, role assignments, and direct permission grants. Role assignments and direct grants carry an access scope (global, company, sector, channel, finance month, or connector). Tenant isolation is enforced at the database layer via Postgres Row-Level Security with `FORCE ROW LEVEL SECURITY` on 25 tenant-scoped tables (shipped PR #106). Each protected route declares the permission it needs (`require_permission(Permission.LOCK_FINANCE_MONTH)`) plus, often, a scope predicate (`can_view_channel_revenue(principal, channel_id)`). The `auth/policy.py` module is the single source of truth for that decision. Every sensitive read or write writes an `AuditLogEntry` with the actor, scope, sensitive flag, and (for writes) a non-blank reason. Production runs `UMS_AUTHZ_SOURCE=database` so headers cannot be spoofed.

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
| [Docs/20_COMPOSE_STORAGE_RUNBOOK.md](Docs/20_COMPOSE_STORAGE_RUNBOOK.md) | Compose bind preparation, coordinated backup, verification, and recovery |
| [Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md](Docs/22_BACKUP_RESTORE_AND_REHEARSAL.md) | Atomic database backup, clean restore, and throwaway rehearsal |
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
