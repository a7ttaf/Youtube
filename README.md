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
| Background jobs | In-process `ThreadPoolExecutor` (bounded queue; off by default via `connector_job_executor_enabled` setting) |
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

# 3) Configure environment (see below for the full env-var matrix)
$env:PYTHONPATH = (Resolve-Path "backend").Path
$env:UMS_DATABASE_URL = "postgresql+psycopg://ums:ums@localhost:5432/ums_smart_revenue"
$env:UMS_AUTHZ_SOURCE = "headers"
$env:UMS_TRUSTED_GATEWAY_TOKEN = "<set-a-local-development-token>"

# 4) Run migrations
uv run alembic upgrade head

# 5) Run the API
uv run uvicorn ums_smart_revenue.app:app --reload --host 0.0.0.0 --port 8000
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
| `UMS_TRUSTED_GATEWAY_TOKEN` | yes for protected routes | none | Shared secret asserted by the upstream identity gateway. Required for both `headers` bootstrap auth and `database` auth. Also read by `frontend/vite.config.ts` in Node to inject the dev proxy `X-UMS-Trusted-Gateway-Token` header. **Never use a `VITE_*` alias** — any `VITE_*` env is embedded in the client bundle. |
| `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` | required for Google connector runs | none | UUID used as the connector service principal for audit events. Optional at process boot so non-connector workloads can start; connector execution fails closed at runtime when unset, and malformed values fail settings load. |
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
