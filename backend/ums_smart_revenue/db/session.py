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
from threading import Lock, get_ident, local
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


def _refuses_overlapping_static_pool_begin(
    connection: Connection,
    writer_lock: Lock,
    owner_fairy: Any,
    owner_thread_id: int | None,
) -> None:
    """Refuse a BEGIN that would collide with an open transaction on the
    shared StaticPool connection when the holder is the owning checkout or
    the owning thread.

    Waiting can never unblock the holder in those two cases (it is this very
    checkout, or it is parked on this same thread), so the collision is
    refused with the overlapping-sessions diagnostic instead of deadlocking
    on the non-reentrant acquire. A DIFFERENT checkout on a DIFFERENT thread
    simply falls through and waits for the owner's commit/rollback -- that
    wait is the writer serialization this lock exists to provide.
    """
    if not writer_lock.locked():
        return
    dbapi_connection = connection.connection.dbapi_connection
    if not getattr(dbapi_connection, "in_transaction", False):
        return
    if connection.connection is owner_fairy or get_ident() == owner_thread_id:
        raise RuntimeError(
            "Overlapping SQLite Sessions on StaticPool: another transaction "
            "is already open on the shared connection. Reuse one Session "
            "(see _sqlite_platform_session_from_request) or wait for commit."
        )


