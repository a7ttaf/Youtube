# ============================================================================
# Purpose: SQLAlchemy engine and session-factory helpers with per-URL engine
#   caching, SQLite StaticPool writer serialization, and Postgres RLS role hooks.
# Database/ORM: All ORM models (engine is the shared connection source); Session
#   factories mark tenant vs platform lanes via session.info.
# Standards: typed boundaries; no error swallowing; SQLite is test/dev-only;
#   Postgres pool_pre_ping and dual-lane role switching unchanged.
# Blast Radius: DB connection topology and transaction discipline. SQLite writer
#   lock is test/dev-only; Postgres authorization/RLS path is untouched.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> request + platform factories.
#   - File: backend/ums_smart_revenue/auth/users.py -> savepoint retries rely on
#     real SQLite BEGIN so RELEASE does not commit.
#   - File: scripts/bootstrap_operator.py -> one-transaction operator bootstrap.
# ============================================================================
"""SQLAlchemy engine and session-factory helpers with per-URL engine caching."""

from collections.abc import Callable, Iterator
from threading import Lock, get_ident
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ums_smart_revenue.db.rls import (
    APP_PLATFORM_ROLE,
    APP_TENANT_ROLE,
)

SessionFactory = sessionmaker[Session]

_SESSION_ROLE_KEY = "ums_db_role"
# Set on session.info by db.lane.platform_lane while a privileged-write block is
# active. The after_begin hook reads it so that a nested SAVEPOINT (which also
# fires after_begin) does not re-pin the tenant lane and silently undo the
# elevation mid-transaction. Only server-side code holding the Session object
# can set it (same trust boundary as the existing single-session elevation in
# finance/committed_allocation.py); the fail-closed default is unchanged.
_PLATFORM_LANE_ACTIVE_KEY = "ums_platform_lane_active"

_engine_cache: dict[str, Engine] = {}
_engine_cache_lock = Lock()


# ============================================================================
# Purpose: Build the per-URL SQLAlchemy Engine. SQLite (test-only) shares one
#   connection via StaticPool so the request session and the audit/platform
#   session serialize through a single writer (SQLite allows only one writer
#   at a time). Postgres (production) keeps the normal connection pool so the
#   dual-lane design hands the tenant lane and the privileged app_platform/
#   audit lane DISTINCT role-switched connections.
# Database/ORM: All ORM models (engine is the shared connection source).
# Standards: typed boundary; no error swallowing; pool_pre_ping retained.
#   SQLite engines additionally get real BEGIN discipline (see
#   _enable_sqlite_transactional_savepoints below) so SAVEPOINTs nest instead
#   of committing, plus a per-engine writer lock so independent Sessions cannot
#   collide on the shared DBAPI connection.
# Blast Radius: DB connection topology. SQLite branch is test-only; Postgres
#   path is intentionally unchanged so RLS role switching is not weakened.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> wires request + platform factories.
# ============================================================================
def build_engine(database_url: str) -> Engine:
    """Create a SQLAlchemy Engine; SQLite shares one connection, others pool."""
    if database_url.startswith("sqlite"):
        # FIX: SQLite permits only ONE writer at a time. The request session and
        # the audit/platform session bind to the same engine but, under the
        # default pool, would each grab a DISTINCT DBAPI connection -> the audit
        # INSERT on connection #2 blocks on the request's open write txn on
        # connection #1 ("database is locked"). StaticPool forces a single shared
        # connection so both sessions serialize through one writer (file-based
        # and in-memory SQLite alike). Postgres needs distinct connections for
        # its two role-switched lanes, so this branch is SQLite-only.
        engine = create_engine(
            database_url,
            poolclass=StaticPool,
            connect_args={"check_same_thread": False},
        )
        _enable_sqlite_transactional_savepoints(engine)
        return engine
    return create_engine(database_url, pool_pre_ping=True)


