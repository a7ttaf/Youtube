"""SQLAlchemy engine and session-factory helpers with per-URL engine caching."""
from collections.abc import Callable, Iterator
from threading import Lock

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.db.rls import (
    APP_PLATFORM_ROLE,
    APP_TENANT_ROLE,
    TENANT_CONTEXT_CLEARER,
    TENANT_CONTEXT_SETTER,
)

SessionFactory = sessionmaker[Session]

_SESSION_ROLE_KEY = "ums_db_role"

_engine_cache: dict[str, Engine] = {}
_engine_cache_lock = Lock()


def build_engine(database_url: str) -> Engine:
    """Create a connection-pooled SQLAlchemy Engine for ``database_url``."""
    return create_engine(database_url, pool_pre_ping=True)


def build_session_factory(
    database_url: str, engine: Engine | None = None
) -> SessionFactory:
    """Return a sessionmaker bound to a per-URL cached engine (or the given one)."""
    if engine is None:
        with _engine_cache_lock:
            if database_url not in _engine_cache:
                _engine_cache[database_url] = build_engine(database_url)
            engine = _engine_cache[database_url]
    return sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)


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
#   fail-closed (missing tenant context => no row => RLS policy rejects access).
# Blast Radius: Authorization/finance reads+writes at the DB boundary. No-op on
#   SQLite and on tenant-lane sessions opened without a resolved tenant, so the
#   existing test suite and pre-S2.4 non-tenant paths are unaffected.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/context.py -> tenant in contextvar.
#   - File: backend/ums_smart_revenue/db/rls.py -> role names + tenant context helpers.
# ============================================================================
@event.listens_for(Session, "after_begin")
def _apply_tenant_isolation(session, _transaction, connection):
    """Set transaction role + trusted tenant context for Postgres sessions."""
    if connection.dialect.name != "postgresql":
        return
    role = session.info.get(_SESSION_ROLE_KEY, APP_TENANT_ROLE)
    # Default app_tenant lane: only act when a tenant is in context.
    from ums_smart_revenue.tenancy.context import get_current_tenant

    tenant = get_current_tenant()
    if tenant is not None:
        # Tolerate schemas built via metadata.create_all (no RLS migration): the
        # hook no-ops when the setter function is absent rather than erroring.
        if _tenant_context_fn_exists(connection, f"{TENANT_CONTEXT_SETTER}(uuid)"):
            # Elevate to app_platform only to run the trusted setter, so tenant
            # code cannot mutate the context through the tenant lane.
            connection.exec_driver_sql(f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"')
            connection.exec_driver_sql(
                f"SELECT {TENANT_CONTEXT_SETTER}(%s)",
                (str(tenant.id),),
            )
    elif _tenant_context_fn_exists(connection, f"{TENANT_CONTEXT_CLEARER}()"):
        # FIX: Clear any stale pooled-connection context via the SECURITY DEFINER
        # clearer (runs as its owner) instead of a raw DELETE; the app lanes hold
        # only SELECT on the context table, so the prior DELETE permission-denied.
        # No role elevation needed; tolerant of the absent function
        # (metadata.create_all PG schemas) by skipping the call.
        connection.exec_driver_sql(f"SELECT {TENANT_CONTEXT_CLEARER}()")
    if role == APP_PLATFORM_ROLE:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"')
    elif tenant is not None:
        # Drop into the restricted app_tenant lane only once a tenant context row
        # is set; a context-less tenant-lane session stays on the login role
        # (fail-closed: no context row => RLS exposes no rows), and the
        # pooled-reset path leaves a reused connection off app_tenant.
        connection.exec_driver_sql(f'SET LOCAL ROLE "{APP_TENANT_ROLE}"')


def _tenant_context_fn_exists(connection, signature: str) -> bool:
    """Return True if the named public tenant-context function is installed."""
    # Cheap per-call to_regprocedure probe (accepts a full arg signature): NULL
    # when the function is absent (e.g. a metadata.create_all schema with no RLS
    # migration applied), so the hook no-ops instead of erroring.
    return (
        connection.exec_driver_sql(
            f"SELECT to_regprocedure('public.{signature}') IS NOT NULL"
        ).scalar()
        is True
    )


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
