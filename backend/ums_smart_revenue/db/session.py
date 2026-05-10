from collections.abc import Callable, Iterator
from typing import Optional

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker


SessionFactory = sessionmaker[Session]

_engine_cache: dict[str, Engine] = {}


def build_engine(database_url: str) -> Engine:
    return create_engine(database_url, pool_pre_ping=True)


def build_session_factory(database_url: str, engine: Optional[Engine] = None) -> SessionFactory:
    if engine is None:
        if database_url not in _engine_cache:
            _engine_cache[database_url] = build_engine(database_url)
        engine = _engine_cache[database_url]
    return sessionmaker(bind=engine, autoflush=True, expire_on_commit=False)


def session_dependency(session_factory: SessionFactory) -> Callable[[], Iterator[Session]]:
    def dependency() -> Iterator[Session]:
        with session_factory() as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    return dependency
