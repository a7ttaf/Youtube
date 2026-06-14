"""FORCE tenant Row-Level Security so table OWNERS are also policy-subject.

Revision ID: 20260612_0002
Revises: 20260612_0001
Create Date: 2026-06-14

Postgres-only in effect (SQLite has no RLS; the body is dialect-guarded and
no-ops there). The 20260608_0001 migration ENABLEd RLS + an isolation policy on
every tenant-scoped table, which subjects non-owner roles (app_tenant /
app_platform are NOLOGIN + NOBYPASSRLS non-owners, so they were already bound).
But a table's OWNER bypasses RLS by default; a non-superuser owner would silently
read across the policy. This migration adds FORCE ROW LEVEL SECURITY to every
tenant table so the owner is bound too — closing the owner-bypass gap pre-planned
in Docs/17_MULTI_TENANT_ARCHITECTURE.md ("FORCE ROW LEVEL SECURITY follow-up").

Observability note: a SUPERUSER and any BYPASSRLS role still bypass FORCE — only
a non-superuser, non-bypass owner is newly subject. In the disposable test DB the
login is the postgres superuser that owns the tables, so FORCE changes nothing it
can observe; existing PG RLS tests are unaffected. The behavioural proof lives in
tests/tenancy/test_force_rls.py via a throwaway non-superuser owner.

The same drift primitive the ENABLE migration uses (db.rls.discover_tenant_tables_sql
scanning the live schema vs db.rls.TENANT_SCOPED_TABLES) fails this migration if
the live tenant_id table set != the allowlist, so a new tenant table cannot ship
un-FORCEd. The canonical list and the discovery SQL stay single-sourced in
db.rls; only the small comparison is local (the ENABLE migration's own
_assert_no_drift is a module-private helper inside that revision, not importable
without a fragile digit-prefixed cross-migration import). One difference from the
ENABLE migration's guard: it runs its check BEFORE creating the backend-context
table, whereas this revision runs after it exists, so the scan also returns
``app_tenant_context`` (a tenant_id-bearing backend-PID helper, NOT a tenant data
table). We subtract it via db.rls.TENANT_CONTEXT_TABLE — it is intentionally not
FORCEd and intentionally absent from TENANT_SCOPED_TABLES.

Rollback: drop FORCE on every tenant table (NO FORCE). RLS stays ENABLED and the
isolation policies stay in place — this revision owns only the FORCE flag.
"""

import sqlalchemy as sa
from alembic import op

from ums_smart_revenue.db.rls import (
    TENANT_CONTEXT_TABLE,
    TENANT_SCOPED_TABLES,
    discover_tenant_tables_sql,
)

revision = "20260612_0002"
down_revision = "20260612_0001"
branch_labels = None
depends_on = None


def _assert_no_drift(bind) -> None:
    """Fail if the live tenant_id table set != the db.rls allowlist constant.

    Same fail-closed contract as the ENABLE migration (20260608_0001): a new
    tenant_id table that is not in db.rls.TENANT_SCOPED_TABLES becomes a loud
    migration failure here, so it cannot ship un-FORCEd. The list and the
    discovery SQL are imported from db.rls (single source of truth).

    Unlike the ENABLE migration — whose drift call runs BEFORE it creates the
    backend-context table — this revision runs AFTER 20260608_0001, so the live
    scan also returns ``app_tenant_context``. That table carries a ``tenant_id``
    column but is a backend-PID context helper, not a tenant-scoped data table
    (it is deliberately absent from TENANT_SCOPED_TABLES and must NOT be FORCEd),
    so it is subtracted here via the db.rls.TENANT_CONTEXT_TABLE constant. A
    genuinely new, unexpected tenant_id data table still trips the guard.
    """
    live = set(bind.execute(sa.text(discover_tenant_tables_sql())).scalars())
    live.discard(TENANT_CONTEXT_TABLE)
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


# ============================================================================
# Purpose: FORCE RLS on every tenant-scoped table so the table OWNER is also
#          subject to the Track-E isolation policies (ENABLE alone leaves a
#          non-superuser owner bypassing). Closes the pre-planned owner-bypass
#          gap; the drift guard keeps the FORCE set == the allowlist.
# Database/ORM: PostgreSQL only; the 25 db.rls.TENANT_SCOPED_TABLES. No ORM
#          models — raw ALTER TABLE ... FORCE ROW LEVEL SECURITY.
# Standards: Dialect-guarded (no-op off Postgres); table names are internal
#          constants, not user input; idempotent (FORCE is set-state, re-running
#          is harmless). Reuses the db.rls drift primitive (TENANT_SCOPED_TABLES
#          + discover_tenant_tables_sql) so a new tenant table becomes a loud
#          migration failure, not a silent un-FORCEd leak.
# Blast Radius: Authorization / tenant isolation at the DB owner boundary.
#          No finance math, audit-write, export, or Neo4j impact. Superuser /
#          BYPASSRLS connections (incl. the postgres test login) are unaffected.
# Connections:
#   - File: backend/ums_smart_revenue/db/rls.py -> TENANT_SCOPED_TABLES list +
#     discover_tenant_tables_sql (single source of truth for the drift guard).
#   - File: backend/ums_smart_revenue/db/alembic/versions/
#     20260608_0001_tenant_rls_enforcement.py -> the ENABLE + policy migration
#     this revision hardens with FORCE.
#   - File: Docs/17_MULTI_TENANT_ARCHITECTURE.md -> "FORCE ROW LEVEL SECURITY
#     follow-up" pre-plan.
# ============================================================================
def upgrade() -> None:
    """FORCE RLS on every tenant-scoped table (Postgres only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _assert_no_drift(bind)
    for table in TENANT_SCOPED_TABLES:
        # Table names are internal constants from the vetted allowlist.
        bind.execute(
            sa.text(f"ALTER TABLE public.\"{table}\" FORCE ROW LEVEL SECURITY")
        )


def downgrade() -> None:
    """Drop the FORCE flag on every tenant table; RLS stays ENABLED."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in TENANT_SCOPED_TABLES:
        bind.execute(
            sa.text(f"ALTER TABLE public.\"{table}\" NO FORCE ROW LEVEL SECURITY")
        )
