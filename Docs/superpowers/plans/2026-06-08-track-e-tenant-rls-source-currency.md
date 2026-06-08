# Track E — Tenant RLS Hardening + Source-Rows Read API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Postgres database-level tenant isolation (RLS policies + `app_tenant`/`app_platform` roles + a context-gated session role/GUC hook + write-side asserts) and a read-only Google source-rows API, in one backend PR.

**Architecture:** RLS enforces tenant filtering at the DB. A single connection pool switches role per transaction via `SET LOCAL ROLE` and sets `app.current_tenant_id` from the request `TENANT_CTX` contextvar — **Postgres-only** and **only when a tenant is in context**, so the existing SQLite suite and non-tenant Postgres sessions are untouched. A new `GET /revenue/source-rows` surface mirrors the existing connector-runs cursor read pattern.

**Tech Stack:** FastAPI, SQLAlchemy 2.x, Alembic, PostgreSQL (RLS), pytest, psycopg.

**Spec:** `Docs/superpowers/specs/2026-06-08-track-e-tenant-rls-source-currency-design.md` (commits 939790a + 24e00be).

**Branch:** `feat/track-e-tenant-rls-source-currency` (off origin/main `e27b388`).

---

## Hard constraints (apply to every task)

- **TDD per task:** failing test → run-to-fail → minimal impl → run-to-pass → commit.
- **Commits are trailer-free** (no `Co-Authored-By`, no generated footer).
- Validation commands: `python -m pytest ...` (never bare `pytest`), `python -m ruff check backend tests scripts`, `git diff --check`.
- **Keep ALL touched Python lines ≤ 100 chars** (DeepSource FLK-E501 enforces 100, not 120).
- Do **not** use `git checkout`/`restore`/`reset` on files.
- **Shell is PowerShell 7+ on Windows.** Commands below use PowerShell syntax.
  Env vars: `$env:NAME = "value"` (not `export`). Chain with `;` or `&&`.
  Multi-line Python is passed via a temp `.py` file run with `python file.py`,
  **not** a bash `<<'PY'` heredoc (heredocs are not PowerShell).
- Postgres-tier tests need `UMS_TEST_DATABASE_URL` → disposable container `ums-mig-pg-test`:
  set it with
  `$env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/test_ums"`.
- Do **not** push, open a PR, or merge. Mahmoud handles that.
- **Re-confirm every line number against current code before editing** (read first); anchors below were captured 2026-06-08 but may drift.

---

## File Structure

**Create:**
- `backend/ums_smart_revenue/db/rls.py` — shared RLS helpers: the canonical tenant-table allowlist constant, the `enable_tenant_rls(op, table)` / `disable_tenant_rls(op, table)` migration helpers, and the introspection-vs-allowlist drift check.
- `backend/ums_smart_revenue/db/alembic/versions/20260608_0001_tenant_rls_enforcement.py` — roles + RLS policies + grants migration.
- `backend/ums_smart_revenue/tenancy/isolation.py` — `TenantIsolationError` + `assert_tenant_match`.
- `backend/ums_smart_revenue/finance/source_rows_read.py` — read repo: `SourceRowEntry`, `SourceRowPage`, `list_source_rows`, `get_source_row`, `SourceRowReadError`/`SourceRowValidationError`.
- `backend/ums_smart_revenue/api/source_rows.py` — `GET /revenue/source-rows` + `/{id}` router.
- `tests/tenancy/test_isolation.py` — Postgres-only DB-boundary matrix (run as `app_tenant`).
- `tests/db/test_session_tenant_hook.py` — session hook unit + pooled-reset tests.
- `tests/tenancy/test_assert_tenant_match.py` — helper unit tests.
- `tests/finance/test_source_rows_read_repository.py` — read-repo tests.
- `tests/api/test_source_rows_api.py` — route tests.
- `tests/db/test_tenant_rls_migration.py` — migration round-trip (Postgres-only).

**Modify:**
- `backend/ums_smart_revenue/db/session.py` — add the `after_begin` hook + `build_platform_session_factory`.
- `backend/ums_smart_revenue/app.py` — register the source-rows router.
- The enumerated write-path repositories (Task 6) — add `assert_tenant_match` calls.
- `Docs/17`, `Docs/18`, `Docs/12`, `Docs/01`, `Docs/15` (Task 9).

---

## Task 1: RLS helper module + canonical tenant-table allowlist

**Files:**
- Create: `backend/ums_smart_revenue/db/rls.py`
- Test: `tests/db/test_rls_helpers.py`

The allowlist is the authoritative list of every table carrying a `tenant_id` column. Build it from the live schema (do not trust memory).

- [ ] **Step 1: Capture the real tenant-table set.** Write a temp script `_scan_tenant_tables.py` at the repo root (PowerShell has no bash heredoc), then run and delete it:

```python
# _scan_tenant_tables.py
import importlib
import pkgutil

import ums_smart_revenue.db as db

for mod in pkgutil.iter_modules(db.__path__):
    if mod.name.endswith("_models"):
        importlib.import_module(f"ums_smart_revenue.db.{mod.name}")

from sqlalchemy.orm import DeclarativeBase  # noqa: E402

seen = set()
for base in DeclarativeBase.__subclasses__():
    for table in base.metadata.tables.values():
        if "tenant_id" in table.columns and table.name != "tenants":
            seen.add(table.name)
print(sorted(seen))
```

```powershell
python _scan_tenant_tables.py
Remove-Item _scan_tenant_tables.py
```

If that import-discovery is awkward (e.g. multiple declarative bases not all imported), instead confirm by reading each `*_models.py` under `backend/ums_smart_revenue/db/` and listing every `__tablename__` whose class declares a `tenant_id` mapped column (use the Grep tool for `tenant_id`). Write the exact sorted list down — it becomes `TENANT_SCOPED_TABLES` below. Expected to include at least: the 18 from migration `20260517_0001` (`users`, `access_scopes`, `user_role_assignments`, `user_permission_grants`, `audit_logs`, `api_connector_credentials`, `org_units`, `youtube_channels`, `channel_groups`, `channel_group_members`, `finance_month_close`, `monthly_channel_revenue_facts`, `revenue_manual_overrides`, `adsense_payments`, `bank_reconciliation_entries`, `raw_report_files`, `number_explanations`, `export_jobs`) **plus** every later tenant table: `google_revenue_source_rows`, `channel_account_map`, the committed-allocation tables (e.g. `committed_allocation_runs` and its child tables), and the deduction-component tables. `currencies` is platform-wide and is **excluded**.

- [ ] **Step 2: Write the failing test** (`tests/db/test_rls_helpers.py`):

```python
from ums_smart_revenue.db.rls import (
    TENANT_SCOPED_TABLES,
    tenant_rls_policy_name,
    discover_tenant_tables_sql,
)


def test_allowlist_is_nonempty_and_excludes_platform_tables():
    assert "monthly_channel_revenue_facts" in TENANT_SCOPED_TABLES
    assert "google_revenue_source_rows" in TENANT_SCOPED_TABLES
    assert "tenants" not in TENANT_SCOPED_TABLES
    assert "currencies" not in TENANT_SCOPED_TABLES
    # No duplicates.
    assert len(TENANT_SCOPED_TABLES) == len(set(TENANT_SCOPED_TABLES))


def test_policy_name_is_table_scoped():
    assert tenant_rls_policy_name("adsense_payments") == (
        "adsense_payments_tenant_isolation"
    )


def test_discover_sql_targets_tenant_id_columns():
    sql = discover_tenant_tables_sql()
    assert "information_schema.columns" in sql
    assert "tenant_id" in sql
```

- [ ] **Step 3: Run to verify it fails.** `python -m pytest tests/db/test_rls_helpers.py -q` → FAIL (module missing).

- [ ] **Step 4: Implement `backend/ums_smart_revenue/db/rls.py`:**

