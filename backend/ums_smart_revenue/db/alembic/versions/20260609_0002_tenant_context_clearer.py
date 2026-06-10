"""Add privileged helper for clearing the trusted tenant context."""

import sqlalchemy as sa
from alembic import op

from ums_smart_revenue.db.rls import (
    APP_PLATFORM_ROLE,
    TENANT_CONTEXT_CLEARER,
    TENANT_CONTEXT_TABLE,
)

revision = "20260609_0002"
down_revision = "20260609_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the platform-only tenant-context cleanup helper."""
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
            f'REVOKE ALL ON FUNCTION {TENANT_CONTEXT_CLEARER}() FROM PUBLIC'
        )
    )
    bind.execute(
        sa.text(
            f'GRANT EXECUTE ON FUNCTION {TENANT_CONTEXT_CLEARER}() TO "{APP_PLATFORM_ROLE}"'
        )
    )


def downgrade() -> None:
    """Drop the platform-only tenant-context cleanup helper."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(sa.text(f'DROP FUNCTION IF EXISTS {TENANT_CONTEXT_CLEARER}()'))