# ============================================================================
# Purpose: Make SAVEPOINTs REAL on SQLite and serialize independent writers on
#   the StaticPool connection. pysqlite's legacy isolation mode emits BEGIN only
#   before DML, never before SELECT or SAVEPOINT, so a ``Session.begin_nested()``
#   entered while the DBAPI sits in autocommit starts ITS OWN SQLite-level
#   transaction — and the RELEASE on clean exit DURABLY COMMITS it. Every
#   savepoint-wrapped write in this codebase (the auth/users.py storage-retry
#   envelope, the audit sinks, the channel-registry write boundary) therefore
#   committed EARLY on SQLite, silently breaking one-transaction envelopes:
#   scripts/bootstrap_operator.py promised "the whole run is one transaction"
#   while a failure AFTER a savepointed account write left the account
#   half-created (parent-verified red test
#   test_bootstrap_reports_a_database_failure_instead_of_a_traceback). This is
#   the SQLAlchemy-documented pysqlite recipe: stop the DBAPI from managing
#   transactions and emit BEGIN ourselves whenever SQLAlchemy begins one, so
#   savepoints nest inside a real outer transaction and RELEASE releases
#   instead of committing.
#
#   The same StaticPool connection is also shared across independent Sessions
#   (HTTP request + connector scheduler/executor). An unconditional second BEGIN
#   raises ``cannot start a transaction within a transaction`` or lets Session B
#   commit/roll back Session A's writes. A per-engine RLock is acquired before
#   BEGIN and released only after dialect do_commit/do_rollback finishes
#   (Connection commit/rollback events fire too early). Same-thread overlap
#   while a txn is already open is refused rather than nesting a colliding BEGIN.
# Database/ORM: Engine-level transaction discipline for every SQLite session;
#   no table, column, or query change.
# Standards: Documented dialect recipe (SQLAlchemy "Serializable isolation /
#   Savepoints / Transactional DDL" for pysqlite). Postgres path untouched —
#   psycopg already emits BEGIN, so its savepoints were always real.
# Blast Radius: SQLite (test/dev-only per build_engine) transaction semantics:
#   reads now open a real deferred transaction that holds its snapshot until
#   commit/rollback/close; concurrent Sessions wait for the writer lock. No
#   authorization, finance, or Postgres behavior.
# Connections:
#   - File: backend/ums_smart_revenue/auth/users.py -> _run_with_storage_retries
#     relies on RELEASE not committing to keep the bootstrap atomic.
#   - File: scripts/bootstrap_operator.py -> the one-transaction guarantee.
#   - File: backend/ums_smart_revenue/app.py -> executor/scheduler share this
#     engine's session_factory on SQLite test apps.
# ============================================================================
def _enable_sqlite_transactional_savepoints(engine: Engine) -> None:
    """Emit real BEGINs on SQLite so SAVEPOINT/RELEASE nest instead of committing."""
    # StaticPool has one DBAPI connection; one non-reentrant lock is enough.
    # A boolean (not id(dbapi)) tracks ownership because Connection.dbapi_connection
    # and dialect.do_commit's argument are not always the same Python object.
    writer_lock = Lock()
    lock_held = False
    lock_owner_thread: int | None = None

    @event.listens_for(engine, "connect")
    def _disable_pysqlite_transaction_management(
        dbapi_connection: Any, _connection_record: Any
    ) -> None:
        """Disable pysqlite auto-BEGIN so SAVEPOINT nests under our begin hook."""
        # Legacy-autocommit mode: pysqlite stops emitting implicit BEGINs
        # entirely; the begin hook below owns transaction starts instead.
        dbapi_connection.isolation_level = None

    def _release_writer_lock(dbapi_connection: Any = None) -> None:
        """Release the StaticPool writer lock -- only by its owning thread,
        and only when the shared connection is quiescent.

        FIX(codex rounds 22-27, four threads): the pool ``reset`` event fires
        BEFORE SQLAlchemy's reset-on-return ROLLBACK executes, so an
        unconditional release there handed the lock to a waiting Session
        whose fresh ``BEGIN`` was then rolled back by the first session's
        still-running reset -- on StaticPool both "transactions" share ONE
        DBAPI connection. The release therefore requires BOTH: (a) the
        calling thread is the recorded lock owner -- pool events fire on
        whichever thread returns the connection, so the ORIGINAL checkout's
        checkin on another thread can never release a successor's lock, even
        in the narrow window where the successor holds the lock but has not
        emitted its BEGIN yet; and (b) the shared connection reports
        ``in_transaction == False`` -- a reset arriving mid-transaction is
        skipped (the following checkin releases), and the quiescent fast
        path still releases on whichever event lands first, preserving the
        reset-only path that a checkin-only release used to deadlock.
        """
        nonlocal lock_held, lock_owner_thread
        if not lock_held:
            return
        if lock_owner_thread is not None and lock_owner_thread != get_ident():
            return
        if dbapi_connection is not None and getattr(
            dbapi_connection, "in_transaction", False
        ):
            return
        lock_held = False
        lock_owner_thread = None
        writer_lock.release()

    @event.listens_for(engine, "begin")
    def _emit_begin(connection: Connection) -> None:
        """Emit an explicit BEGIN for each outer transaction; serialize writers."""
        # FIX: Hold until the connection returns to the pool after commit/rollback
        # callbacks finish. Releasing in do_commit alone races a second Session
        # onto the still-resetting shared StaticPool connection.
        nonlocal lock_held, lock_owner_thread
        current_thread = get_ident()
        if writer_lock.locked():
            if lock_owner_thread == current_thread:
                dbapi_connection = connection.connection.dbapi_connection
                if getattr(dbapi_connection, "in_transaction", False):
                    raise RuntimeError(
                        "Overlapping SQLite Sessions on StaticPool: another transaction "
                        "is already open on the shared connection. Reuse one Session "
                        "(see _sqlite_platform_session_from_request) or wait for commit."
                    )
            writer_lock.acquire()
        else:
            writer_lock.acquire()
        lock_owner_thread = current_thread
        lock_held = True
        try:
            dbapi_connection = connection.connection.dbapi_connection
            if getattr(dbapi_connection, "in_transaction", False):
                raise RuntimeError(
                    "Overlapping SQLite Sessions on StaticPool: another transaction "
                    "is already open on the shared connection. Reuse one Session "
                    "(see _sqlite_platform_session_from_request) or wait for commit."
                )
            connection.exec_driver_sql("BEGIN")
        except Exception:
            _release_writer_lock()
            raise

    @event.listens_for(engine.pool, "checkin")
    def _release_writer_lock_on_checkin(
        dbapi_connection: Any, _connection_record: Any
    ) -> None:
        """Release the writer lock after SQLAlchemy returns the connection to the pool."""
        _release_writer_lock(dbapi_connection)

    @event.listens_for(engine.pool, "reset")
    def _release_writer_lock_on_reset(
        dbapi_connection: Any, _connection_record: Any, _reset_state: Any
    ) -> None:
        """Release on reset -- but only once the connection is quiescent.

        The reset event fires BEFORE the reset-on-return rollback, so the
        quiescent check inside ``_release_writer_lock`` is what keeps a
        waiting Session from acquiring and having its BEGIN rolled back by
        this very reset. Keep this alongside checkin release (idempotent via
        ``lock_held``). Checkin-only release deadlocked concurrent StaticPool
        writers in
        ``test_sqlite_overlapping_sessions_serialize_instead_of_colliding_begin``.
        """
        _release_writer_lock(dbapi_connection)

    dialect = engine.dialect
    original_do_commit = dialect.do_commit
    original_do_rollback = dialect.do_rollback

    def _do_commit(dbapi_connection: Any) -> None:
        """Commit on the DBAPI connection; lock release waits for pool check-in."""
        original_do_commit(dbapi_connection)

    def _do_rollback(dbapi_connection: Any) -> None:
        """Roll back on the DBAPI connection; lock release waits for pool check-in."""
        original_do_rollback(dbapi_connection)

    # Deliberate instance-level monkeypatch via setattr (avoids method-assign
    # type suppressions). Lock release remains on reset+checkin listeners above.
    setattr(dialect, "do_commit", _do_commit)
    setattr(dialect, "do_rollback", _do_rollback)