```python
"""Shared Row-Level Security helpers: the canonical tenant-table allowlist and
the migration helpers that enable/disable tenant-isolation policies.

Used by the RLS enforcement migration and by the isolation drift guard. The
allowlist is the single source of truth for "which tables are tenant-scoped";
the migration cross-checks it against the live schema so a new tenant table that
ships without RLS becomes a loud migration failure, not a silent leak.
"""
from __future__ import annotations

# Every table carrying a tenant_id column (platform-wide tables excluded).
# Keep sorted; the migration asserts this equals the live information_schema set.
TENANT_SCOPED_TABLES: tuple[str, ...] = (
    # NOTE: populate verbatim from Step 1's captured list, sorted.
    "access_scopes",
    "adsense_payments",
    "api_connector_credentials",
    "audit_logs",
    "bank_reconciliation_entries",
    "channel_account_map",
    "channel_group_members",
    "channel_groups",
    "committed_allocation_runs",
    "export_jobs",
    "finance_month_close",
    "google_revenue_source_rows",
    "monthly_channel_revenue_facts",
    "number_explanations",
    "org_units",
    "raw_report_files",
    "revenue_manual_overrides",
    "user_permission_grants",
    "user_role_assignments",
    "users",
    "youtube_channels",
)

APP_TENANT_ROLE = "app_tenant"
APP_PLATFORM_ROLE = "app_platform"
TENANT_GUC = "app.current_tenant_id"


def tenant_rls_policy_name(table: str) -> str:
    """Return the deterministic isolation-policy name for a tenant table."""
    return f"{table}_tenant_isolation"


def discover_tenant_tables_sql() -> str:
    """SQL returning every public table that has a tenant_id column (minus tenants)."""
    return (
        "SELECT table_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND column_name = 'tenant_id' "
        "AND table_name <> 'tenants' ORDER BY table_name"
    )
```

> Implementer: replace the `TENANT_SCOPED_TABLES` body with the exact sorted list captured in Step 1. The list above is the expected shape; verify each entry against the live schema (committed-allocation child table names and deduction-component table names must be confirmed — read `20260602_0001` and `20260529_0002` migrations).

- [ ] **Step 5: Run to verify it passes.** `python -m pytest tests/db/test_rls_helpers.py -q` → PASS.

- [ ] **Step 6: Lint + commit.**

```bash
python -m ruff check backend tests
git add backend/ums_smart_revenue/db/rls.py tests/db/test_rls_helpers.py
git commit -m "feat(rls): tenant-table allowlist and RLS helper module"
```

---

## Task 2: RLS enforcement migration (roles + policies + grants + drift guard)

**Files:**
- Create: `backend/ums_smart_revenue/db/alembic/versions/20260608_0001_tenant_rls_enforcement.py`
- Test: `tests/db/test_tenant_rls_migration.py`

Migration is **Postgres-only** in effect (wrap the body in a dialect guard; no-op on SQLite so SQLite migration tests still pass). `down_revision = "20260606_0001"` (current head — reconfirm with `python -m alembic heads` before writing).

- [ ] **Step 1: Confirm the head revision.**

```bash
cd backend && python -m alembic heads
```

Expected: `20260606_0001 (head)`. If different, use the reported head as `down_revision`.

- [ ] **Step 2: Write the failing Postgres round-trip test** (`tests/db/test_tenant_rls_migration.py`):

```python
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from tests.db._postgres_helpers import require_postgres_url
from ums_smart_revenue.db.rls import (
    APP_PLATFORM_ROLE,
    APP_TENANT_ROLE,
    TENANT_SCOPED_TABLES,
    tenant_rls_policy_name,
)


def _alembic_config(url: str) -> Config:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_rls_migration_creates_roles_policies_and_grants():
    url = require_postgres_url()
    cfg = _alembic_config(url)
    command.upgrade(cfg, "head")
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            roles = set(
                conn.execute(
                    sa.text("SELECT rolname FROM pg_roles")
                ).scalars()
            )
            assert APP_TENANT_ROLE in roles
            assert APP_PLATFORM_ROLE in roles
            # app_platform bypasses RLS; app_tenant does not.
            bypass = dict(
                conn.execute(
                    sa.text("SELECT rolname, rolbypassrls FROM pg_roles")
                ).all()
            )
            assert bypass[APP_PLATFORM_ROLE] is True
            assert bypass[APP_TENANT_ROLE] is False
            # Every allowlisted table has RLS enabled + the isolation policy.
            for table in TENANT_SCOPED_TABLES:
                enabled = conn.execute(
                    sa.text(
                        "SELECT relrowsecurity FROM pg_class "
                        "WHERE relname = :t"
                    ),
                    {"t": table},
                ).scalar()
                assert enabled is True, f"{table} RLS not enabled"
                policy = conn.execute(
                    sa.text(
                        "SELECT policyname FROM pg_policies "
                        "WHERE tablename = :t AND policyname = :p"
                    ).bindparams(t=table, p=tenant_rls_policy_name(table))
                ).first()
                assert policy is not None, f"{table} missing policy"
    finally:
        engine.dispose()
```

> `pg_policies` columns are `schemaname, tablename, policyname, ...` — the query uses `policyname` (there is no `polname` in the `pg_policies` view; `polname` is on the underlying `pg_policy` catalog).

- [ ] **Step 3: Run to verify it fails.**

```bash
$env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/test_ums"
python -m pytest tests/db/test_tenant_rls_migration.py -q
```

Expected: FAIL (migration not present / roles absent).

- [ ] **Step 4: Implement the migration.** Use the `20260606_0001` file as the structural template (imports, `revision`/`down_revision`, `op.get_bind().dialect.name` guard). Body:

```python
"""Enable tenant Row-Level Security: app_tenant/app_platform roles + policies.

Revision ID: 20260608_0001
Revises: 20260606_0001
Create Date: 2026-06-08

Postgres-only in effect (SQLite has no RLS/roles; the whole body is guarded and
no-ops there). Creates the two roles idempotently, enables RLS + an isolation
policy on every tenant-scoped table, and grants the tenant CRUD surface to
app_tenant. A drift guard fails the migration if the live set of tenant_id
tables does not equal db.rls.TENANT_SCOPED_TABLES, so a new tenant table cannot
ship unprotected.

Deploy precondition: the migration/bootstrap DB user needs role-management
privilege (CREATEROLE or membership-admin on these roles), OR a DBA pre-creates
the two roles, their grants, and the runtime login's membership
(GRANT app_tenant/app_platform TO <login> WITH INHERIT FALSE, SET TRUE) per the
runbook. The CREATE ROLE statements are guarded against existing roles, so a
DBA-precreated environment upgrades cleanly. This does not assume superuser.

Rollback: drops policies, disables RLS, revokes grants, and drops the two roles
(guarded). Like the prior tenant_id NOT NULL work, the practical rollback is the
RLS-state reversal only.
"""
import sqlalchemy as sa
from alembic import op

from ums_smart_revenue.db.rls import (
    APP_PLATFORM_ROLE,
    APP_TENANT_ROLE,
    TENANT_SCOPED_TABLES,
    discover_tenant_tables_sql,
    tenant_rls_policy_name,
)

revision = "20260608_0001"
down_revision = "20260606_0001"
branch_labels = None
depends_on = None


def _create_role(bind, role: str, *, bypassrls: bool) -> None:
    """Create a NOLOGIN role idempotently; set BYPASSRLS as requested."""
    exists = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
    ).first()
    if exists is None:
        bypass = "BYPASSRLS" if bypassrls else "NOBYPASSRLS"
        # Role names are internal constants, not user input.
        bind.execute(sa.text(f'CREATE ROLE "{role}" NOLOGIN {bypass}'))
    else:
        bypass = "BYPASSRLS" if bypassrls else "NOBYPASSRLS"
        bind.execute(sa.text(f'ALTER ROLE "{role}" {bypass}'))


def _assert_no_drift(bind) -> None:
    """Fail if the live tenant_id table set != the allowlist constant."""
    live = set(bind.execute(sa.text(discover_tenant_tables_sql())).scalars())
    expected = set(TENANT_SCOPED_TABLES)
    if live != expected:
        missing = expected - live
        extra = live - expected
        raise RuntimeError(
            "Tenant RLS allowlist drift. "
            f"In allowlist but not in schema: {sorted(missing)}; "
            f"in schema but not in allowlist: {sorted(extra)}. "
            "Update db.rls.TENANT_SCOPED_TABLES."
        )


def upgrade() -> None:
    """Create roles and enable tenant-isolation RLS on all tenant tables."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _assert_no_drift(bind)
    _create_role(bind, APP_TENANT_ROLE, bypassrls=False)
    _create_role(bind, APP_PLATFORM_ROLE, bypassrls=True)
    bind.execute(sa.text(f'GRANT USAGE ON SCHEMA public TO "{APP_TENANT_ROLE}"'))
    bind.execute(
        sa.text(f'GRANT USAGE ON SCHEMA public TO "{APP_PLATFORM_ROLE}"')
    )
    for table in TENANT_SCOPED_TABLES:
        policy = tenant_rls_policy_name(table)
        bind.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        # DROP-then-CREATE so a half-applied dev run / re-run does not wedge on
        # an existing policy (CREATE POLICY has no IF NOT EXISTS).
        bind.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
        bind.execute(
            sa.text(
                f"CREATE POLICY {policy} ON {table} "
                "USING (tenant_id = current_setting('app.current_tenant_id')::uuid) "
                "WITH CHECK (tenant_id = current_setting('app.current_tenant_id')::uuid)"
            )
        )
        bind.execute(
            sa.text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} "
                f'TO "{APP_TENANT_ROLE}"'
            )
        )
        bind.execute(
            sa.text(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} "
                f'TO "{APP_PLATFORM_ROLE}"'
            )
        )


def downgrade() -> None:
    """Drop policies, disable RLS, revoke grants, and drop the two roles."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in TENANT_SCOPED_TABLES:
        policy = tenant_rls_policy_name(table)
        bind.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
        bind.execute(
            sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
        )
        for role in (APP_TENANT_ROLE, APP_PLATFORM_ROLE):
            bind.execute(
                sa.text(
                    f"REVOKE SELECT, INSERT, UPDATE, DELETE ON {table} "
                    f'FROM "{role}"'
                )
            )
    for role in (APP_TENANT_ROLE, APP_PLATFORM_ROLE):
        bind.execute(
            sa.text(f'REVOKE USAGE ON SCHEMA public FROM "{role}"')
        )
        bind.execute(sa.text(f'DROP ROLE IF EXISTS "{role}"'))
```