def _successor_checkout_holds_record(connection_record: Any, owner_fairy: Any) -> bool:
    """Whether a DIFFERENT checkout's fairy currently holds the pool record.

    The record's ``fairy_ref`` is overwritten at every checkout, so a
    non-None ``fairy_ref`` that is not the lock-owning fairy means a successor
    checkout exists and may already be waiting to BEGIN; the reset listener
    must then leave both the connection and the release to the checkin event.
    """
    closing_fairy = (
        connection_record.fairy_ref() if connection_record.fairy_ref else None
    )
    return closing_fairy is not None and closing_fairy is not owner_fairy


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
#   commit/roll back Session A's writes. A per-engine non-reentrant Lock is
#   acquired before BEGIN and owned by the CHECKOUT (pool connection record +
#   checked-out fairy), never by a thread: FastAPI's synchronous yielded-session
#   dependency has no thread-affinity guarantee, so the pool checkin/reset events
#   can fire on a different thread than the one that ran BEGIN (round-28
#   finding). The reset listener completes the reset's own ROLLBACK under the
#   lock before releasing and absorbs SQLAlchemy's duplicate pending call, so a
#   successor Session's fresh BEGIN can never be rolled back by a predecessor's
#   late reset. Same-checkout overlap while a txn is already open is refused
#   rather than nesting a colliding BEGIN.
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
    writer_lock = Lock()
    lock_held = False
    # The lock is owned by a CHECKOUT -- the pool connection record plus the
    # specific checked-out fairy -- never by a thread. FastAPI's synchronous
    # yielded-session dependency has no thread-affinity guarantee, so the pool
    # checkin/reset events may fire on a different thread than the one that
    # emitted BEGIN; thread-keyed ownership would strand the lock forever.
    # (A fresh fairy object is created per checkout, so fairy identity doubles
    # as the per-checkout generation marker; the record alone is ambiguous
    # because StaticPool reuses ONE record for every checkout.)
    owner_record: Any = None
    owner_fairy: Any = None
    # Recorded at begin ONLY so the overlapping-session diagnostic can fail
    # fast (RuntimeError) for same-thread misuse instead of deadlocking on a
    # non-reentrant acquire. It never authorizes a release.
    owner_thread_id: int | None = None
    # Armed on the closing thread when the reset listener releases the lock
    # while SQLAlchemy's own reset-on-return ROLLBACK is still pending; the
    # patched dialect do_rollback below consumes it so that pending rollback
    # can never execute against a successor Session's fresh BEGIN.
    absorbed_reset_rollback = local()

    @event.listens_for(engine, "connect")
    def _disable_pysqlite_transaction_management(
        dbapi_connection: Any, _connection_record: Any
    ) -> None:
        """Disable pysqlite auto-BEGIN so SAVEPOINT nests under our begin hook."""
        # Legacy-autocommit mode: pysqlite stops emitting implicit BEGINs
        # entirely; the begin hook below owns transaction starts instead.
        dbapi_connection.isolation_level = None

    def _forget_lock_owner() -> None:
        """Clear owner state and release the lock; caller has verified ownership."""
        nonlocal lock_held, owner_record, owner_fairy, owner_thread_id
        lock_held = False
        owner_record = None
        owner_fairy = None
        owner_thread_id = None
        writer_lock.release()

    def _release_writer_lock(dbapi_connection: Any, connection_record: Any) -> None:
        """Release the StaticPool writer lock on pool CHECKIN -- only for the
        owning checkout, and only when the shared connection is quiescent.

        FIX(codex rounds 22-28, five threads): thread-keyed release was wrong
        in BOTH directions. Pool events fire on whichever thread returns the
        connection, and FastAPI's synchronous dependency teardown gives no
        thread-affinity guarantee, so requiring the releasing thread to equal
        the recorded owner thread could strand the non-reentrant lock forever
        (cross-thread deadlock). Ownership is now the checked-out CONNECTION
        RECORD captured at BEGIN: (a) the lock must be held, (b) the event's
        connection record must be the one recorded at the acquisition -- so a
        stale checkout's events can never release a successor's lock -- and
        (c) the shared DBAPI connection must report ``in_transaction == False``.
        """
        nonlocal lock_held, owner_record, owner_fairy, owner_thread_id
        if not lock_held or connection_record is not owner_record:
            return
        if getattr(dbapi_connection, "in_transaction", False):
            return
        _forget_lock_owner()

    # FIX: the pool record of the BEGINning checkout is captured from the pool
    # ``checkout`` event -- the PUBLIC carrier of the record -- into this
    # thread-local, instead of reading the fairy's protected
    # ``_connection_record`` attribute (DeepSource PYL-W0212). The checkout
    # event fires on the same thread immediately before that checkout's first
    # BEGIN, so the begin hook always reads the record of its own checkout.
    _last_checkout = local()

    @event.listens_for(engine.pool, "checkout")
    def _note_checked_out_record(
        _dbapi_connection: Any, connection_record: Any, _checkout_fairy: Any
    ) -> None:
        """Hand the checked-out pool record to the begin hook on this thread."""
        _last_checkout.record = connection_record

    @event.listens_for(engine, "begin")
    def _emit_begin(connection: Connection) -> None:
        """Emit an explicit BEGIN for each outer transaction; serialize writers."""
        # FIX: Hold until the connection returns to the pool after commit/rollback
        # callbacks finish. Releasing in do_commit alone races a second Session
        # onto the still-resetting shared StaticPool connection.
        nonlocal lock_held, owner_record, owner_fairy, owner_thread_id
        _refuses_overlapping_static_pool_begin(
            connection, writer_lock, owner_fairy, owner_thread_id
        )
        writer_lock.acquire()
        owner_record = getattr(_last_checkout, "record", None)
        owner_fairy = connection.connection
        owner_thread_id = get_ident()
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
            # The acquisition above is ours and our BEGIN failed: undo it
            # unconditionally so the lock cannot strand on this error path.
            _forget_lock_owner()
            raise

    @event.listens_for(engine.pool, "checkin")
    def _release_writer_lock_on_checkin(
        dbapi_connection: Any, connection_record: Any
    ) -> None:
        """Release after the pool returns the owning checkout's connection.

        The checkin event fires AFTER the reset-on-return rollback finished
        (``_finalize_fairy`` runs ``fairy._reset`` first, then
        ``record.checkin``), so it is an authoritative release point for
        every close path that reaches it -- including a checkin on a
        different thread than the one that emitted BEGIN.
        """
        _release_writer_lock(dbapi_connection, connection_record)

    @event.listens_for(engine.pool, "reset")
    def _release_writer_lock_on_reset(
        dbapi_connection: Any, connection_record: Any, reset_state: Any
    ) -> None:
        """Complete the reset for the owning checkout and release the lock.

        FIX(codex round-28): the pool ``reset`` event fires BEFORE the
        reset-on-return ROLLBACK, so an earlier iteration that released here
        could hand the lock to a waiting Session whose fresh ``BEGIN`` was
        then rolled back by this checkout's still-running reset; and when the
        record was already checked in (``fairy_ref`` cleared, so no checkin
        event follows at all), skipping the release here leaked the lock
        permanently. This listener therefore FINISHES the reset itself while
        still holding the lock -- one synchronous ROLLBACK (a no-op once the
        connection is quiescent) -- and only then releases, arming the
        ``absorbed_reset_rollback`` flag so the patched ``do_rollback``
        swallows SQLAlchemy's now-duplicate pending call on this same thread.
        A successor checkout that acquires the lock afterwards can never have
        its BEGIN rolled back by this close, and the release no longer
        depends on a checkin event arriving.
        """
        if not lock_held or connection_record is not owner_record:
            return
        # When a successor checkout already exists (its fairy replaced
        # ``fairy_ref``) it may be waiting on this lock: leave the connection
        # alone and let the following checkin event release instead.
        if _successor_checkout_holds_record(connection_record, owner_fairy):
            return
        if not getattr(reset_state, "transaction_was_reset", False):
            engine.dialect.do_rollback(dbapi_connection)
            absorbed_reset_rollback.armed = True
        _forget_lock_owner()

    dialect = engine.dialect
    original_do_commit = dialect.do_commit
    original_do_rollback = dialect.do_rollback

    def _do_commit(dbapi_connection: Any) -> None:
        """Commit on the DBAPI connection; lock release waits for pool check-in."""
        original_do_commit(dbapi_connection)

    def _do_rollback(dbapi_connection: Any) -> None:
        """Roll back unless this call is the already-absorbed reset-on-return.

        The reset listener performs the reset's ROLLBACK itself (under the
        lock) and arms ``absorbed_reset_rollback`` on its own thread; the
        reset-on-return callback SQLAlchemy runs immediately afterwards lands
        on that same thread and is skipped, so a rollback that is provably a
        quiescent no-op can never be re-executed against a successor's BEGIN.
        Every other rollback -- including any other thread's live transaction
        rollback -- runs the original dialect call untouched.
        """
        if getattr(absorbed_reset_rollback, "armed", False):
            absorbed_reset_rollback.armed = False
            return
        original_do_rollback(dbapi_connection)

    # Deliberate instance-level monkeypatch via setattr (avoids method-assign
    # type suppressions). do_rollback additionally absorbs the duplicated
    # reset-on-return rollback described in the reset listener above.
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
