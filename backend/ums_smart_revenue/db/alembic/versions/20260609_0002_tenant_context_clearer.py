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
    # FIX: install a BEFORE DELETE trigger on the context table that
    # raises if a row's `backend_pid` does not match the current
    # backend. This bounds the platform lane's mutation surface over
    # the RLS context table at steady state: even with the DELETE grant
    # in place, `app_platform` (and the session hook's missing-helper
    # fallback) can only ever delete its OWN row, never another
    # backend's. The privileged `clear_app_current_tenant_id()` helper
    # also runs under this trigger, but its body filters on
    # `backend_pid = pg_backend_pid()` so the trigger's predicate is
    # satisfied and the DELETE goes through.
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
    # FIX: grant DELETE on the context table to app_platform so the
    # session hook's missing-helper fallback (a direct DELETE under
    # app_platform) permission-succeeds during a rolling migration gap.
    # This grant is installed in 20260609_0002 (not 20260608_0001) so
    # databases that already ran the previous version of 20260608_0001
    # pick it up on their next upgrade without re-running 20260608_0001.
    # The BEFORE DELETE trigger above bounds the mutation surface to
    # the caller's own backend row, so widening the platform lane's
    # DELETE privilege here does NOT widen its effective blast radius
    # (Codex P2 review on PR #88).
    bind.execute(
        sa.text(
            f'GRANT DELETE ON {TENANT_CONTEXT_TABLE} TO "{APP_PLATFORM_ROLE}"'
        )
    )
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
    # FIX: revoke the DELETE grant installed in upgrade() so the role
    # does not retain a privilege on the table after this revision
    # rolls back. The table itself is owned by 20260608_0001 and
    # persists past this downgrade; the grant must be removed in lock
    # step with the helper so the previous "no raw DELETE permission"
    # state is fully restored.
    bind.execute(
        sa.text(
            f'REVOKE DELETE ON {TENANT_CONTEXT_TABLE} FROM "{APP_PLATFORM_ROLE}"'
        )
    )
    # FIX: drop the BEFORE DELETE guard trigger installed in upgrade()
    # so the table returns to its pre-migration state (no extra
    # triggers, no helper, no DELETE grant). The trigger function is
    # dropped after the trigger to avoid an "in use" error.
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
            f'DROP FUNCTION IF EXISTS {TENANT_CONTEXT_CLEARER}_guard_delete()'
        )
    )
    bind.execute(sa.text(f'DROP FUNCTION IF EXISTS {TENANT_CONTEXT_CLEARER}()'))