> SQL-injection note: every interpolated name is an internal constant from `db.rls`, never user input. Keep it that way; do not interpolate request data.

- [ ] **Step 5: Run to verify it passes.** Re-run the Step 3 command → PASS. Also run the existing SQLite migration test suite to confirm the dialect guard keeps SQLite green:

```bash
python -m pytest tests/db -q
```

- [ ] **Step 6: Lint + commit.**

```bash
python -m ruff check backend tests
git add backend/ums_smart_revenue/db/alembic/versions/20260608_0001_tenant_rls_enforcement.py tests/db/test_tenant_rls_migration.py
git commit -m "feat(rls): migration creating tenant roles, policies, grants, drift guard"
```

---

## Task 3: Session role/GUC hook + platform lane

**Files:**
- Modify: `backend/ums_smart_revenue/db/session.py`
- Test: `tests/db/test_session_tenant_hook.py`

The hook is `after_begin` (transaction-scoped), Postgres-only, and acts only when the session's lane warrants it: default (`app_tenant`) lane sets role + GUC **only when a tenant is in `TENANT_CTX`**; platform lane sets `app_platform` role and **no** GUC.

- [ ] **Step 1: Write failing unit tests** (`tests/db/test_session_tenant_hook.py`). SQLite-backed no-op tests run anywhere; pooled-reset is Postgres-only.

```python
import sqlalchemy as sa
from sqlalchemy.orm import Session

from tests.db._postgres_helpers import require_postgres_url
from ums_smart_revenue.db.session import (
    build_platform_session_factory,
    build_session_factory,
)
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus


def _tenant(uuid_str: str) -> Tenant:
    from datetime import datetime, timezone
    from uuid import UUID
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Tenant(
        id=UUID(uuid_str), slug="ums", display_name="UMS",
        primary_currency="USD", status=TenantStatus.ACTIVE,
        onboarding_at=now, created_at=now, updated_at=now,
    )


def test_sqlite_session_issues_no_set_statements():
    # On SQLite the hook must be a complete no-op (no SET ROLE / GUC).
    factory = build_session_factory("sqlite+pysqlite:///:memory:")
    token = TENANT_CTX.set(_tenant("00000000-0000-0000-0000-000000000001"))
    try:
        with factory() as session:
            # A trivial query must not raise (no Postgres-only SQL emitted).
            assert session.execute(sa.text("SELECT 1")).scalar() == 1
    finally:
        TENANT_CTX.reset(token)


def test_postgres_tenant_lane_sets_role_and_guc():
    url = require_postgres_url()
    factory = build_session_factory(url)
    tid = "00000000-0000-0000-0000-000000000001"
    token = TENANT_CTX.set(_tenant(tid))
    try:
        with factory() as session:
            assert session.execute(
                sa.text("SELECT current_setting('app.current_tenant_id', true)")
            ).scalar() == tid
            assert session.execute(
                sa.text("SELECT current_user")
            ).scalar() == "app_tenant"
    finally:
        TENANT_CTX.reset(token)


def test_postgres_no_context_leaves_login_role_and_unset_guc():
    url = require_postgres_url()
    factory = build_session_factory(url)
    # No TENANT_CTX → hook must not switch role or set the GUC.
    with factory() as session:
        assert session.execute(
            sa.text("SELECT current_setting('app.current_tenant_id', true)")
        ).scalar() in (None, "")
        assert session.execute(
            sa.text("SELECT current_user")
        ).scalar() != "app_tenant"


def test_platform_lane_uses_app_platform_and_no_guc():
    url = require_postgres_url()
    factory = build_platform_session_factory(url)
    with factory() as session:
        assert session.execute(
            sa.text("SELECT current_user")
        ).scalar() == "app_platform"
        assert session.execute(
            sa.text("SELECT current_setting('app.current_tenant_id', true)")
        ).scalar() in (None, "")


def test_pooled_connection_does_not_leak_role_or_guc():
    # Transaction 1 sets tenant lane; transaction 2 on the SAME pooled
    # connection (no context) must see no leaked role/GUC.
    url = require_postgres_url()
    engine = sa.create_engine(url, pool_size=1, max_overflow=0)
    factory = build_session_factory(url, engine=engine)
    tid = "00000000-0000-0000-0000-000000000001"
    token = TENANT_CTX.set(_tenant(tid))
    try:
        with factory() as s1:
            assert s1.execute(sa.text("SELECT current_user")).scalar() == "app_tenant"
            s1.commit()
    finally:
        TENANT_CTX.reset(token)
    # Reuse the pool with no context.
    with factory() as s2:
        assert s2.execute(sa.text("SELECT current_user")).scalar() != "app_tenant"
        assert s2.execute(
            sa.text("SELECT current_setting('app.current_tenant_id', true)")
        ).scalar() in (None, "")
    engine.dispose()
```

- [ ] **Step 2: Run to verify failure.** SQLite test first:

```bash
python -m pytest tests/db/test_session_tenant_hook.py::test_sqlite_session_issues_no_set_statements -q
```

Expected: FAIL (`build_platform_session_factory` missing).

- [ ] **Step 3: Implement the hook + platform factory in `db/session.py`.** Add imports and append:

```python
from sqlalchemy import event

from ums_smart_revenue.db.rls import APP_PLATFORM_ROLE, APP_TENANT_ROLE, TENANT_GUC

_SESSION_ROLE_KEY = "ums_db_role"


def build_platform_session_factory(
    database_url: str, engine: Engine | None = None
) -> SessionFactory:
    """Return a sessionmaker whose sessions run the privileged app_platform lane."""
    factory = build_session_factory(database_url, engine=engine)
    factory.configure(info={_SESSION_ROLE_KEY: APP_PLATFORM_ROLE})
    return factory


# ============================================================================
# Purpose: Per-transaction tenant isolation hook. On Postgres only, switch the
#   transaction role and set app.current_tenant_id so RLS policies filter rows.
# Database/ORM: All tenant-scoped tables (RLS policies created in 20260608_0001).
# Standards: SET LOCAL (transaction-scoped, auto-reset on commit/rollback);
#   fail-closed (no tenant context on the tenant lane => no GUC => RLS errors).
# Blast Radius: Authorization/finance reads+writes at the DB boundary. No-op on
#   SQLite and on tenant-lane sessions opened without a resolved tenant, so the
#   existing test suite and pre-S2.4 non-tenant paths are unaffected.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/context.py -> tenant in contextvar.
#   - File: backend/ums_smart_revenue/db/rls.py -> role names + GUC key.
# ============================================================================
@event.listens_for(Session, "after_begin")
def _apply_tenant_isolation(session, transaction, connection):
    """Set transaction role + tenant GUC for Postgres sessions when warranted."""
    if connection.dialect.name != "postgresql":
        return
    role = session.info.get(_SESSION_ROLE_KEY, APP_TENANT_ROLE)
    if role == APP_PLATFORM_ROLE:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"')
        return
    # Default app_tenant lane: only act when a tenant is in context.
    from ums_smart_revenue.tenancy.context import get_current_tenant

    tenant = get_current_tenant()
    if tenant is None:
        return
    # ROLE is an identifier (cannot be a bind param) but is an internal
    # constant, never user input. The tenant id IS data -> set via parameterized
    # set_config (is_local=true => transaction-scoped, same lifetime as SET LOCAL).
    connection.exec_driver_sql(f'SET LOCAL ROLE "{APP_TENANT_ROLE}"')
    connection.exec_driver_sql(
        "SELECT set_config(%s, %s, true)", (TENANT_GUC, str(tenant.id))
    )
```

