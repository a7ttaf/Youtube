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

The same drift primitive the ENABLE migration uses
(db.rls.discover_tenant_tables_sql scanning the live schema) fails this migration
if the live tenant_id table set != this revision's table snapshot, so this
revision cannot silently skip one of its owned tables. One difference from the
ENABLE migration's guard: it runs its check BEFORE creating the backend-context
table, whereas this revision runs after it exists, so the scan also returns
``app_tenant_context`` (a tenant_id-bearing backend-PID helper, NOT a tenant data
table). We subtract it via db.rls.TENANT_CONTEXT_TABLE.

Rollback: drop FORCE on every tenant table (NO FORCE). RLS stays ENABLED and the
isolation policies stay in place — this revision owns only the FORCE flag.
"""

import sqlalchemy as sa
from alembic import op

from ums_smart_revenue.db.rls import (
    TENANT_CONTEXT_TABLE,
    discover_tenant_tables_sql,
)

revision = "20260612_0002"
down_revision = "20260612_0001"
branch_labels = None
depends_on = None

_REVISION_TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "access_scopes",
    "adsense_content_owner_links",
    "adsense_payments",
    "api_connector_credentials",
    "audit_logs",
    "bank_reconciliation_entries",
    "channel_group_members",
    "channel_groups",
    "committed_allocation_runs",
    "connector_run_raw_files",
    "connector_runs",
    "content_owner_channel_links",
    "deduction_components",
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


def _assert_no_drift(bind) -> None:
    """Fail if the live tenant_id table set != the db.rls allowlist constant.

    Same fail-closed contract as the ENABLE migration (20260608_0001): this
    revision fails if its live schema and revision-time table snapshot diverge.

    Unlike the ENABLE migration — whose drift call runs BEFORE it creates the
    backend-context table — this revision runs AFTER 20260608_0001, so the live
    scan also returns ``app_tenant_context``. That table carries a ``tenant_id``
    column but is a backend-PID context helper, not a tenant-scoped data table,
    so it is subtracted here via the db.rls.TENANT_CONTEXT_TABLE constant.
    """
    live = set(bind.execute(sa.text(discover_tenant_tables_sql())).scalars())
    live.discard(TENANT_CONTEXT_TABLE)
    expected = set(_REVISION_TENANT_SCOPED_TABLES)
    if live != expected:
        missing = expected - live
        extra = live - expected
        raise RuntimeError(
            "Tenant RLS allowlist drift. "
            f"In allowlist but not in schema: {sorted(missing)}; "
            f"in schema but not in allowlist: {sorted(extra)}. "
            "Update this revision's tenant table snapshot or db.rls.TENANT_SCOPED_TABLES."
        )


# ============================================================================
# Purpose: FORCE RLS on every tenant-scoped table so the table OWNER is also
#          subject to the Track-E isolation policies (ENABLE alone leaves a
#          non-superuser owner bypassing). Closes the pre-planned owner-bypass
#          gap; the drift guard keeps the FORCE set == the allowlist.
# Database/ORM: PostgreSQL only; this revision's tenant table snapshot. No ORM
#          models - raw ALTER TABLE ... FORCE ROW LEVEL SECURITY.
# Standards: Dialect-guarded (no-op off Postgres); table names are internal
#          constants, not user input; idempotent (FORCE is set-state, re-running
#          is harmless). Reuses the db.rls drift primitive and a revision-local
#          table snapshot so tables added by later revisions install their own
#          FORCE state instead of breaking historical upgrade steps.
# Blast Radius: Authorization / tenant isolation at the DB owner boundary.
#          No finance math, audit-write, export, or Neo4j impact. Superuser /
#          BYPASSRLS connections (incl. the postgres test login) are unaffected.
# Connections:
#   - File: backend/ums_smart_revenue/db/rls.py -> Current TENANT_SCOPED_TABLES
#     list plus discover_tenant_tables_sql.
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
    for table in _REVISION_TENANT_SCOPED_TABLES:
        # Table names are internal constants from the vetted allowlist.
        bind.execute(sa.text(f'ALTER TABLE public."{table}" FORCE ROW LEVEL SECURITY'))


def downgrade() -> None:
    """Drop the FORCE flag on every tenant table; RLS stays ENABLED."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in _REVISION_TENANT_SCOPED_TABLES:
        bind.execute(sa.text(f'ALTER TABLE public."{table}" NO FORCE ROW LEVEL SECURITY'))
