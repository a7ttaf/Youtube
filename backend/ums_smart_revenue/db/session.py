"""SQLAlchemy engine and session-factory helpers with per-URL engine caching."""

from collections.abc import Callable, Iterator
from threading import Lock
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

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
# Purpose: Open the request-owned physical SQLite transaction before route
#   services can enter repository SAVEPOINTs. The engine begin hook emits the
#   one real BEGIN when this helper forces a connection checkout.
# Database/ORM: All SQLite-backed request writes; no table or model changes.
# Standards: Caller-owned transaction boundary; SQLite-only compatibility;
#   PostgreSQL keeps its implicit BEGIN plus RLS after_begin hook unchanged.
# Blast Radius: SQLite request atomicity; authorization/audit writes roll back
#   together on handled route errors without changing PostgreSQL behavior.
# Connections:
#   - File: backend/ums_smart_revenue/api/dependencies.py -> request session owner.
#   - File: backend/ums_smart_revenue/auth/users.py -> nested account SAVEPOINTs.
# ============================================================================
def begin_request_transaction(session: Session) -> None:
    """Establish the physical outer transaction for one SQLite request."""
    if session.get_bind().dialect.name != "sqlite":
        return
    # FIX: The engine begin hook owns the raw BEGIN. Forcing checkout invokes
    # it exactly once; issuing BEGIN here as well would collide with the active
    # transaction. The one-slot QueuePool keeps other Sessions from reaching
    # the physical connection until this request returns it.
    session.begin()
    session.connection()


# ============================================================================
# Purpose: Build the per-URL SQLAlchemy Engine. SQLite (test-only) uses a
#   one-slot QueuePool so independent Sessions cannot overlap on one physical
#   connection. Postgres keeps the normal pool and distinct role-switched lanes.
# Database/ORM: All ORM models (engine is the shared connection source).
# Standards: typed boundary; no error swallowing; SQLite transaction recipe;
#   PostgreSQL pool_pre_ping retained.
# Blast Radius: DB connection topology. SQLite branch is test-only; Postgres
#   path is intentionally unchanged so RLS role switching is not weakened.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> wires request + platform factories.
# ============================================================================
def build_engine(database_url: str) -> Engine:
    """Create an Engine; SQLite serializes one connection, others use defaults."""
    if database_url.startswith("sqlite"):
        # FIX: StaticPool reissued one live DBAPI connection to overlapping
        # Session fairies. A second request's failed BEGIN/rollback could erase
        # the first request's write while the first later reported commit. A
        # one-slot QueuePool retains one connection (including for :memory:)
        # but withholds it until its current owner finishes reset/checkin.
        engine = create_engine(
            database_url,
            poolclass=QueuePool,
            pool_size=1,
            max_overflow=0,
            connect_args={"check_same_thread": False},
        )
        _enable_sqlite_transactional_savepoints(engine)
        return engine
    return create_engine(database_url, pool_pre_ping=True)


# ============================================================================
# Purpose: Make SQLite SAVEPOINTs nest under a real outer transaction. sqlite3
#   legacy mode does not BEGIN for SELECT/SAVEPOINT, so RELEASE can otherwise
#   become an early durable commit that caller rollback cannot undo.
# Database/ORM: Engine transaction discipline only; no table/model changes.
# Standards: SQLAlchemy pysqlite transaction recipe; parameter-free control
#   SQL; PostgreSQL path is untouched.
# Blast Radius: SQLite test/dev transactions now hold their outer transaction
#   until commit/rollback and independent Sessions serialize at pool checkout.
# Connections:
#   - File: backend/ums_smart_revenue/auth/users.py -> savepoint retries.
#   - File: scripts/bootstrap_operator.py -> one-transaction bootstrap.
# ============================================================================
def _enable_sqlite_transactional_savepoints(engine: Engine) -> None:
    """Emit real SQLite BEGINs so SAVEPOINT/RELEASE cannot commit early."""

    @event.listens_for(engine, "connect")
    def _disable_pysqlite_transaction_management(
        dbapi_connection: Any, _connection_record: Any
    ) -> None:
        """Disable sqlite3 auto-BEGIN so the engine hook owns transaction start."""
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_begin(connection: Connection) -> None:
        """Open the physical transaction whenever SQLAlchemy begins an outer one."""
        connection.exec_driver_sql("BEGIN")


# ============================================================================
# Purpose: Build the default app tenant-lane session factory. Sessions produced
#   here opt into the Postgres RLS role hook through session.info; raw SQLAlchemy
#   Sessions used by migrations/tests remain owner sessions unless explicitly
#   marked. SQLite Sessions serialize at the one-slot checkout boundary; the
#   HTTP platform/audit lane still reuses its request Session.
# Database/ORM: SQLAlchemy Session factory metadata only.
# Standards: explicit role marker; no ambient global role changes for unmarked
#   sessions; SQLite checkout serialization stays engine-owned.
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
    # SAVEPOINT for these engine-bound sessions. The audit / platform lane does
    # not need a separate Session on SQLite: the app wires
    # `_sqlite_platform_session_from_request` in `app.py` so the platform lane
    # reuses the request session (the same Session object), which removes the
    # multi-Session contention that would otherwise need SAVEPOINT isolation.
    # Independent Sessions serialize at QueuePool checkout.
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
                begin_request_transaction(session)
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    return dependency