> Postgres `SET LOCAL <name> = <value>` does **not** accept bind parameters (the value must be a literal/identifier). `set_config(setting, value, is_local)` is the function form and **does** take parameters, so the tenant id flows as data, never string-interpolated SQL. `is_local=true` matches `SET LOCAL` (cleared on commit/rollback). The `SET LOCAL ROLE` statement keeps the constant role name (identifiers cannot be bound).

- [ ] **Step 4: Run to verify passes.** SQLite test (no DB needed), then the Postgres-tier tests with the container up:

```bash
python -m pytest tests/db/test_session_tenant_hook.py::test_sqlite_session_issues_no_set_statements -q
$env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/test_ums"
python -m pytest tests/db/test_session_tenant_hook.py -q
```

Both → PASS. (The Postgres tests require Task 2's migration to have created the roles; if the test DB is fresh, run the migration test first — `python -m pytest tests/db/test_tenant_rls_migration.py -q` — which upgrades the DB to head via Alembic `command.upgrade`, or rely on it having run earlier in the suite.)

- [ ] **Step 5: Guard against import cycles + lint.** Confirm `python -c "import ums_smart_revenue.db.session"` imports cleanly (the `get_current_tenant` import is lazy inside the hook to avoid a cycle). Then:

```bash
python -m ruff check backend tests
git add backend/ums_smart_revenue/db/session.py tests/db/test_session_tenant_hook.py
git commit -m "feat(rls): context-gated tenant role/GUC session hook and platform lane"
```

---

## Task 4: TenantIsolationError + assert_tenant_match helper

**Files:**
- Create: `backend/ums_smart_revenue/tenancy/isolation.py`
- Test: `tests/tenancy/test_assert_tenant_match.py`

- [ ] **Step 1: Write failing tests:**

```python
import pytest
from uuid import UUID

from ums_smart_revenue.tenancy.isolation import (
    TenantIsolationError,
    assert_tenant_match,
)

A = UUID("00000000-0000-0000-0000-000000000001")
B = UUID("00000000-0000-0000-0000-000000000002")


def test_match_passes_for_equal_uuids():
    assert_tenant_match(A, A)  # no raise


def test_match_accepts_string_forms():
    assert_tenant_match(str(A), A)
    assert_tenant_match(A, str(A))


def test_mismatch_raises():
    with pytest.raises(TenantIsolationError):
        assert_tenant_match(A, B)


def test_none_raises():
    with pytest.raises(TenantIsolationError):
        assert_tenant_match(None, A)
```

- [ ] **Step 2: Run to verify failure.** `python -m pytest tests/tenancy/test_assert_tenant_match.py -q` → FAIL.

- [ ] **Step 3: Implement `backend/ums_smart_revenue/tenancy/isolation.py`:**

```python
"""Defense-in-depth tenant assertion for write paths.

RLS blocks cross-tenant writes at the DB; this raises a clear typed error
*before* the round-trip so the route boundary can return 403 instead of a raw
DB exception. Pair with the route translation added in api boundaries.
"""
from __future__ import annotations

from uuid import UUID


class TenantIsolationError(Exception):
    """Raised when a write targets a tenant other than the principal's."""


def _coerce(value: object) -> UUID | None:
    """Coerce a UUID-or-str to UUID, returning None if absent/invalid."""
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value).strip())
    except ValueError:
        return None


def assert_tenant_match(
    row_tenant_id: object, principal_tenant_id: object
) -> None:
    """Raise TenantIsolationError unless both ids resolve and are equal."""
    row = _coerce(row_tenant_id)
    principal = _coerce(principal_tenant_id)
    if row is None or principal is None or row != principal:
        raise TenantIsolationError(
            "tenant mismatch between row and principal"
        )
```

- [ ] **Step 4: Run to verify passes.** `python -m pytest tests/tenancy/test_assert_tenant_match.py -q` → PASS.

- [ ] **Step 5: Lint + commit.**

```bash
python -m ruff check backend tests
git add backend/ums_smart_revenue/tenancy/isolation.py tests/tenancy/test_assert_tenant_match.py
git commit -m "feat(tenancy): assert_tenant_match defense-in-depth helper"
```

---

## Task 5: Write-path enumeration + asserts (spec §2.6)

**Files:**
- Modify: enumerated write-path repositories/services (confirm each before editing).
- Modify: `Docs/superpowers/plans/2026-06-08-track-e-tenant-rls-source-currency.md` (fill the classification table below).
- Test: extend or add tests per `ASSERTED` path.

This task resolves every cell in the spec §2.6 table to `ASSERTED` or `COVERED-ELSEWHERE` **with file:line evidence**. "Probably fine" is not allowed.

- [ ] **Step 1: Re-confirm each write path.** For each row below, open the file, find the write method, and determine where `tenant_id` comes from (param, `self._tenant_id`, or derived from `get_current_tenant()`/principal). Record the file:line.

Starting inventory (verify; line numbers will have drifted):
- `finance/revenue_facts.py` `record_fact` — tenant from `self._tenant_id`.
- `finance/manual_overrides.py` `create_override`.
- `finance/committed_allocation.py` `commit_allocation` + recalc.
- `connectors/google_source_rows/repository.py` `upsert_many`.
- `finance/adsense_payments.py` payment-sync write.
- `finance/bank_reconciliation.py` write.
- `finance/month_close.py` close write.
- `finance/channel_account_links.py` propose/verify/reject.
- `finance/deduction_ingestion.py` write.
- report writes: `raw_report_files`, `export_jobs`.
- `finance/explanations.py` number-explanation write.
- `auth/sql_audit_sink.py` audit append.
- `auth/users.py` (+ scope/grant writes).
- `connectors/credentials.py` credential write.

- [ ] **Step 2: Classification rule.**
  - A write where `tenant_id` is taken from caller **input** that could differ from the principal → must be `ASSERTED`: call `assert_tenant_match(target_tenant_id, principal_tenant_id)` before the write, and translate `TenantIsolationError` → 403 at the route.
  - A write where `tenant_id` is **derived solely** from `get_current_tenant()`/`principal.tenant_id` at the only call site and never accepted from request input → `COVERED-ELSEWHERE`: document the exact mechanism + file:line. No code change, but add/cite a test asserting the derivation.

- [ ] **Step 3: For each `ASSERTED` path, write a failing test** proving a cross-tenant write raises `TenantIsolationError` (→ 403 at route). Example for revenue facts (adapt per repo):

```python
import pytest
from ums_smart_revenue.tenancy.isolation import TenantIsolationError


def test_record_fact_rejects_cross_tenant_target(revenue_repo_tenant_a):
    with pytest.raises(TenantIsolationError):
        revenue_repo_tenant_a.record_fact(
            month="2026-03",
            youtube_channel_id="chan-belongs-to-b",
            # ... minimal valid args ...
            actor_user_id="user-a",
            target_tenant_id="00000000-0000-0000-0000-000000000002",
        )
```

> Only add a `target_tenant_id`-style assertion where input can carry a tenant. Where the repo already pins tenant from context with no input override, classify `COVERED-ELSEWHERE` and write a test asserting the row's `tenant_id` equals the context tenant instead.

- [ ] **Step 4: Implement the asserts** in each `ASSERTED` path (insert `assert_tenant_match(...)` before the write) and ensure the owning route maps `TenantIsolationError` → `HTTPException(403)` (mirror `_require_permission`'s 403 shape).

- [x] **Step 5: Classification table (resolved 2026-06-08).**

**Result: every tenant-scoped write path is `COVERED-ELSEWHERE`. No
`assert_tenant_match` calls were added.** The honesty rule in §2.6 forbids
manufacturing self-comparison asserts. Investigation (live tree, 2026-06-08)
established a single uniform mechanism across all 25 tenant tables:

1. Every write repository pins its tenant once in `__init__` via
   `_resolve_tenant_id(tenant_id)` (explicit param → ambient
   `get_current_tenant()` → default UMS), storing `self._tenant_id`.
2. Every INSERT/UPDATE stamps `tenant_id=self._tenant_id` (or threads the same
   resolved value into child-table rows). No write *method* accepts a separate
   `target_tenant_id` from caller input.
3. At the only HTTP call sites, the FastAPI DI factories construct repositories
   with **`session` only** (e.g. `api/users.py:255,262,269`,
   `api/connectors.py:88`) — no request-body `tenant_id` is ever passed, so the
   row tenant and the principal tenant are the same `TENANT_CTX` value by
   construction.
4. The two write *methods* that take an explicit `tenant_id` arg
   (`GoogleRevenueSourceRowRepository.upsert_many` and the connector-run
   repository `start_run`/`record_raw_file`/`finish_run`) are reached only from
   the connector orchestrator `run_one` (`connectors/runs/orchestrator.py:367`),
   a CLI/orchestration entry — **not** an HTTP route — whose `tenant_id` is the
   orchestration-provided principal tenant, never divergent request input. It
   even self-guards cross-tenant reads (`orchestrator.py:918`,
   `repository.py:385`).

Because no write path accepts a tenant-bearing value from caller input that
could differ from the acting principal, the §2.6 starting inventory's `ASSERTED`
cells resolve to `COVERED-ELSEWHERE`. RLS (Task 2) + the context-gated session
GUC (Task 3) remain the DB-level enforcement; `assert_tenant_match` (Task 4)
stays available for any *future* write that does accept divergent tenant input.

| Write path | File:line (stamp) | Classification | Evidence / pinning test |
|---|---|---|---|
| `SqlAlchemyRevenueFactRepository.record_fact` (monthly_channel_revenue_facts) | finance/revenue_facts.py:157,291 | COVERED-ELSEWHERE | stamps `self._tenant_id` (resolved in `__init__` via `_resolve_tenant_id`, revenue_facts.py:346); ctor-tenant scoping proven by tests/finance/test_revenue_facts.py::test_revenue_fact_rejects_locked_month_in_bound_tenant |
| manual-override write (revenue_manual_overrides) | finance/manual_overrides.py:106,253 | COVERED-ELSEWHERE | stamps `self._tenant_id`; tests/finance/test_manual_overrides.py::test_manual_override_rejects_locked_month_in_bound_tenant |
| account-allocation commit (committed_allocation_runs + child lines) | finance/committed_allocation.py:219,308 | COVERED-ELSEWHERE | run + child lines stamp `self._tenant_id` (line 304 reads `link_repository.tenant_id`); tests/finance/test_committed_allocation.py::test_tenant_id_property_exposes_resolved_tenant + ::test_first_commit_creates_version_1 |
| account-allocation recalc (committed_allocation_runs) | finance/committed_allocation.py:219 | COVERED-ELSEWHERE | same repo/tenant path as commit; same tests |
| Google source-row `upsert_many` (google_revenue_source_rows) | connectors/google_source_rows/repository.py:451,480 | COVERED-ELSEWHERE | `tenant_id` arg threaded from orchestrator `run_one` (orchestrator.py:367/1138), a CLI/orchestration entry, never an HTTP request body; cross-tenant raw-file guard repository.py:893; tests/connectors/google_source_rows/test_repository.py::test_tenant_isolation, ::test_rejects_cross_tenant_raw_file |
| AdSense payment sync write (adsense_payments) | finance/adsense_payments.py:148,255 | COVERED-ELSEWHERE | `sync_payments` takes no tenant arg, stamps `self._tenant_id`; HTTP route api/adsense.py:153 passes no tenant; tests/finance/test_adsense_payments_tenant_scope.py::test_sync_payments_uses_request_tenant_context_by_default |
| bank-reconciliation write (bank_reconciliation_entries) | finance/bank_reconciliation.py:166,242 | COVERED-ELSEWHERE | `record_entry` takes no tenant arg, stamps `self._tenant_id`; tests/finance/test_bank_reconciliation_tenant_scope.py::test_record_entry_uses_request_tenant_context_by_default |
| finance month-close write (finance_month_close) | finance/month_close.py:131,182 | COVERED-ELSEWHERE | stamps `self._tenant_id` / `resolved_tenant_id` (= `_resolve_tenant_id`); tests/finance/test_month_close_locking.py::test_get_or_create_month_close_row_uses_request_tenant_context |
| channel-account-map write (adsense_content_owner_links, content_owner_channel_links) | finance/channel_account_links.py:322,645 | COVERED-ELSEWHERE | propose/verify/reject take no tenant arg, stamp `self._tenant_id`; tests/finance/test_channel_account_links.py::test_verify_marks_verified_and_stamps, ::test_read_contract_is_tenant_isolated |
| deduction-component write (deduction_components) | finance/deduction_ingestion.py:204 | COVERED-ELSEWHERE | stamps `self._tenant_id`; tests/finance/test_deduction_ingestion.py::test_repository_uses_request_tenant_context_by_default |
| raw-report-file write (raw_report_files) | reports/raw_files.py:99 | COVERED-ELSEWHERE | `register_file` takes no tenant arg, stamps `self._tenant_id`; tests/reports/test_raw_files.py::test_register_file_uses_ambient_tenant_context |
| export-job write (export_jobs) | reports/exports.py:180 | COVERED-ELSEWHERE | `request_export` takes no tenant arg, stamps `self._tenant_id`; tests/reports/test_export_jobs_repository.py::test_request_export_uses_ambient_tenant_context |
| number-explanation write (number_explanations) | finance/explanations.py:124 | COVERED-ELSEWHERE | `record_explanation` takes no tenant arg, stamps `self._tenant_id`; tests/finance/test_explanations.py::test_record_explanation_uses_ambient_tenant_context, ::test_record_explanation_isolates_writes_between_tenants |
| audit-log write (audit_logs) | auth/sql_audit_sink.py:43 | COVERED-ELSEWHERE | `append` stamps `self._tenant_id` (sink resolved from principal tenant at construction, sql_audit_sink.py:76); tests/auth/test_audit_tenant_scope.py::test_audit_repository_uses_request_tenant_context_by_default |
| user-account write (users) | auth/users.py:159 | COVERED-ELSEWHERE | `create_user` takes no tenant arg, stamps `self._tenant_id` (require_current_tenant fail-closed, users.py:587); tests/auth/test_user_account_tenant_scope.py::test_user_repository_persists_current_tenant_on_create |
| role-assignment write (user_role_assignments) | auth/user_roles.py:138,314 | COVERED-ELSEWHERE | `assign_role` takes no tenant arg, stamps `self._tenant_id`; tests/auth/test_user_roles_repository.py |
| permission-grant write (user_permission_grants) | auth/user_permissions.py:177,340 | COVERED-ELSEWHERE | `grant_permission` takes no tenant arg, stamps `self._tenant_id`; tests/auth/test_user_permissions_repository.py |
| access-scope write (access_scopes) | auth/user_roles.py:314 (`_get_or_create_scope`); same pattern in auth/user_permissions.py | COVERED-ELSEWHERE | scope rows stamp `tenant_id=self._tenant_id` and are read-filtered to it (user_roles.py:300); reached only via role/permission writes (no tenant arg); covered by the role/permission repository tests above |
| connector-credential write (api_connector_credentials) | connectors/credentials.py:142 | COVERED-ELSEWHERE | `create_credential` takes no tenant arg, stamps `self._tenant_id`; cross-tenant actor rejected (tests/connectors/test_credentials.py::test_create_credential_rejects_actor_user_from_another_tenant); ::test_create_credential_uses_ambient_tenant_context |
| org-unit / channel write (org_units, youtube_channels) | org/sql_channel_registry.py:83 | COVERED-ELSEWHERE | stamps `self._tenant_id`; cross-tenant company_id rejected (tests/org/test_sql_channel_registry.py::test_sql_channel_registry_rejects_cross_tenant_company_id); ::test_sql_channel_registry_explicit_tenant_writes_are_isolated |
| channel-group / member write (channel_groups, channel_group_members) | org/sql_channel_groups.py:59,105,128,163 | COVERED-ELSEWHERE | group + member rows stamp `self._tenant_id`; cross-tenant channel rejected (tests/org/test_sql_channel_groups.py::test_create_group_rejects_cross_tenant_channel_external_id); ::test_create_group_uses_request_tenant_context_by_default |
| connector-run write (connector_runs) | connectors/runs/repository.py:121,200 | COVERED-ELSEWHERE | `start_run`/`finish_run` `tenant_id` arg threaded from orchestrator `run_one` (CLI/orchestration, not HTTP request body); cross-tenant guards repository.py:347,385; tests/connectors/runs/test_repository.py |
| connector-run raw-file link write (connector_run_raw_files) | connectors/runs/repository.py:162 | COVERED-ELSEWHERE | `record_raw_file` re-fetches run + raw file under the same `tenant_id` (repository.py:156-157) before insert; same orchestration-tenant provenance; tests/connectors/runs/test_repository.py |

- [ ] **Step 6: Run targeted tests + lint + commit.**

```bash
python -m pytest tests/finance tests/api -q -k "tenant or isolation"
python -m ruff check backend tests
git add -A
git commit -m "feat(tenancy): enumerate and assert tenant-scoped write paths"
```

---

## Task 6: Source-rows read repository

**Files:**
- Create: `backend/ums_smart_revenue/finance/source_rows_read.py`
- Test: `tests/finance/test_source_rows_read_repository.py`

Mirror the connector-runs read pattern (`ConnectorRunPage`/`ConnectorRunEntry`/`list_runs`). Order `(ingested_at DESC, id DESC)`; both-or-neither cursor; `limit+1`; `MAX_SOURCE_ROW_PAGE_SIZE = 100`. **`raw_payload` is never projected** into the entry.

- [ ] **Step 1: Write failing tests** (`tests/finance/test_source_rows_read_repository.py`). Use the SQLite fixture pattern from `tests/finance/test_google_source_normalizer_service.py` (create all relevant `Base.metadata`, seed `TenantORM`, `CurrencyORM`, `GoogleRevenueSourceRowORM`).

```python
import pytest
from uuid import UUID

from ums_smart_revenue.finance.source_rows_read import (
    MAX_SOURCE_ROW_PAGE_SIZE,
    SourceRowValidationError,
    get_source_row,
    list_source_rows,
)

A = UUID("00000000-0000-0000-0000-000000000001")
B = UUID("00000000-0000-0000-0000-000000000002")


def test_list_filters_by_tenant_and_month(session, seed_rows):
    page = list_source_rows(session, tenant_id=A, month="2026-03", limit=50)
    assert all(e.report_month == "2026-03" for e in page.items)
    # No tenant B row leaks.
    assert all(e for e in page.items)  # entries carry no tenant_id field


def test_entry_never_exposes_raw_payload(session, seed_rows):
    page = list_source_rows(session, tenant_id=A, month="2026-03", limit=50)
    api = page.items[0].to_api()
    assert "raw_payload" not in api
    assert api["raw_payload_redacted"] is True
    assert "tenant_id" not in api


def test_source_system_filter(session, seed_rows):
    page = list_source_rows(
        session, tenant_id=A, month="2026-03",
        source_system="adsense_management", limit=50,
    )
    assert all(e.source_system == "adsense_management" for e in page.items)


def test_half_cursor_raises(session):
    with pytest.raises(SourceRowValidationError):
        list_source_rows(
            session, tenant_id=A, month="2026-03",
            cursor_ingested_at=None, cursor_id="x", limit=50,
        )


def test_limit_out_of_range_raises(session):
    with pytest.raises(SourceRowValidationError):
        list_source_rows(session, tenant_id=A, month="2026-03", limit=0)
    with pytest.raises(SourceRowValidationError):
        list_source_rows(
            session, tenant_id=A, month="2026-03",
            limit=MAX_SOURCE_ROW_PAGE_SIZE + 1,
        )


def test_get_returns_none_for_other_tenant(session, seed_rows):
    # An id owned by tenant B is invisible to tenant A (=> route 404).
    assert get_source_row(session, tenant_id=A, row_id=seed_rows["b_id"]) is None


def test_pagination_has_more_and_cursor(session, seed_many):
    page = list_source_rows(session, tenant_id=A, month="2026-03", limit=2)
    assert len(page.items) == 2
    assert page.next_cursor is not None
    nxt = list_source_rows(
        session, tenant_id=A, month="2026-03", limit=2,
        cursor_ingested_at=page.next_cursor["ingested_at"],
        cursor_id=page.next_cursor["id"],
    )
    assert {e.id for e in nxt.items}.isdisjoint({e.id for e in page.items})
```

> Implementer writes the `session`, `seed_rows`, `seed_many` fixtures following `test_google_source_normalizer_service.py`. `cursor_ingested_at` may be passed as ISO string from the API; accept `str | datetime` and parse (raise `SourceRowValidationError` on bad value), mirroring connector-runs cursor handling.

- [ ] **Step 2: Run to verify failure.** `python -m pytest tests/finance/test_source_rows_read_repository.py -q` → FAIL.

- [ ] **Step 3: Implement `finance/source_rows_read.py`** mirroring `connectors/runs/repository.py`:

```python
"""Read-only, tenant-scoped access to google_revenue_source_rows.

Mirrors the connector-runs keyset read pattern. raw_payload is never projected
into the API entry (spec §3.3: never returned in this PR for any caller).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from ums_smart_revenue.db.source_models import GoogleRevenueSourceRowORM

MAX_SOURCE_ROW_PAGE_SIZE = 100

_VALID_SOURCE_SYSTEMS = frozenset(
    {"youtube_reporting", "youtube_analytics", "adsense_management"}
)


class SourceRowReadError(Exception):
    """Base error for source-row reads."""


class SourceRowValidationError(SourceRowReadError):
    """Invalid filter, limit, or cursor for a source-row read."""


@dataclass(frozen=True)
class SourceRowEntry:
    """Immutable source row projected into the read API (no raw_payload)."""

    id: str
    source_system: str
    source_account_id: str
    content_owner_id: str | None
    youtube_channel_id: str | None
    report_type: str
    report_month: str
    period_start: date
    period_end: date
    metric_key: str
    value_kind: str
    amount_native: str
    currency_code: str
    source_report_id: str | None
    ingested_at: datetime

    def to_api(self) -> dict[str, object]:
        """Serialize to the stable API shape; raw_payload always redacted."""
        return {
            "id": self.id,
            "source_system": self.source_system,
            "source_account_id": self.source_account_id,
            "content_owner_id": self.content_owner_id,
            "youtube_channel_id": self.youtube_channel_id,
            "report_type": self.report_type,
            "report_month": self.report_month,
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "metric_key": self.metric_key,
            "value_kind": self.value_kind,
            "amount_native": self.amount_native,
            "currency_code": self.currency_code,
            "source_report_id": self.source_report_id,
            "ingested_at": self.ingested_at.isoformat(),
            "raw_payload_redacted": True,
        }


def _to_entry(row: GoogleRevenueSourceRowORM) -> SourceRowEntry:
    """Project an ORM row into the immutable entry (decimal -> str)."""
    return SourceRowEntry(
        id=str(row.id),
        source_system=row.source_system,
        source_account_id=row.source_account_id,
        content_owner_id=row.content_owner_id,
        youtube_channel_id=row.youtube_channel_id,
        report_type=row.report_type,
        report_month=row.report_month,
        period_start=row.period_start,
        period_end=row.period_end,
        metric_key=row.metric_key,
        value_kind=row.value_kind,
        amount_native=format(row.amount_native, "f"),
        currency_code=row.currency_code,
        source_report_id=row.source_report_id,
        ingested_at=row.ingested_at,
    )


@dataclass(frozen=True)
class SourceRowPage:
    """One page of source rows plus its keyset cursor."""

    items: list[SourceRowEntry]
    limit: int
    next_cursor: dict[str, str] | None


def _parse_cursor_dt(value: str | datetime) -> datetime:
    """Parse the cursor ingested_at (ISO str or datetime) or raise."""
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise SourceRowValidationError("cursor_ingested_at must be ISO-8601") from exc


def _parse_cursor_uuid(value: str) -> UUID:
    """Parse the cursor id UUID or raise."""
    try:
        return UUID(value)
    except ValueError as exc:
        raise SourceRowValidationError("cursor_id must be a valid UUID") from exc


def _next_cursor(items: list[SourceRowEntry]) -> dict[str, str] | None:
    """Build the next cursor from the last entry of a full page."""
    if not items:
        return None
    last = items[-1]
    return {"ingested_at": last.ingested_at.isoformat(), "id": last.id}


def list_source_rows(
    session: Session,
    *,
    tenant_id: UUID,
    month: str,
    source_system: str | None = None,
    cursor_ingested_at: str | datetime | None = None,
    cursor_id: str | None = None,
    limit: int,
) -> SourceRowPage:
    """List tenant-scoped source rows for a month, newest-first, keyset-paged."""
    if limit < 1 or limit > MAX_SOURCE_ROW_PAGE_SIZE:
        raise SourceRowValidationError(
            f"limit must be between 1 and {MAX_SOURCE_ROW_PAGE_SIZE}"
        )
    if (cursor_ingested_at is None) != (cursor_id is None):
        raise SourceRowValidationError(
            "cursor_ingested_at and cursor_id must be provided together"
        )
    if source_system is not None and source_system not in _VALID_SOURCE_SYSTEMS:
        raise SourceRowValidationError("invalid source_system")

    orm = GoogleRevenueSourceRowORM
    stmt = (
        sa.select(orm)
        .where(orm.tenant_id == tenant_id, orm.report_month == month)
        .order_by(orm.ingested_at.desc(), orm.id.desc())
    )
    if source_system is not None:
        stmt = stmt.where(orm.source_system == source_system)
    if cursor_ingested_at is not None and cursor_id is not None:
        cur_dt = _parse_cursor_dt(cursor_ingested_at)
        cur_id = _parse_cursor_uuid(cursor_id)
        stmt = stmt.where(
            sa.or_(
                orm.ingested_at < cur_dt,
                sa.and_(orm.ingested_at == cur_dt, orm.id < cur_id),
            )
        )
    rows = session.scalars(stmt.limit(limit + 1)).all()
    items = [_to_entry(r) for r in rows[:limit]]
    has_more = len(rows) > limit
    return SourceRowPage(
        items=items, limit=limit,
        next_cursor=_next_cursor(items) if has_more else None,
    )


def get_source_row(
    session: Session, *, tenant_id: UUID, row_id: str
) -> SourceRowEntry | None:
    """Return one tenant-scoped source row, or None if absent/cross-tenant."""
    try:
        parsed = UUID(row_id)
    except ValueError as exc:
        raise SourceRowValidationError("id must be a valid UUID") from exc
    orm = GoogleRevenueSourceRowORM
    row = session.scalars(
        sa.select(orm).where(orm.id == parsed, orm.tenant_id == tenant_id)
    ).first()
    return _to_entry(row) if row is not None else None
```

- [ ] **Step 4: Run to verify passes.** `python -m pytest tests/finance/test_source_rows_read_repository.py -q` → PASS.

- [ ] **Step 5: Lint + commit.**

```bash
python -m ruff check backend tests
git add backend/ums_smart_revenue/finance/source_rows_read.py tests/finance/test_source_rows_read_repository.py
git commit -m "feat(source-rows): tenant-scoped read repository with keyset pagination"
```

---

## Task 7: Source-rows read API + router registration

**Files:**
- Create: `backend/ums_smart_revenue/api/source_rows.py`
- Modify: `backend/ums_smart_revenue/app.py`
- Test: `tests/api/test_source_rows_api.py`

Routes live under the `/revenue` prefix: `GET /revenue/source-rows` and `GET /revenue/source-rows/{id}`. Gate on `Permission.VIEW_REVENUE` (the same gate guarding revenue-fact reads — confirm in `api/revenue.py`). Cross-tenant `{id}` → 404. No audit emission (read-only list, mirrors connector-runs).

- [ ] **Step 1: Write failing route tests** (`tests/api/test_source_rows_api.py`). Follow the existing `tests/api/test_connectors_api.py` TestClient + principal-header pattern. Cover: 403 without `VIEW_REVENUE`; tenant isolation (A can't see B); `month`/`source_system` filters honored; `{id}` 404 for cross-tenant; `raw_payload` absent in list and detail; half-cursor → 422.

```python
def test_requires_view_revenue(client_without_revenue_perm):
    resp = client_without_revenue_perm.get("/revenue/source-rows?month=2026-03")
    assert resp.status_code == 403


def test_lists_only_own_tenant_rows(client_tenant_a, seed_two_tenants):
    resp = client_tenant_a.get("/revenue/source-rows?month=2026-03")
    assert resp.status_code == 200
    body = resp.json()
    assert all("raw_payload" not in item for item in body["items"])
    assert "tenant_id" not in body["items"][0]


def test_detail_cross_tenant_is_404(client_tenant_a, seed_two_tenants):
    other = seed_two_tenants["b_row_id"]
    resp = client_tenant_a.get(f"/revenue/source-rows/{other}")
    assert resp.status_code == 404


def test_detail_redacts_raw_payload(client_tenant_a, seed_two_tenants):
    own = seed_two_tenants["a_row_id"]
    resp = client_tenant_a.get(f"/revenue/source-rows/{own}")
    assert resp.status_code == 200
    assert "raw_payload" not in resp.json()
    assert resp.json()["raw_payload_redacted"] is True


def test_half_cursor_is_422(client_tenant_a):
    resp = client_tenant_a.get(
        "/revenue/source-rows?month=2026-03&cursor_id=x"
    )
    assert resp.status_code == 422
```

- [ ] **Step 2: Run to verify failure.** `python -m pytest tests/api/test_source_rows_api.py -q` → FAIL (route missing / 404).

- [ ] **Step 3: Implement `api/source_rows.py`:**

```python
"""Read-only Google source-rows API (spec §3). Finance-gated, tenant-scoped,
raw_payload never returned."""
from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.finance.source_rows_read import (
    MAX_SOURCE_ROW_PAGE_SIZE,
    SourceRowValidationError,
    get_source_row,
    list_source_rows,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

router = APIRouter(prefix="/revenue", tags=["revenue"])


def _require_view_revenue(user: UserPrincipal) -> None:
    """Raise 403 unless the principal can view revenue (global scope)."""
    if not has_permission(user, Permission.VIEW_REVENUE, AccessScope.global_scope()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.VIEW_REVENUE.value}",
        )


def _tenant_uuid(user: UserPrincipal) -> UUID:
    """Resolve the principal's tenant UUID (fallback to UMS for pre-S2.4)."""
    return UUID(user.tenant_id) if user.tenant_id else UUID(UMS_TENANT_ID)


@router.get("/source-rows")
def list_revenue_source_rows(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    session: Annotated[Session, Depends(current_db_session)],
    month: str,
    source_system: str | None = None,
    cursor_ingested_at: str | None = None,
    cursor_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_SOURCE_ROW_PAGE_SIZE)] = 50,
) -> dict[str, object]:
    """Return a newest-first page of tenant-scoped Google source rows."""
    _require_view_revenue(user)
    try:
        page = list_source_rows(
            session,
            tenant_id=_tenant_uuid(user),
            month=month,
            source_system=source_system,
            cursor_ingested_at=cursor_ingested_at,
            cursor_id=cursor_id,
            limit=limit,
        )
    except SourceRowValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    items = [e.to_api() for e in page.items]
    return {
        "items": items,
        "pagination": {
            "limit": page.limit,
            "returned": len(items),
            "has_more": page.next_cursor is not None,
            "next_cursor": page.next_cursor,
        },
    }


@router.get("/source-rows/{row_id}")
def get_revenue_source_row(
    row_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    session: Annotated[Session, Depends(current_db_session)],
) -> dict[str, object]:
    """Return one tenant-scoped source row; 404 if absent or cross-tenant."""
    _require_view_revenue(user)
    try:
        entry = get_source_row(
            session, tenant_id=_tenant_uuid(user), row_id=row_id
        )
    except SourceRowValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="source row not found"
        )
    return entry.to_api()
```

> Import paths verified against live tree (2026-06-08): `has_permission` in `ums_smart_revenue.auth.policy`; `AccessScope.global_scope()` in `ums_smart_revenue.auth.scopes`; `UMS_TENANT_ID` in `tenancy/constants.py`. Confirm `current_db_session` / `current_principal_from_headers` import locations in `api/dependencies.py`. If the codebase's revenue reads use a scope narrower than global, mirror that scope here; otherwise global read is correct for a tenant-wide month listing.

- [ ] **Step 4: Register the router in `app.py`.** Add the import near the other `api.*` imports and `_app.include_router(source_rows_router)` in `create_app`:

```python
from ums_smart_revenue.api.source_rows import router as source_rows_router
# ...
_app.include_router(source_rows_router)
```

- [ ] **Step 5: Run to verify passes.** `python -m pytest tests/api/test_source_rows_api.py -q` → PASS.

- [ ] **Step 6: Lint + commit.**

```bash
python -m ruff check backend tests
git add backend/ums_smart_revenue/api/source_rows.py backend/ums_smart_revenue/app.py tests/api/test_source_rows_api.py
git commit -m "feat(source-rows): GET /revenue/source-rows list + detail API"
```

---

## Task 8: Tenant isolation matrix (Postgres-only, run as app_tenant)

**Files:**
- Create: `tests/tenancy/test_isolation.py`

Proves the DB boundary holds with app-level filters deliberately bypassed. Connects via `require_postgres_url`, runs migrations to head, then `SET ROLE app_tenant` and exercises raw queries.

- [ ] **Step 1: Write the matrix test:**

```python
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from tests.db._postgres_helpers import require_postgres_url

A = "00000000-0000-0000-0000-000000000001"
B = "00000000-0000-0000-0000-000000000002"


def _upgrade(url: str) -> None:
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")


def test_app_tenant_cannot_read_other_tenant_rows():
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            # Seed tenant B + a B-owned org_unit as owner (RLS bypassed here).
            conn.execute(sa.text(
                "INSERT INTO tenants (id, slug, display_name, primary_currency) "
                "VALUES (:id, 'rotana', 'Rotana', 'USD') ON CONFLICT DO NOTHING"
            ), {"id": B})
            # ... seed one org_units row for A and one for B ...
        with engine.connect() as conn:
            conn.execute(sa.text('SET ROLE "app_tenant"'))
            # set_config(name, value, is_local=false) -> session-scoped GUC;
            # parameterized so the tenant id flows as data, not literal SQL.
            conn.execute(
                sa.text("SELECT set_config('app.current_tenant_id', :a, false)"),
                {"a": A},
            )
            # Bare select, NO WHERE tenant_id — RLS must filter to A only.
            rows = conn.execute(sa.text("SELECT tenant_id FROM org_units")).scalars().all()
            assert all(str(t) == A for t in rows)
    finally:
        engine.dispose()


def test_with_check_blocks_cross_tenant_insert():
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text('SET ROLE "app_tenant"'))
            conn.execute(
                sa.text("SELECT set_config('app.current_tenant_id', :a, false)"),
                {"a": A},
            )
            with pytest.raises(Exception):
                # Inserting a B-owned row while GUC=A violates WITH CHECK.
                conn.execute(sa.text(
                    "INSERT INTO org_units (id, tenant_id, ...) "
                    "VALUES (gen_random_uuid(), :b, ...)"
                ), {"b": B})
                conn.commit()
    finally:
        engine.dispose()


def test_missing_guc_fails_closed():
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text('SET ROLE "app_tenant"'))
            # No GUC set: current_setting without missing_ok must error.
            with pytest.raises(Exception):
                conn.execute(sa.text("SELECT * FROM org_units")).all()
    finally:
        engine.dispose()


def test_app_platform_reads_across_tenants():
    url = require_postgres_url()
    _upgrade(url)
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(sa.text('SET ROLE "app_platform"'))
            # BYPASSRLS: no GUC needed; can see all tenants.
            conn.execute(sa.text("SELECT COUNT(*) FROM org_units")).scalar()
    finally:
        engine.dispose()
```

> Implementer completes the `INSERT` column lists for `org_units` from its real schema (read `org` models). Add `import pytest`. Pick `org_units` (or another simple tenant table) as the representative table; one representative table is sufficient for the matrix since the policy template is identical across tables (the migration test already proves every table has a policy).

- [ ] **Step 2: Run to verify (then implement-fix as needed).**

```bash
$env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/test_ums"
python -m pytest tests/tenancy/test_isolation.py -q
```

Iterate on the seed SQL until all four pass. (If they pass immediately, that confirms Tasks 2–3 are correct end-to-end.)

- [ ] **Step 3: Lint + commit.**

```bash
python -m ruff check backend tests
git add tests/tenancy/test_isolation.py
git commit -m "test(rls): Postgres tenant-isolation matrix as app_tenant"
```

---

## Task 9: Docs + full validation gate

**Files:**
- Modify: `Docs/17_MULTI_TENANT_ARCHITECTURE.md`, `Docs/18_MULTI_CURRENCY_ENGINE.md`, `Docs/12_BACKEND_API_SPEC.md`, `Docs/01_IMPLEMENTATION_PLAN.md`, `Docs/15_DELIVERY_BACKLOG.md`

- [ ] **Step 1: Update Docs/17** — mark RLS enforcement + the two roles as implemented; document the single-pool `SET LOCAL ROLE` realization, the context-gated Postgres-only hook, the `INHERIT FALSE, SET TRUE` login membership requirement, the deploy precondition, and the FORCE-RLS follow-up note.

- [ ] **Step 2: Update Docs/18** — mark the source-rows read API as built; document `raw_payload` is never returned; restate the paired-column `*_usd`→native migration remains a separate future spec.

- [ ] **Step 3: Update Docs/12** — document `GET /revenue/source-rows` and `GET /revenue/source-rows/{id}` (query params, gate, envelope, redaction, 404/422).

- [ ] **Step 4: Update Docs/01 + Docs/15** — per-PR status: S3 RLS enforcement DONE, B1 source-rows read API DONE, paired-column migration still PENDING (separate spec).

- [ ] **Step 5: Doc hygiene.**

```bash
git diff --check
```

- [ ] **Step 6: Full validation gate** (Postgres container up so RLS/migration/isolation tests run):

```bash
python -m ruff check backend tests scripts
git diff --check
$env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:ums@localhost:55432/test_ums"
python -m pytest -q
```

Expected: ruff clean, diff-check clean, full suite green (existing count + the new tests). If any pre-existing failure appears, prove it is unrelated pre-existing debt or stop and report per CLAUDE.md.

- [ ] **Step 7: Commit docs.**

```bash
git add Docs/12_BACKEND_API_SPEC.md Docs/17_MULTI_TENANT_ARCHITECTURE.md Docs/18_MULTI_CURRENCY_ENGINE.md Docs/01_IMPLEMENTATION_PLAN.md Docs/15_DELIVERY_BACKLOG.md
git commit -m "docs(track-e): record RLS enforcement + source-rows read API"
```

---

## Self-Review (completed by plan author)

**1. Spec coverage:**
- §2.1 two roles + INHERIT FALSE/SET TRUE membership → Task 2 (roles), Task 9 doc (membership/deploy). ✓
- §2.2 all tenant_id tables + drift guard → Task 1 (allowlist) + Task 2 (`_assert_no_drift`). ✓
- §2.3 policy template + fail-closed → Task 2. ✓
- §2.4 Postgres-only, context-gated hook → Task 3. ✓
- §2.5 platform lane → Task 3 (`build_platform_session_factory`). ✓
- §2.6 write-path enumeration ASSERTED/COVERED-ELSEWHERE → Task 4 (helper) + Task 5 (sweep + table). ✓
- §2.7 isolation matrix incl. pooled-reset (#7) → Task 8 + Task 3 pooled-reset test. ✓
- §3 source-rows read API + redaction + 404 → Tasks 6–7. ✓
- §5 blast radius / deploy precondition → Task 2 docstring + Task 9. ✓
- §6 testing contract → tests across Tasks 2,3,4,5,6,7,8. ✓
- §7 docs → Task 9. ✓
- OUT-of-scope (paired-column migration, FORCE RLS) → not built; restated in Task 9. ✓

**2. Placeholder scan:** The `TENANT_SCOPED_TABLES` body and the §2.6 classification table are *derivation steps with concrete methods/commands*, not placeholders — each has an exact procedure (Step 1 enumeration query; Task 5 Step 1 per-file confirmation). Import paths in Task 7 are flagged "confirm against live tree" with the anchor file named. No bare TODO/TBD remains.

**3. Type/name consistency:** `TENANT_SCOPED_TABLES`, `APP_TENANT_ROLE`, `APP_PLATFORM_ROLE`, `TENANT_GUC`, `tenant_rls_policy_name`, `discover_tenant_tables_sql` (Task 1) are reused verbatim in Tasks 2–3. `SourceRowEntry`/`SourceRowPage`/`list_source_rows`/`get_source_row`/`MAX_SOURCE_ROW_PAGE_SIZE`/`SourceRowValidationError` (Task 6) are reused verbatim in Task 7. `assert_tenant_match`/`TenantIsolationError` (Task 4) reused in Task 5. Consistent.
