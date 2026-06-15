"""Enable tenant Row-Level Security: app_tenant/app_platform roles + policies.

Revision ID: 20260608_0001
Revises: 20260606_0001
Create Date: 2026-06-08

Postgres-only in effect (SQLite has no RLS/roles; the whole body is guarded and
no-ops there). Creates the two roles idempotently, installs the backend-owned
tenant-context helpers, enables RLS + an isolation policy on every
tenant-scoped table, and grants the least-privilege tenant/platform DML
surface. A drift guard fails the migration if the live set of tenant_id tables
does not equal db.rls.TENANT_SCOPED_TABLES, so a new tenant table cannot ship
unprotected.

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
    TENANT_CONTEXT_CLEARER,
    TENANT_CONTEXT_GETTER,
    TENANT_CONTEXT_SETTER,
    TENANT_CONTEXT_TABLE,
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
TENANT_PLATFORM_ONLY_WRITE_TABLES: tuple[str, ...] = (
    "audit_logs",
    "finance_month_close",
    "monthly_channel_revenue_facts",
)
PLATFORM_ONLY_WRITE_TABLES: tuple[str, ...] = (
    "committed_allocation_lines",
    "committed_allocation_notes",
    "committed_allocation_unallocated",
)


def _create_role(bind, role: str) -> None:
    """Create a NOLOGIN role idempotently without special RLS bypass."""
    exists = bind.execute(sa.text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": role}).first()
    if exists is None:
        # Role names are internal constants, not user input.
        bind.execute(sa.text(f'CREATE ROLE "{role}" NOLOGIN'))
        return
    bypass = bind.execute(
        sa.text("SELECT rolbypassrls FROM pg_roles WHERE rolname = :r"),
        {"r": role},
    ).scalar_one()
    if not bypass:
        return
    is_superuser = bind.execute(
        sa.text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
    ).scalar_one()
    if not is_superuser:
        raise RuntimeError(
            f"Role {role} already has BYPASSRLS and the migrator is not a "
            "superuser; clear the attribute before upgrading."
        )
    bind.execute(sa.text(f'ALTER ROLE "{role}" NOBYPASSRLS'))


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


# ============================================================================
# Purpose: Bound direct DELETE fallback access to the caller's backend context
#          row before app_platform receives DELETE on app_tenant_context.
# Database/ORM: PostgreSQL table app_tenant_context; no SQLAlchemy ORM models.
# Standards: Migration-owned SQL is explicit, idempotent, and fail-closed by
#            raising on cross-backend DELETE attempts.
# Blast Radius: Authorization and audit-adjacent tenant isolation guard.
# Connections:
#   - File: backend/ums_smart_revenue/db/session.py -> Uses direct DELETE
#     fallback when clear_app_current_tenant_id is absent.
#   - File: backend/ums_smart_revenue/db/alembic/versions/20260609_0002_tenant_context_clearer.py
#     -> Reinstalls the same guard for databases that already ran 20260608_0001.
# ============================================================================
def _create_tenant_context_delete_guard(bind) -> None:
    """Install the backend-row guard for direct context DELETE fallback."""
    bind.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {TENANT_CONTEXT_CLEARER}_guard_delete()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF OLD.backend_pid <> pg_backend_pid() THEN
                    RAISE EXCEPTION
                        'app_tenant_context DELETE restricted to current backend';
                END IF;
                RETURN OLD;
            END;
            $$
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            DROP TRIGGER IF EXISTS {TENANT_CONTEXT_CLEARER}_guard_delete_trg
            ON {TENANT_CONTEXT_TABLE}
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            CREATE TRIGGER {TENANT_CONTEXT_CLEARER}_guard_delete_trg
            BEFORE DELETE ON {TENANT_CONTEXT_TABLE}
            FOR EACH ROW
            EXECUTE FUNCTION {TENANT_CONTEXT_CLEARER}_guard_delete()
            """
        )
    )