# ============================================================================
# Purpose: Build the default app tenant-lane session factory. Sessions produced
#   here opt into the Postgres RLS role hook through session.info; raw SQLAlchemy
#   Sessions used by migrations/tests remain owner sessions unless explicitly
#   marked. SQLite concurrent Sessions are serialized by the engine writer lock
#   in ``_enable_sqlite_transactional_savepoints``; the HTTP dual-lane path still
#   reuses one request Session via ``_sqlite_platform_session_from_request``.
# Database/ORM: SQLAlchemy Session factory metadata only.
# Standards: explicit role marker; no ambient global role changes for unmarked
#   sessions; SQLite writer serialization scoped to the SQLite engine only.
# Blast Radius: Authorization/RLS role selection at the DB boundary (Postgres
#   path); SQLite transaction-isolation discipline (test-only).
# Connections:
#   - File: backend/ums_smart_revenue/db/rls.py -> app_tenant role name.
#   - File: backend/ums_smart_revenue/app.py -> request session factory.
# ============================================================================
def build_session_factory(database_url: str, engine: Engine | None = None) -> SessionFactory:
    """Return a tenant-lane sessionmaker bound to a cached or given engine."""
    if engine is None:
        with _engine_cache_lock:
            if database_url not in _engine_cache:
                _engine_cache[database_url] = build_engine(database_url)
            engine = _engine_cache[database_url]
    # NOTE: We deliberately do NOT set `join_transaction_mode` on the
    # engine-bound sessionmaker. Codex P2 review on PR #88 confirmed that
    # `join_transaction_mode="create_savepoint"` does not actually open a
    # SAVEPOINT for engine-bound sessions on a StaticPool engine (each Session
    # checkout gets a fresh SQLAlchemy Connection wrapper around the same DBAPI
    # connection, so a Session's commit/rollback still acts on the shared
    # underlying transaction). The audit / platform lane does not need a
    # separate Session on SQLite: the app wires
    # `_sqlite_platform_session_from_request` in `app.py` so the platform lane
    # reuses the request session (the same Session object), which removes the
    # multi-Session contention that would otherwise need SAVEPOINT isolation.
    # Independent background Sessions (executor/scheduler) serialize via the
    # per-engine writer lock installed by `_enable_sqlite_transactional_savepoints`.
    # Postgres keeps the default ("conditional_savepoint") because each
    # Session gets a distinct pooled connection with its own outer transaction.
    return sessionmaker(
        bind=engine,
        autoflush=True,
        expire_on_commit=False,
        info={_SESSION_ROLE_KEY: APP_TENANT_ROLE},
    )


