"""Add a SECURITY DEFINER clear function for the tenant-context table.

Revision ID: 20260609_0002
Revises: 20260608_0002
Create Date: 2026-06-09

Postgres-only in effect (SQLite has no SECURITY DEFINER functions; the body is
guarded and no-ops there). The session hook's pooled-reset path previously ran a
raw ``DELETE FROM app_tenant_context`` under the app_tenant/app_platform lane,
which only holds SELECT on that backend-owned table, so the DELETE
permission-denied. This installs a privileged clearer that runs as the function
owner, mirroring the setter/getter REVOKE-from-PUBLIC + GRANT-EXECUTE pattern in
20260608_0001._create_tenant_context_helpers.

NOTE: down_revision is 20260608_0002. If Track-F PR #87 (migration
20260609_0001) merges first, rebase this down_revision onto 20260609_0001 to keep
a single alembic head.
"""
import sqlalchemy as sa
from alembic import op

from ums_smart_revenue.db.rls import (
    APP_PLATFORM_ROLE,
    APP_TENANT_ROLE,
    TENANT_CONTEXT_CLEARER,
    TENANT_CONTEXT_TABLE,
)

revision = "20260609_0002"
down_revision = "20260608_0002"
branch_labels = None
depends_on = None


# ============================================================================
# Purpose: Install the SECURITY DEFINER clearer that the session hook uses to
#   drop the current backend's tenant-context row on pooled-connection reuse,
#   without granting the tenant lane DELETE on the trusted context table.
# Database/ORM: app_tenant_context (backend-owned tenant-context table).
# Standards: Postgres-only (dialect-guarded); REVOKE-from-PUBLIC then explicit
#   GRANT EXECUTE to both app roles; role/object names are internal constants.
# Blast Radius: Authorization at the DB boundary (clears tenant context so a
#   reused pooled connection cannot inherit a prior tenant's RLS scope).
# Connections:
#   - File: backend/ums_smart_revenue/db/session.py -> hook calls this clearer.
#   - File: backend/ums_smart_revenue/db/rls.py -> TENANT_CONTEXT_CLEARER name.
# ============================================================================
def upgrade() -> None:
    """Create the clearer function and grant EXECUTE to both app roles."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION {TENANT_CONTEXT_CLEARER}()
            RETURNS void
            LANGUAGE sql
            SECURITY DEFINER
            SET search_path = pg_catalog, public
            AS $$
                DELETE FROM {TENANT_CONTEXT_TABLE}
                WHERE backend_pid = pg_backend_pid()
            $$;
            """
        )
    )
    bind.execute(
        sa.text(
            f"REVOKE ALL ON FUNCTION {TENANT_CONTEXT_CLEARER}() FROM PUBLIC"
        )
    )
    bind.execute(
        sa.text(
            f'GRANT EXECUTE ON FUNCTION {TENANT_CONTEXT_CLEARER}() '
            f'TO "{APP_TENANT_ROLE}"'
        )
    )
    bind.execute(
        sa.text(
            f'GRANT EXECUTE ON FUNCTION {TENANT_CONTEXT_CLEARER}() '
            f'TO "{APP_PLATFORM_ROLE}"'
        )
    )


def downgrade() -> None:
    """Drop the clearer function (Postgres-only)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(
        sa.text(f"DROP FUNCTION IF EXISTS {TENANT_CONTEXT_CLEARER}()")
    )