def _create_tenant_context_helpers(bind) -> None:
    """Create the backend-owned tenant-context table and helper functions."""
    bind.execute(
        sa.text(
            f"""
            CREATE TABLE IF NOT EXISTS {TENANT_CONTEXT_TABLE} (
                backend_pid integer PRIMARY KEY,
                tenant_id uuid NOT NULL,
                updated_at timestamptz NOT NULL DEFAULT now()
            )
            """
        )
    )
    _create_tenant_context_delete_guard(bind)
    # FIX: Grant DELETE on the context table to app_platform so the
    # session hook can fall back to a direct DELETE during a rolling
    # migration gap (i.e. when 20260609_0002 has not yet installed the
    # privileged `clear_app_current_tenant_id` helper). The helper
    # itself runs SECURITY DEFINER and bypasses these grants, but the
    # fallback path runs as the caller role, so app_platform needs
    # DELETE explicitly. The BEFORE DELETE guard above limits that grant
    # to the caller's own backend row; without the grant, no-context
    # sessions on a fresh 20260608_0001 install would permission-deny on
    # the missing-helper fallback flagged by Codex P2 review on PR #88.
    bind.execute(sa.text(f'GRANT DELETE ON {TENANT_CONTEXT_TABLE} TO "{APP_PLATFORM_ROLE}"'))
    bind.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {TENANT_CONTEXT_SETTER}(tenant uuid)
            RETURNS void
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $$
                INSERT INTO {TENANT_CONTEXT_TABLE} (
                    backend_pid, tenant_id, updated_at
                )
                VALUES (pg_backend_pid(), tenant, now())
                ON CONFLICT (backend_pid) DO UPDATE
                SET tenant_id = EXCLUDED.tenant_id,
                    updated_at = EXCLUDED.updated_at
            $$;
            """
        )
    )
    bind.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {TENANT_CONTEXT_GETTER}()
            RETURNS uuid
            LANGUAGE sql
            SECURITY DEFINER
            STABLE
            SET search_path = pg_catalog, public
            AS $$
                SELECT tenant_id
                FROM {TENANT_CONTEXT_TABLE}
                WHERE backend_pid = pg_backend_pid()
            $$;
            """
        )
    )
    # FIX: The privileged clearer (`clear_app_current_tenant_id`) is NOT
    # installed here on purpose. Its sole owner is `20260609_0002`, which
    # creates the function in upgrade() and drops it in downgrade(). A fresh
    # DB at this revision (20260608_0001) will not have the helper yet — the
    # session hook tolerates that via its `to_regprocedure` probe and falls
    # back to a direct DELETE on the trusted-context row, so the helper is
    # only required once 20260609_0002 has run. Installing it here too would
    # leave two migrations claiming ownership of the same function, so a
    # downgrade past 20260609_0002 would drop a helper this earlier revision
    # still claims to have created (the Alembic ownership double-claim bug
    # flagged by Codex P2 review on PR #88).
    #
    # The direct DELETE fallback is allowed because the GRANT DELETE on
    # `app_tenant_context` to `app_platform` is installed in
    # `_create_tenant_context_helpers` (above), so the fallback path
    # permission-succeeds during a rolling migration gap.
    bind.execute(sa.text(f"REVOKE ALL ON FUNCTION {TENANT_CONTEXT_SETTER}(uuid) FROM PUBLIC"))
    bind.execute(sa.text(f"REVOKE ALL ON FUNCTION {TENANT_CONTEXT_GETTER}() FROM PUBLIC"))
    bind.execute(
        sa.text(f'GRANT EXECUTE ON FUNCTION {TENANT_CONTEXT_SETTER}(uuid) TO "{APP_PLATFORM_ROLE}"')
    )
    bind.execute(
        sa.text(f'GRANT EXECUTE ON FUNCTION {TENANT_CONTEXT_GETTER}() TO "{APP_TENANT_ROLE}"')
    )
    bind.execute(
        sa.text(f'GRANT EXECUTE ON FUNCTION {TENANT_CONTEXT_GETTER}() TO "{APP_PLATFORM_ROLE}"')
    )


