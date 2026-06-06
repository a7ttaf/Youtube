"""SQLAlchemy engine and session-factory helpers with per-URL engine caching."""
from collections.abc import Callable, Iterator
from threading import Lock

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

SessionFactory = sessionmaker[Session]

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