# ============================================================================
# Purpose: Dispose AND evict the cached engine for one database_url so its
#   connection pool is released (e.g. so SQLite frees a throwaway file handle on
#   Windows before the file is deleted). No-op when no engine is cached.
# Database/ORM: SQLAlchemy Engine cached in _engine_cache (no ORM models).
# Standards: typed; thread-safe under _engine_cache_lock; no error swallowing.
# Blast Radius: Engine lifecycle only. No auth, finance, audit, Neo4j, exports.
# Connections:
#   - File: scripts/smoke_mvp.py -> calls this before deleting the throwaway db.
# ============================================================================
def dispose_cached_engine(database_url: str) -> None:
    """Dispose and evict the cached engine for ``database_url`` (no-op if absent)."""
    with _engine_cache_lock:
        engine = _engine_cache.pop(database_url, None)
    if engine is not None:
        engine.dispose()


def build_platform_session_factory(
    database_url: str, engine: Engine | None = None
) -> SessionFactory:
    """Return a sessionmaker whose sessions run the privileged app_platform lane."""
    factory = build_session_factory(database_url, engine=engine)
    factory.configure(info={_SESSION_ROLE_KEY: APP_PLATFORM_ROLE})
    return factory


# ============================================================================
# Purpose: Per-transaction tenant isolation hook. On Postgres only, write the
#   current tenant into a backend-owned context row, then switch the role that
#   the transaction runs under. RLS policies read the trusted context row,
#   never a tenant-settable setting.
# Database/ORM: All tenant-scoped tables (RLS policies created in 20260608_0001).
# Standards: SET LOCAL (transaction-scoped, auto-reset on commit/rollback);
#   fail-closed (missing tenant context => no row => RLS policy rejects access);
#   always switch tenant-lane sessions into the restricted app_tenant role so
#   that an unset TENANT_CTX (or an owner/superuser login that bypasses RLS on
#   the raw login role) cannot read tenant tables. EXCEPTION: while
#   db/lane.py:platform_lane holds an active block (session.info carries
#   _PLATFORM_LANE_ACTIVE_KEY), the tenant-lane demote at the end of this hook
#   is SKIPPED, so the session keeps running app_platform for every transaction
#   begun in-block (the elevation already set app_platform above). The flag is
#   set/popped ONLY by db/lane.py.
# Blast Radius: Authorization/finance reads+writes at the DB boundary. SQLite
#   is unaffected; Postgres tenant-lane sessions stay on app_tenant with or
#   without a trusted context row, so RLS is the only authorization gate.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/context.py -> tenant in contextvar.
#   - File: backend/ums_smart_revenue/db/rls.py -> role names + tenant context helpers.
# ============================================================================
@event.listens_for(Session, "after_begin")
def _apply_tenant_isolation(session, _transaction, connection):
    """Set transaction role + trusted tenant context for Postgres sessions."""
    if connection.dialect.name != "postgresql":
        return
    role = session.info.get(_SESSION_ROLE_KEY)
    if role is None:
        return
    # Default app_tenant lane: set tenant context when present and stay
    # restricted when it is absent. Always elevate to app_platform first so the
    # privileged helpers (setter, clearer) can run under a role that holds
    # EXECUTE on them; the runtime login does not inherit EXECUTE under the
    # restricted-login (WITH INHERIT FALSE) deployment model.
    from ums_smart_revenue.tenancy.context import get_current_tenant

    tenant = get_current_tenant()
    connection.exec_driver_sql(f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"')
    if tenant is not None:
        # The setter runs while app_platform is active, so tenant code cannot
        # mutate the trusted context through the tenant lane.
        connection.exec_driver_sql(
            "SELECT set_app_current_tenant_id(%s)",
            (str(tenant.id),),
        )
    else:
        helper_exists = connection.exec_driver_sql(
            "SELECT to_regprocedure(%s) IS NOT NULL",
            ("clear_app_current_tenant_id()",),
        ).scalar()
        if helper_exists:
            # Clear any stale row through the privileged helper before the next request.
            connection.exec_driver_sql("SELECT clear_app_current_tenant_id()")
        else:
            # FIX: During a rolling migration gap, clear the trusted context row
            # directly instead of leaving a pooled backend pinned to a prior tenant.
            connection.exec_driver_sql(
                "DELETE FROM app_tenant_context WHERE backend_pid = pg_backend_pid()"
            )
    if role == APP_TENANT_ROLE:
        # FIX: Keep tenant-lane sessions restricted even when TENANT_CTX is
        # absent; missing trusted context is the fail-closed RLS signal.
        # Always switch into app_tenant so an unset context row (or an owner
        # login that bypasses RLS on the raw role) cannot leak tenant data.
        # EXCEPTION: while db.lane.platform_lane holds an active privileged
        # block, stay on app_platform. after_begin also fires for nested
        # SAVEPOINTs, so without this a savepoint inside the block would re-pin
        # app_tenant and silently undo the elevation mid-transaction (the
        # platform-only write then permission-denies). The elevation already
        # set app_platform above; leaving it in place is correct here.
        if not session.info.get(_PLATFORM_LANE_ACTIVE_KEY):
            connection.exec_driver_sql(f'SET LOCAL ROLE "{APP_TENANT_ROLE}"')


def session_dependency(
    session_factory: SessionFactory,
) -> Callable[[], Iterator[Session]]:
    """Build a FastAPI dependency that yields a request-scoped, auto-committed session."""

    def dependency() -> Iterator[Session]:
        """Yield one session, committing on success and rolling back on any error."""
        with session_factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    return dependency
