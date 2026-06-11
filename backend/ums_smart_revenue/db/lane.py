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
from ums_smart_revenue.db.session import (
    _PLATFORM_LANE_ACTIVE_KEY,
    _SESSION_ROLE_KEY,
)


# ============================================================================
# Purpose: Elevate one session to the privileged ``app_platform`` role for a
#   block of platform-only writes (audit / finance-fact / month-close), then
#   restore the tenant lane on a clean exit. No-op off Postgres so the SQLite
#   test tier is unaffected.
# Database/ORM: All tables the wrapped block writes; specifically the
#   TENANT_PLATFORM_ONLY_WRITE_TABLES set (audit_logs, finance_month_close,
#   monthly_channel_revenue_facts).
# Standards (MEASURED semantics -- probe-verified against the live PG container,
#   not the SET-LOCAL textbook):
#   - SET LOCAL ROLE is transaction-scoped, but the elevation here spans EVERY
#     transaction begun on the session while the _PLATFORM_LANE_ACTIVE_KEY flag
#     is set, NOT just the entry transaction. The after_begin hook
#     unconditionally issues SET LOCAL ROLE "app_platform" first; while the flag
#     is set it then SKIPS the app_tenant demote. So elevation persists across a
#     mid-block commit, a mid-block rollback, and nested SAVEPOINTs -- the next
#     after_begin re-elevates and the demote stays skipped until block exit.
#     This full-block persistence is LOAD-BEARING: the run path's failure
#     recorder commits/rolls back mid-block and then writes platform-only audit
#     edges that depend on still being app_platform.
#   - The flag pop ALWAYS runs in finally, so the flag never leaks past the
#     block (clean exit or exception). The next after_begin then re-pins the
#     tenant lane.
#   - The role restore runs ONLY on a clean exit AND only when the entry
#     transaction is still active and healthy (see the Finding 4 guard below):
#     skipped when the session is not in a transaction (post-commit/post-rollback
#     body -- avoids autobegining an idle-in-transaction connection just to set a
#     role the next after_begin re-pins anyway) and skipped when the in-flight
#     transaction is in error state (avoids raising InFailedSqlTransaction out of
#     __exit__ and masking the real error). The restore targets only tenant-lane
#     sessions (session.info marker); a platform-lane session is left elevated
#     because demoting it would wrongly restrict an already-privileged lane.
#   - On a BODY EXCEPTION the restore is skipped entirely (success-gated). The
#     session keeps executing as app_platform until the caller's rollback ends
#     the transaction, after which the next after_begin re-pins the tenant lane.
#   - NOT nest-safe: a platform_lane inside another platform_lane would pop the
#     flag at the inner exit and demote early; the run path never nests it.
# Blast Radius: Authorization (which DB role executes already-trusted writes).
#   RLS still applies to app_platform (NOBYPASSRLS + trusted-context policies),
#   so tenant scoping is preserved; this only widens the write-grant surface
#   for the duration of the block. No finance math, no audit semantics, no
#   Neo4j, no exports change.
# Connections:
#   - File: backend/ums_smart_revenue/finance/committed_allocation.py ->
#     the single-session elevation precedent this helper generalizes.
#   - File: backend/ums_smart_revenue/db/session.py -> _SESSION_ROLE_KEY marker
#     + the after_begin hook that pins the configured lane per transaction and
#     skips the demote while _PLATFORM_LANE_ACTIVE_KEY is set.
#   - File: backend/ums_smart_revenue/connectors/runs/orchestrator.py +
#     normalization.py -> the run-path platform-only write surfaces wrapped here
#     (including the mid-block failure recorder that relies on the persistence).
# ============================================================================
@contextmanager
def platform_lane(session: Session) -> Iterator[None]:
    """Run the wrapped block under ``app_platform`` (no-op off Postgres)."""
    connection = session.connection()
    if connection.dialect.name != "postgresql":
        # Off Postgres there are no roles to switch; stay transparent.
        yield
        return
    # Mark the lane active BEFORE elevating so the after_begin hook keeps any
    # nested SAVEPOINT on app_platform instead of re-pinning the tenant lane.
    session.info[_PLATFORM_LANE_ACTIVE_KEY] = True
    # The after_begin hook fired on session.connection() above and pinned the
    # configured lane; elevate to app_platform for the platform-only writes.
    connection.exec_driver_sql(f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"')
    completed = False
    try:
        yield
        completed = True
    finally:
        # The flag pop is an in-memory dict op and always runs, so the next
        # after_begin re-pins the tenant lane regardless of how the block exits.
        session.info.pop(_PLATFORM_LANE_ACTIVE_KEY, None)
        # Restore the tenant lane only on a clean exit, and only for tenant-lane
        # sessions (a platform-lane session is privileged for its whole life;
        # demoting it would wrongly restrict it). On the exception path the
        # block's transaction may be aborted, so issuing SET LOCAL ROLE would
        # raise "current transaction is aborted" and mask the real error -- the
        # caller's rollback ends the transaction and the next after_begin
        # re-pins the configured lane. This mirrors the committed_allocation
        # precedent, which restores only after a successful child write.
        if completed and session.info.get(_SESSION_ROLE_KEY) == APP_TENANT_ROLE:
            _restore_tenant_lane(session)


# ============================================================================
# Purpose: Issue the clean-exit SET LOCAL ROLE "app_tenant" restore for a
#   tenant-lane session, but ONLY when the entry transaction is still active and
#   healthy -- never autobegin a fresh transaction and never touch an aborted
#   one.
# Database/ORM: None directly (issues a role statement on the live connection).
# Standards (Finding 4 guard):
#   - Skip when ``not session.in_transaction()``: the body ended with a commit
#     or rollback (EVERY currently-wired block commits). session.connection()
#     would AUTOBEGIN a brand-new transaction just to set a role the next
#     after_begin re-pins anyway, leaving an idle-in-transaction connection
#     (holding the hook's uncommitted context-row write) across the next
#     report's network download -- any deployment with
#     idle_in_transaction_session_timeout would then kill the run.
#   - Skip when the in-flight transaction is in error state: a body that
#     swallowed a DB error but exited cleanly would otherwise raise
#     InFailedSqlTransaction out of __exit__. We only reach the psycopg status
#     read AFTER the in_transaction() guard, so session.connection() never
#     autobegins here.
# Blast Radius: Authorization (role restore timing only). No finance, audit,
#   Neo4j, or export change.
# Connections:
#   - File: backend/ums_smart_revenue/db/session.py -> the after_begin hook that
#     re-pins the tenant lane on the next transaction when this restore is
#     skipped.
# ============================================================================
def _restore_tenant_lane(session: Session) -> None:
    """Restore app_tenant only when the entry transaction is active + healthy."""
    # FIX: previously this ran unconditionally on clean exit, which autobegan an
    # idle-in-transaction connection after a body commit and raised
    # InFailedSqlTransaction after a swallowed DB error. Guard on the live
    # transaction state so neither footgun can fire.
    if not session.in_transaction():
        return
    connection = session.connection()
    driver_connection = connection.connection.driver_connection
    transaction_status = driver_connection.info.transaction_status
    # Compare by name ("INERROR") to avoid a module-level psycopg import in a
    # module the SQLite test tier imports; psycopg.pq.TransactionStatus.INERROR
    # is the aborted-transaction sentinel.
    if getattr(transaction_status, "name", "") == "INERROR":
        return
    connection.exec_driver_sql(f'SET LOCAL ROLE "{APP_TENANT_ROLE}"')