def upgrade() -> None:
    """Create roles and enable tenant-isolation RLS on all tenant tables."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _assert_no_drift(bind)
    _create_role(bind, APP_TENANT_ROLE)
    _create_role(bind, APP_PLATFORM_ROLE)
    _create_tenant_context_helpers(bind)
    bind.execute(sa.text(f'GRANT USAGE ON SCHEMA public TO "{APP_TENANT_ROLE}"'))
    bind.execute(sa.text(f'GRANT USAGE ON SCHEMA public TO "{APP_PLATFORM_ROLE}"'))
    for table in TENANT_SCOPED_TABLES:
        policy = tenant_rls_policy_name(table)
        bind.execute(sa.text(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY"))
        # DROP-then-CREATE so a half-applied dev run / re-run does not wedge on
        # an existing policy (CREATE POLICY has no IF NOT EXISTS).
        bind.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
        bind.execute(
            sa.text(
                f"CREATE POLICY {policy} ON {table} USING "
                f"(tenant_id = {TENANT_CONTEXT_GETTER}()) "
                "WITH CHECK "
                f"(tenant_id = {TENANT_CONTEXT_GETTER}())"
            )
        )
        bind.execute(sa.text(f'GRANT SELECT ON {table} TO "{APP_TENANT_ROLE}"'))
        if table not in TENANT_PLATFORM_ONLY_WRITE_TABLES:
            bind.execute(sa.text(f'GRANT INSERT, UPDATE, DELETE ON {table} TO "{APP_TENANT_ROLE}"'))
        bind.execute(
            sa.text(f'GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO "{APP_PLATFORM_ROLE}"')
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
    #   NON_TENANT_WRITE_TABLES for both app roles, on
    #   TENANT_PLATFORM_ONLY_WRITE_TABLES + PLATFORM_ONLY_WRITE_TABLES for
    #   app_platform only, and on the remaining tenant tables for both roles.
    # Standards: Enumerated DML keeps DB least-privilege; role names + table list
    #   are internal constants. If a future endpoint writes another non-tenant
    #   table, add it to NON_TENANT_WRITE_TABLES (else it 'permission denies'
    #   under a restricted login — caught by the non-owner-login RLS test).
    # Blast Radius: Authorization (DB privilege surface only — app permission
    #   checks unchanged); finance/audit reads of platform-shared catalogs.
    # ========================================================================
    for role in (APP_TENANT_ROLE, APP_PLATFORM_ROLE):
        bind.execute(sa.text(f'GRANT SELECT ON ALL TABLES IN SCHEMA public TO "{role}"'))
        bind.execute(sa.text(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{role}"'))
        for table in NON_TENANT_WRITE_TABLES:
            bind.execute(sa.text(f'GRANT INSERT, UPDATE, DELETE ON {table} TO "{role}"'))
    for table in TENANT_PLATFORM_ONLY_WRITE_TABLES:
        bind.execute(sa.text(f'GRANT INSERT, UPDATE, DELETE ON {table} TO "{APP_PLATFORM_ROLE}"'))
    for table in PLATFORM_ONLY_WRITE_TABLES:
        bind.execute(sa.text(f'GRANT INSERT, UPDATE, DELETE ON {table} TO "{APP_PLATFORM_ROLE}"'))


def downgrade() -> None:
    """Drop policies, disable RLS, revoke grants, and drop the two roles."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in TENANT_SCOPED_TABLES:
        policy = tenant_rls_policy_name(table)
        bind.execute(sa.text(f"DROP POLICY IF EXISTS {policy} ON {table}"))
        bind.execute(sa.text(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY"))
    bind.execute(sa.text(f"DROP FUNCTION IF EXISTS {TENANT_CONTEXT_SETTER}(uuid)"))
    bind.execute(sa.text(f"DROP FUNCTION IF EXISTS {TENANT_CONTEXT_GETTER}()"))
    # FIX: The privileged clearer is owned by `20260609_0002`, not by this
    # migration. Its downgrade in `20260609_0002` will drop it as part of
    # rolling back past that revision; we must NOT drop it here as part of
    # the normal new-install path, otherwise two migrations would race to
    # remove the same object (the Alembic ownership double-claim bug
    # flagged by Codex P2 review on PR #88). We DO add an IF EXISTS drop
    # here as a legacy-cleanup safety net: databases that already ran the
    # previous version of this revision had `clear_app_current_tenant_id`
    # installed by it, and rolling such a database back past
    # `20260608_0001` without first passing through `20260609_0002` would
    # otherwise leave the stale helper referencing a dropped table — a
    # later no-context session would call the helper and fail against the
    # missing `app_tenant_context` instead of taking the absent-helper
    # fallback (Codex P2 review on PR #88). The IF EXISTS makes this a
    # no-op for new installs that never created the helper here.
    bind.execute(sa.text(f"DROP FUNCTION IF EXISTS {TENANT_CONTEXT_CLEARER}()"))
    bind.execute(sa.text(f"DROP TABLE IF EXISTS {TENANT_CONTEXT_TABLE}"))
    bind.execute(sa.text(f"DROP FUNCTION IF EXISTS {TENANT_CONTEXT_CLEARER}_guard_delete()"))
    # ========================================================================
    # Purpose: Strip every dependent privilege/object before DROP ROLE.
    #   Postgres refuses DROP ROLE while the role still holds (or was granted)
    #   any privilege in the DB — notably the EXECUTE grants on the SECURITY
    #   DEFINER tenant-context functions, which the blanket table/sequence/
    #   schema REVOKEs do not cover. DROP OWNED BY clears all
    #   privileges-granted-to and objects-owned-by the role in the current DB,
    #   so DROP ROLE then succeeds. The schema/table REVOKEs are kept as
    #   harmless belt-and-suspenders; the membership revoke clears the
    #   restricted-login grant graph.
    # Database/ORM: cluster-global app_tenant/app_platform roles; all public
    #   tables/sequences; the tenant-context SECURITY DEFINER functions.
    # Standards: Postgres-only (guarded by dialect at function top); role names
    #   are internal constants, not user input.
    # Blast Radius: dev/test-only downgrade path (never run in prod); no
    #   finance/audit/Neo4j impact.
    # ========================================================================
    for role in (APP_TENANT_ROLE, APP_PLATFORM_ROLE):
        # Revoke any current memberships before dropping the lane role. The
        # deployed restricted-login model grants these roles to runtime logins,
        # so a downgrade must clear the membership graph first.
        member_roles = bind.execute(
            sa.text(
                "SELECT rolname "
                "FROM pg_auth_members "
                "JOIN pg_roles ON pg_roles.oid = pg_auth_members.member "
                "WHERE pg_auth_members.roleid = ("
                "SELECT oid FROM pg_roles WHERE rolname = :role)"
            ),
            {"role": role},
        ).scalars()
        for member_role in member_roles:
            bind.execute(sa.text(f'REVOKE "{role}" FROM "{member_role}"'))
        bind.execute(sa.text(f'REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{role}"'))
        bind.execute(
            sa.text(f'REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{role}"')
        )
        bind.execute(sa.text(f'REVOKE USAGE ON SCHEMA public FROM "{role}"'))
        # FIX: DROP OWNED BY clears the function EXECUTE grants (and any other
        # privilege granted to the role) that the blanket REVOKEs above miss;
        # without it DROP ROLE raised DependentObjectsStillExist.
        bind.execute(sa.text(f'DROP OWNED BY "{role}"'))
        bind.execute(sa.text(f'DROP ROLE IF EXISTS "{role}"'))
