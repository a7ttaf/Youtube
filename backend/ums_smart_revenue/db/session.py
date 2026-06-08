"""SQLAlchemy engine and session-factory helpers with per-URL engine caching."""
from collections.abc import Callable, Iterator
from threading import Lock

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ums_smart_revenue.db.rls import APP_PLATFORM_ROLE, APP_TENANT_ROLE, TENANT_GUC

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
# Purpose: Per-transaction tenant isolation hook. On Postgres only, switch the
#   transaction role and set app.current_tenant_id so RLS policies filter rows.
# Database/ORM: All tenant-scoped tables (RLS policies created in 20260608_0001).
# Standards: SET LOCAL (transaction-scoped, auto-reset on commit/rollback);
#   fail-closed (no tenant context on the tenant lane => no GUC => RLS errors).
# Blast Radius: Authorization/finance reads+writes at the DB boundary. No-op on
#   SQLite and on tenant-lane sessions opened without a resolved tenant, so the
#   existing test suite and pre-S2.4 non-tenant paths are unaffected.
# Connections:
#   - File: backend/ums_smart_revenue/tenancy/context.py -> tenant in contextvar.
#   - File: backend/ums_smart_revenue/db/rls.py -> role names + GUC key.
# ============================================================================
@event.listens_for(Session, "after_begin")
def _apply_tenant_isolation(session, transaction, connection):
    """Set transaction role + tenant GUC for Postgres sessions when warranted."""
    if connection.dialect.name != "postgresql":
        return
    role = session.info.get(_SESSION_ROLE_KEY, APP_TENANT_ROLE)
    if role == APP_PLATFORM_ROLE:
        connection.exec_driver_sql(f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"')
        return
    # Default app_tenant lane: only act when a tenant is in context.
    from ums_smart_revenue.tenancy.context import get_current_tenant

    tenant = get_current_tenant()
    if tenant is None:
        return
    # ROLE is an identifier (cannot be a bind param) but is an internal
    # constant, never user input. The tenant id IS data -> set via parameterized
    # set_config (is_local=true => transaction-scoped, same lifetime as SET LOCAL).
    connection.exec_driver_sql(f'SET LOCAL ROLE "{APP_TENANT_ROLE}"')
    connection.exec_driver_sql(
        "SELECT set_config(%s, %s, true)", (TENANT_GUC, str(tenant.id))
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
