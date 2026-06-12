# alembic/env.py URL-precedence Hardening — Implementation Plan

> **For agentic workers:** strict TDD per task (failing test → run-to-fail → minimal
> impl → run-to-pass → commit). No push/PR/merge without explicit authorization.

**Goal:** Close the `alembic/env.py` footgun where an ambient `UMS_DATABASE_URL` silently
overrides an explicitly-injected programmatic `sqlalchemy.url`, while preserving the
production contract (env var wins over the `alembic.ini` placeholder).

**Architecture:** Approach A from the spec — extract URL resolution into a pure, importable
seam (`db/migration_url.py::resolve_database_url(config, environ)`) gated on
`config.config_file_name`. `env.py::get_database_url()` becomes a thin delegate. The seam is
unit-testable without an Alembic runtime context (env.py runs migrations at import time, so it
cannot be imported directly).

**Tech Stack:** Alembic `Config`, SQLAlchemy, pytest, disposable Postgres 18.

Spec: `Docs/superpowers/specs/2026-06-11-alembic-env-url-precedence-design.md`.

---

## Decision: the precedence rule

```python
configured = config.get_main_option("sqlalchemy.url")
declared_on_disk = _ini_declared_url(config)   # re-read the ini file's own url
if configured and configured != declared_on_disk:
    return configured            # deliberate in-code injection wins over the env var
url = environ.get("UMS_DATABASE_URL") or configured  # prod: env var wins over placeholder
if not url:
    raise RuntimeError(...)       # empty-guard preserved
return url
```

A configured `sqlalchemy.url` that differs from the ini file's on-disk declaration was injected
in code (tests/embedded callers) and must win over an ambient `UMS_DATABASE_URL`. When it equals
the ini's declared value (production placeholder, no override), the env var wins, preserving the
prod contract.

> **Why not just `config.config_file_name is None`?** A review found four migration tests
> (`tests/tenancy/test_isolation.py`, `test_rls_restricted_login.py`, `test_rls_grant_surface.py`,
> `tests/db/test_session_tenant_hook.py`) build `Config("alembic.ini")` and inject the url, so
> `config_file_name` is set for them — a bare `config_file_name` gate would leave the footgun open.
> The on-disk comparison closes it for both caller patterns without touching those sensitive tests.
> See the spec's "Post-review correction".

---

### Task 1: Pure resolver + unit tests (4 quadrants + guard + edge)

**Files:**
- Create: `backend/ums_smart_revenue/db/migration_url.py`
- Test: `tests/db/test_migration_url_resolution.py`

- [ ] **Step 1: Write failing unit tests** covering all four spec quadrants plus the
  empty-guard and the "programmatic Config that injected no url falls back to env var" edge.
- [ ] **Step 2: Run → fail** (`ModuleNotFoundError: ...migration_url`).
- [ ] **Step 3: Implement** `resolve_database_url(config, environ)` with the rule above and a
  contract comment block.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** (trailer-free).

### Task 2: Delegate env.py to the seam

**Files:**
- Modify: `backend/ums_smart_revenue/db/alembic/env.py` (`get_database_url`)

- [ ] **Step 1:** Replace the body of `get_database_url()` with
  `return resolve_database_url(config, os.environ)`, importing the seam. Keep the function
  name/signature so both `run_migrations_offline` and `run_migrations_online` are covered.
- [ ] **Step 2:** Run the unit suite + a representative migration import to confirm no break.
- [ ] **Step 3: Commit.**

### Task 3: End-to-end Postgres regression proof

**Files:**
- Test: `tests/db/test_alembic_env_url_precedence_postgres.py`

- [ ] **Step 1: Write failing test** — export a DECOY `UMS_DATABASE_URL` (unreachable port),
  build a programmatic `Config()` pointed at the real disposable test DB, `command.upgrade(cfg,
  "head")`, assert a known table (`users`) exists in the test DB. With the footgun live this
  would attempt the decoy and raise `OperationalError`; with the fix it targets the test DB.
- [ ] **Step 2: Run against clean-room PG → pass.**
- [ ] **Step 3: Commit.**

### Task 4: Docs + full validation gate

**Files:**
- Modify: `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md`

- [ ] Inline status note (done) for the env-url hardening.
- [ ] `python -m ruff check backend tests` — clean.
- [ ] `python -m pytest -q` on a fresh clean-room PG cluster — full suite green.
- [ ] `git diff --check` — clean.
- [ ] Commit docs + validation.

## Blast radius

`alembic/env.py` is the migration entry point for prod deploys and the test suite. The change is
pure URL-resolution logic; **no schema/data change**. `No graph projection impact detected.`
Production (ini-based) behavior is byte-for-byte preserved; only the programmatic path changes,
and only to honor an explicitly-injected url over an ambient env var.
