# ============================================================================
# Purpose: SQLAlchemy engine and session-factory helpers with per-URL engine
#   caching, one-slot SQLite checkout serialization, and Postgres RLS role hooks.
# Database/ORM: All ORM models (engine is the shared connection source); Session
#   factories mark tenant vs platform lanes via session.info.
# Standards: typed boundaries; no error swallowing; SQLite is test/dev-only;
#   Postgres pool_pre_ping and dual-lane role switching unchanged.
# Blast Radius: DB connection topology and transaction discipline. SQLite's
#   one-slot pool is test/dev-only; Postgres authorization/RLS is untouched.
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
from weakref import ReferenceType, WeakKeyDictionary, ref

from sqlalchemy import Connection, Engine, create_engine, event
from sqlalchemy.exc import InvalidRequestError
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

_SQLITE_OWNER_ENGINE_KEY = "ums_sqlite_owner_engine"
_sqlite_owner_lock = Lock()
_sqlite_session_owners: WeakKeyDictionary[
    Engine,
    tuple[ReferenceType[Session], int],
] = WeakKeyDictionary()


# ============================================================================
# Purpose: Refuse a same-thread second SQLite Session before it asks the
#   one-slot QueuePool for another connection. A blocking checkout on the
#   thread that owns the only live transaction can never make progress; more
#   importantly, the refusal must happen before SQLAlchemy creates a second
#   checkout fairy that could reset the shared DBAPI connection.
# Database/ORM: SQLite Session/Engine ownership metadata only; no table access.
# Standards: Fail fast with SQLAlchemy's typed InvalidRequestError. Other
#   threads still wait at QueuePool, and Postgres Session behavior is unchanged.
# Blast Radius: SQLite test/dev transaction lifecycle only. The owning
#   transaction is neither committed nor rolled back by the rejected Session.
# Connections:
#   - File: backend/ums_smart_revenue/db/session.py -> build_session_factory
#     selects this class only for SQLite engines.
#   - File: tests/db/test_session_tenant_hook.py -> pins pre-checkout refusal
#     and owner-transaction preservation.
# ============================================================================
class _SQLiteSerializedSession(Session):
    """Session that rejects a self-deadlocking second SQLite checkout."""

    def get_bind(self, *args: Any, **kwargs: Any) -> Engine | Connection:
        """Resolve the bind, then reject a same-thread non-owner Session."""
        bind = super().get_bind(*args, **kwargs)
        engine = bind if isinstance(bind, Engine) else bind.engine
        if engine.dialect.name != "sqlite":
            return bind

        with _sqlite_owner_lock:
            owner = _sqlite_session_owners.get(engine)
            if owner is None:
                return bind
            owner_session = owner[0]()
            if owner_session is None or not owner_session.in_transaction():
                _sqlite_session_owners.pop(engine, None)
                return bind
            if owner_session is not self and owner[1] == get_ident():
                # FIX: QueuePool correctly prevents a second physical checkout,
                # but waiting for its timeout on the thread that must release
                # the sole owner is a guaranteed self-deadlock. Reject before
                # Engine.connect() creates a second fairy; the rejected Session
                # consequently has no DBAPI handle it could reset or roll back.
                raise InvalidRequestError(
                    "a second SQLite Session cannot acquire the connection on "
                    "the thread that owns the active transaction"
                )
        return bind


@event.listens_for(_SQLiteSerializedSession, "after_begin")
def _record_sqlite_session_owner(
    session: Session,
    transaction: Any,
    connection: Connection,
) -> None:
    """Record the Session/thread after its real SQLite checkout begins."""
    if connection.dialect.name != "sqlite" or transaction.parent is not None:
        return
    engine = connection.engine
    with _sqlite_owner_lock:
        _sqlite_session_owners[engine] = (ref(session), get_ident())
    session.info[_SQLITE_OWNER_ENGINE_KEY] = engine


@event.listens_for(_SQLiteSerializedSession, "after_transaction_end")
def _release_sqlite_session_owner(session: Session, transaction: Any) -> None:
    """Forget the exact owner when its outer Session transaction ends."""
    if transaction.parent is not None:
        return
    engine = session.info.pop(_SQLITE_OWNER_ENGINE_KEY, None)
    if not isinstance(engine, Engine):
        return
    with _sqlite_owner_lock:
        owner = _sqlite_session_owners.get(engine)
        if owner is not None and owner[0]() is session:
            _sqlite_session_owners.pop(engine, None)


# ============================================================================
# Purpose: Open the request-owned physical SQLite transaction before route
#   services can enter repository SAVEPOINTs. SQLAlchemy's Session transaction
#   marker alone is insufficient under pysqlite legacy transaction behavior;
#   forcing the checkout invokes this module's engine ``begin`` hook, which
#   emits the one physical BEGIN without duplicating it.
# Database/ORM: All SQLite-backed request writes; no table or model changes.
# Standards: Caller-owned transaction boundary; SQLite-only compatibility;
#   PostgreSQL keeps implicit BEGIN and its RLS after_begin hook unchanged.
# Blast Radius: SQLite request atomicity. Account and audit writes remain in
#   one outer transaction and roll back together on handled route failures.
# Connections:
#   - File: backend/ums_smart_revenue/db/session.py -> SQLite begin event recipe.
#   - File: backend/ums_smart_revenue/auth/users.py -> repository SAVEPOINTs.
# ============================================================================
def begin_request_transaction(session: Session) -> None:
    """Establish the physical outer transaction for one SQLite request."""
    if session.get_bind().dialect.name != "sqlite":
        return
    # FIX: PR #223 proved that a request-level Session marker without a
    # physical BEGIN lets RELEASE SAVEPOINT commit early under pysqlite legacy
    # mode. PR #224 already owns physical BEGIN in the engine event recipe, so
    # forcing checkout here invokes exactly that hook; issuing another raw
    # BEGIN here would instead fail with "cannot start a transaction within a
    # transaction".
    session.begin()
    session.connection()


