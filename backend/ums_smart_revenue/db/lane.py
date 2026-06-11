"""Single-session privileged-lane elevation for platform-only writes.

The connector run path writes three tables that the Track-E migration grants
``app_tenant`` no DML on (``audit_logs``, ``finance_month_close``,
``monthly_channel_revenue_facts`` -- ``TENANT_PLATFORM_ONLY_WRITE_TABLES`` in
``20260608_0001``). When the run executes on a tenant-lane session (the CLI /
executor pattern), those writes permission-deny on Postgres. This helper
generalizes the sanctioned single-session elevation precedent in
``finance/committed_allocation.py`` so the run path can issue those writes under
``app_platform`` inside the same atomic transaction.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from ums_smart_revenue.db.rls import APP_PLATFORM_ROLE, APP_TENANT_ROLE
from ums_smart_revenue.db.session import _SESSION_ROLE_KEY


# ============================================================================
# Purpose: Elevate one session to the privileged ``app_platform`` role for a
#   block of platform-only writes (audit / finance-fact / month-close), then
#   restore the tenant lane on exit. No-op off Postgres so the SQLite test tier
#   is unaffected.
# Database/ORM: All tables the wrapped block writes; specifically the
#   TENANT_PLATFORM_ONLY_WRITE_TABLES set (audit_logs, finance_month_close,
#   monthly_channel_revenue_facts).
# Standards: SET LOCAL ROLE is transaction-scoped, so the elevation must be
#   re-applied per transaction AFTER the transaction has begun -- we touch
#   session.connection() first so the after_begin hook pins the configured lane
#   before we elevate. A commit/rollback INSIDE the block ends the elevation
#   with that transaction (callers must not commit mid-block and keep writing
#   under the assumption the elevation persists). The exit restore targets only
#   tenant-lane sessions (session.info marker); a platform-lane session is left
#   elevated because demoting it would wrongly restrict an already-privileged
#   lane. The restore runs in a finally so a body failure cannot strand a
#   tenant-lane session in app_platform.
# Blast Radius: Authorization (which DB role executes already-trusted writes).
#   RLS still applies to app_platform (NOBYPASSRLS + trusted-context policies),
#   so tenant scoping is preserved; this only widens the write-grant surface
#   for the duration of the block. No finance math, no audit semantics, no
#   Neo4j, no exports change.
# Connections:
#   - File: backend/ums_smart_revenue/finance/committed_allocation.py ->
#     the single-session elevation precedent this helper generalizes.
#   - File: backend/ums_smart_revenue/db/session.py -> _SESSION_ROLE_KEY marker
#     + the after_begin hook that pins the configured lane per transaction.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py +
#     normalization.py -> the run-path platform-only write surfaces wrapped here.
# ============================================================================
@contextmanager
def platform_lane(session: Session) -> Iterator[None]:
    """Run the wrapped block under ``app_platform`` (no-op off Postgres)."""
    connection = session.connection()
    if connection.dialect.name != "postgresql":
        # Off Postgres there are no roles to switch; stay transparent.
        yield
        return
    # The after_begin hook fired on the line above and pinned the session's
    # configured lane; elevate to app_platform for the platform-only writes.
    connection.exec_driver_sql(f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"')
    try:
        yield
    finally:
        # Restore the tenant lane only for tenant-lane sessions. A platform-lane
        # session is already privileged for its whole lifetime; demoting it to
        # app_tenant here would wrongly restrict it.
        if session.info.get(_SESSION_ROLE_KEY) == APP_TENANT_ROLE:
            session.connection().exec_driver_sql(
                f'SET LOCAL ROLE "{APP_TENANT_ROLE}"'
            )
