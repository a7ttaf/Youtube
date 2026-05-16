# UMS Smart Revenue Control Center

> Numbers-first internal revenue control plane for YouTube channel portfolios. Built for **UMS** as tenant #1, designed to onboard **Rotana Holding** and other tenants without redeployment.

This service ingests YouTube + AdSense data, reconciles it against bank movements, applies allocation rules, and exposes every monetary value with **source + formula + confidence + audit trail**. Every export is logged. Every locked month is immutable. Every override needs an approver.

---

## At a glance

| Aspect | Value |
|---|---|
| Backend | Python 3.14 · FastAPI · SQLAlchemy 2 · Alembic |
| Frontend | Next.js 16 · React 19 · TypeScript 6 *(in progress — Phase 5)* |
| Storage | PostgreSQL 18 (single source of truth) · Redis 8.6 (cache + pub/sub) · MinIO (S3-compatible object store, future) |
| Background jobs | Celery 5.6 *(workers wired in Phase 2)* |
| Multi-tenant | Postgres Row-Level Security with `tenant_id` column on every tenant-scoped table |
| Multi-currency | AED · USD · EUR · GBP · SAR · EGP — extensible. All math in `Decimal`. |
| Auth modes | `headers` (dev / bootstrap) · `database` (production; SQL-backed principal) |
| License | See [LICENSE](LICENSE) |

For the long-form vision, read [PRODUCT.md](PRODUCT.md) and [DESIGN.md](DESIGN.md). For the spec pack, see [Docs/](Docs/).

---

## Quickstart

> Requires Python 3.14, PostgreSQL 18, Redis 8, and [uv](https://docs.astral.sh/uv/) installed locally. Start PostgreSQL and Redis with your local service manager before launching the API.

```powershell
# 1) Install Python deps with uv
uv sync --extra dev --extra test --extra lint

# 2) Start Postgres + Redis outside this repo.
#    Verify the database accepts connections before running migrations.

# 3) Configure environment (see below for the full env-var matrix)
$env:PYTHONPATH = (Resolve-Path "backend").Path
$env:UMS_DATABASE_URL = "postgresql+asyncpg://ums:ums@localhost:5432/ums_smart_revenue"
$env:UMS_AUTHZ_SOURCE = "headers"
$env:UMS_TRUSTED_GATEWAY_TOKEN = "dev-only-token-set-locally"

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
| `UMS_DATABASE_URL` | yes (prod) | none | SQLAlchemy URL for PostgreSQL. Use `postgresql+asyncpg://…` for the async driver. |
| `UMS_AUTHZ_SOURCE` | no | `headers` | `headers` for dev/bootstrap, `database` for production (loads principal + roles from SQL). |
| `UMS_TRUSTED_GATEWAY_TOKEN` | no | none | Shared secret asserted by the upstream identity gateway. Required in `database` mode. |

**Never commit `.env` files.** Use the `.env.example` template, copy locally, and let the secrets layer (Vault / External Secrets Operator) provide them in clusters.

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

A `Principal` has `tenant_id`, roles, and direct permission grants. Roles are tenant-scoped (e.g. `FINANCE_ADMIN` of UMS ≠ `FINANCE_ADMIN` of Rotana). Each protected route declares the permission it needs (`require_permission(Permission.LOCK_FINANCE_MONTH)`) plus, often, a scope predicate (`can_view_channel_revenue(principal, channel_id)`). The `auth/policy.py` module is the single source of truth for that decision. Every sensitive read or write writes an `AuditLogEntry` with the actor, scope, sensitive flag, and (for writes) a non-blank reason. Production runs `UMS_AUTHZ_SOURCE=database` so headers cannot be spoofed.

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
