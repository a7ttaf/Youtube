"""Sole owner of the privileged helper that clears the trusted tenant context.

The helper (``clear_app_current_tenant_id``) was introduced so the session hook
can wipe a stale trusted-context row on a pooled backend before the next
request lands. The app lanes (``app_tenant`` / ``app_platform``) only hold
``SELECT`` on the ``app_tenant_context`` table, so a raw ``DELETE`` would
permission-deny; this SECURITY DEFINER function runs as its owner (the
migration runner / a DBA-precreated role) and bypasses the lane-level grant
restriction.

Ownership contract:

* This migration is the **only** Alembic revision that creates or drops
  ``clear_app_current_tenant_id``. ``20260608_0001_tenant_rls_enforcement``
  deliberately does NOT install the helper — it pre-dates the helper, and
  dual-ownership between two revisions would let a downgrade past this
  revision drop a function that an earlier revision still claims to have
  installed (Codex P2 review on PR #88).
* The session hook tolerates the missing-helper state with a
  ``to_regprocedure`` probe and falls back to a direct ``DELETE`` on the
  trusted-context row under the elevated ``app_platform`` role, so a fresh
  DB at ``20260608_0001`` (before this revision runs) is not broken.
* ``GRANT EXECUTE`` is only granted to ``app_platform`` because the session
  hook always elevates to ``app_platform`` first before invoking the helper
  (``db/session.py::_apply_tenant_isolation``); ``app_tenant`` does not need
  direct EXECUTE on the clearer.
"""

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
    """Create the privileged tenant-context cleanup helper (sole owner)."""
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
    """Drop the privileged tenant-context cleanup helper (sole owner)."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    bind.execute(sa.text(f'DROP FUNCTION IF EXISTS {TENANT_CONTEXT_CLEARER}()'))