# ============================================================================
# Purpose: Build the per-URL SQLAlchemy Engine. SQLite (test-only) uses a
#   one-slot QueuePool so only one checkout can touch its persistent DBAPI
#   connection at a time. Postgres (production) keeps the normal connection
#   pool so the dual-lane design hands the tenant lane and the privileged
#   app_platform/audit lane DISTINCT role-switched connections.
# Database/ORM: All ORM models (engine is the shared connection source).
# Standards: typed boundary; no error swallowing; pool_pre_ping retained.
#   SQLite engines additionally get real BEGIN discipline (see
#   _enable_sqlite_transactional_savepoints below) so SAVEPOINTs nest instead
#   of committing. QueuePool checkout exclusivity serializes independent
#   Sessions without inferring ownership from ambiguous StaticPool events.
# Blast Radius: DB connection topology. SQLite branch is test-only; Postgres
#   path is intentionally unchanged so RLS role switching is not weakened.
# Connections:
#   - File: backend/ums_smart_revenue/app.py -> wires request + platform factories.
# ============================================================================
def build_engine(database_url: str) -> Engine:
    """Create an Engine; SQLite serializes one connection, others use defaults."""
    if database_url.startswith("sqlite"):
        # FIX: SQLite permits only ONE writer at a time. The request session and
        # the audit/platform session bind to the same engine but, under the
        # default pool, could each grab a DISTINCT DBAPI connection -> the audit
        # INSERT on connection #2 blocks on the request's open write txn on
        # connection #1 ("database is locked"). A one-slot QueuePool retains one
        # connection (including for :memory:) and prevents overlapping checkout
        # generations entirely. This is stronger than a StaticPool BEGIN lock:
        # StaticPool reset/checkin events expose only one reused ConnectionRecord,
        # so a stale fairy can roll back a newer transaction. Postgres needs
        # distinct role-switched connections, so this branch is SQLite-only.
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
# Purpose: Make SAVEPOINTs REAL on SQLite. pysqlite's legacy isolation mode
#   emits BEGIN only before DML, never before SELECT or SAVEPOINT, so a
#   ``Session.begin_nested()``
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
#   Independent Sessions serialize at the one-slot QueuePool checkout boundary.
#   Unlike StaticPool, QueuePool never hands one ConnectionRecord to overlapping
#   checkout fairies, eliminating ambiguous reset/checkin ownership and stale
#   rollback races. A checkout may safely cross threads because pysqlite's
#   check_same_thread guard is disabled for this test/dev-only engine.
# Database/ORM: Engine-level transaction discipline for every SQLite session;
#   no table, column, or query change.
# Standards: Documented dialect recipe (SQLAlchemy "Serializable isolation /
#   Savepoints / Transactional DDL" for pysqlite). Postgres path untouched —
#   psycopg already emits BEGIN, so its savepoints were always real.
# Blast Radius: SQLite (test/dev-only per build_engine) transaction semantics:
#   reads now open a real deferred transaction that holds its snapshot until
#   commit/rollback/close; concurrent Sessions wait for the sole checkout. No
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

    @event.listens_for(engine, "connect")
    def _disable_pysqlite_transaction_management(
        dbapi_connection: Any, _connection_record: Any
    ) -> None:
        """Disable pysqlite auto-BEGIN so SAVEPOINT nests under our begin hook."""
        # Legacy-autocommit mode: pysqlite stops emitting implicit BEGINs
        # entirely; the begin hook below owns transaction starts instead.
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_begin(connection: Connection) -> None:
        """Emit an explicit BEGIN for every SQLAlchemy outer transaction."""
        connection.exec_driver_sql("BEGIN")


# ============================================================================
# Purpose: Build the default app tenant-lane session factory. Sessions produced
#   here opt into the Postgres RLS role hook through session.info; raw SQLAlchemy
#   Sessions used by migrations/tests remain owner sessions unless explicitly
#   marked. SQLite concurrent Sessions serialize at the one-slot pool checkout;
#   the HTTP dual-lane path still reuses one request Session via
#   ``_sqlite_platform_session_from_request``.
# Database/ORM: SQLAlchemy Session factory metadata only.
# Standards: explicit role marker; no ambient global role changes for unmarked
#   sessions; SQLite checkout serialization scoped to the SQLite engine only.
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
    # Independent background Sessions (executor/scheduler) serialize at the
    # one-slot QueuePool checkout boundary.
    # Postgres keeps the default ("conditional_savepoint") because each
    # Session gets a distinct pooled connection with its own outer transaction.
    session_class: type[Session] = (
        _SQLiteSerializedSession if engine.dialect.name == "sqlite" else Session
    )
    return sessionmaker(
        bind=engine,
        class_=session_class,
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
