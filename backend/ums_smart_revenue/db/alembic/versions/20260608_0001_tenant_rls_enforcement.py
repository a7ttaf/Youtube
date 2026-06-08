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

# NON-tenant tables the app writes at runtime (no tenant_id, so not RLS-scoped).
# currency_exchange_rates stays writable from both app roles because tenant
# workflows update exchange rates directly. The committed_allocation_* child
# tables are privilege-sensitive snapshot evidence: app_tenant may read them,
# but only app_platform may write them, and the commit service elevates into the
# privileged lane just for that short child-row insert block.
NON_TENANT_WRITE_TABLES: tuple[str, ...] = ("currency_exchange_rates",)
PLATFORM_ONLY_WRITE_TABLES: tuple[str, ...] = (
    "committed_allocation_lines",
    "committed_allocation_notes",
    "committed_allocation_unallocated",
)


def _create_role(bind, role: str, *, bypassrls: bool) -> None:
    """Create a NOLOGIN role idempotently; set BYPASSRLS as requested."""
    exists = bind.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}
    ).first()
    bypass = "BYPASSRLS" if bypassrls else "NOBYPASSRLS"
    if exists is None:
        # Role names are internal constants, not user input.
        bind.execute(sa.text(f'CREATE ROLE "{role}" NOLOGIN {bypass}'))
    else:
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
    bind.execute(
        sa.text(f'GRANT USAGE ON SCHEMA public TO "{APP_TENANT_ROLE}"')
    )
    bind.execute(
        sa.text(f'GRANT USAGE ON SCHEMA public TO "{APP_PLATFORM_ROLE}"')
    )
    for table in TENANT_SCOPED_TABLES:
        policy = tenant_rls_policy_name(table)
        bind.execute(
            sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        )
        # DROP-then-CREATE so a half-applied dev run / re-run does not wedge on
        # an existing policy (CREATE POLICY has no IF NOT EXISTS).
        bind.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
        bind.execute(
            sa.text(
                f"CREATE POLICY {policy} ON {table} USING "
                "(tenant_id = current_setting('app.current_tenant_id')::uuid) "
                "WITH CHECK "
                "(tenant_id = current_setting('app.current_tenant_id')::uuid)"
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
    # ========================================================================
    # Purpose: Least-privilege grant surface for the app roles. The 25 tenant
    #   tables already got per-table CRUD (above) and are isolated by RLS. Reads
    #   are harmless, so grant SELECT broadly (covers authz catalogs, currencies,
    #   etc. a restricted INHERIT-FALSE login otherwise cannot read). DML is
    #   granted on the runtime-write non-tenant tables, while the
    #   committed_allocation_* evidence tables are writable only from the
    #   privileged platform lane.
    # Database/ORM: SELECT on all public tables/sequences; DML on
    #   NON_TENANT_WRITE_TABLES for both app roles and PLATFORM_ONLY_WRITE_TABLES
    #   for app_platform only.
    # Standards: Enumerated DML keeps DB least-privilege; role names + table list
    #   are internal constants. If a future endpoint writes another non-tenant
    #   table, add it to NON_TENANT_WRITE_TABLES (else it 'permission denies'
    #   under a restricted login — caught by the non-owner-login RLS test).
    # Blast Radius: Authorization (DB privilege surface only — app permission
    #   checks unchanged); finance/audit reads of platform-shared catalogs.
    # ========================================================================
    for role in (APP_TENANT_ROLE, APP_PLATFORM_ROLE):
        bind.execute(
            sa.text(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"')
        )
        bind.execute(
            sa.text(
                "GRANT USAGE, SELECT "
                f'ON ALL SEQUENCES IN SCHEMA public TO "{role}"'
            )
        )
        for table in NON_TENANT_WRITE_TABLES:
            bind.execute(
                sa.text(
                    f"GRANT INSERT, UPDATE, DELETE ON {table} TO \"{role}\""
                )
            )
    for table in PLATFORM_ONLY_WRITE_TABLES:
        bind.execute(
            sa.text(
                f"GRANT INSERT, UPDATE, DELETE ON {table} "
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
    # Blanket REVOKE mirrors the blanket GRANT in upgrade(); DROP ROLE fails
    # while dependent privileges remain, so all table/sequence/schema privs
    # must be revoked first.
    for role in (APP_TENANT_ROLE, APP_PLATFORM_ROLE):
        bind.execute(
            sa.text(
                "REVOKE ALL PRIVILEGES "
                f'ON ALL TABLES IN SCHEMA public FROM "{role}"'
            )
        )
        bind.execute(
            sa.text(
                "REVOKE ALL PRIVILEGES "
                f'ON ALL SEQUENCES IN SCHEMA public FROM "{role}"'
            )
        )
        bind.execute(
            sa.text(f'REVOKE USAGE ON SCHEMA public FROM "{role}"')
        )
        bind.execute(sa.text(f'DROP ROLE IF EXISTS "{role}"'))
