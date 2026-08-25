import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from tests.db._postgres_helpers import require_postgres_url

from ums_smart_revenue.db.rls import TENANT_CONTEXT_TABLE
from ums_smart_revenue.db.session import (
    _apply_tenant_isolation,
    build_platform_session_factory,
    build_session_factory,
)
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

_UPGRADED_URLS: set[str] = set()


def _tenant(uuid_str: str) -> Tenant:
    """Build a deterministic tenant object for session-hook tests."""
    from datetime import UTC, datetime
    from uuid import UUID

    now = datetime(2026, 1, 1, tzinfo=UTC)
    return Tenant(
        id=UUID(uuid_str),
        slug="ums",
        display_name="UMS",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )


def _ensure_upgraded(url: str) -> None:
    """Upgrade the disposable Postgres database before session-hook assertions."""
    if url in _UPGRADED_URLS:
        return
    cfg = Config("alembic.ini")
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    _UPGRADED_URLS.add(url)


def test_sqlite_session_issues_no_set_statements():
    """Verify the SQLite session hook stays a no-op for tenant context."""
    # On SQLite the hook must be a complete no-op (no SET ROLE / tenant context).
    factory = build_session_factory("sqlite+pysqlite:///:memory:")
    token = TENANT_CTX.set(_tenant("00000000-0000-0000-0000-000000000001"))
    try:
        with factory() as session:
            # A trivial query must not raise (no Postgres-only SQL emitted).
            assert session.execute(sa.text("SELECT 1")).scalar() == 1
    finally:
        TENANT_CTX.reset(token)


def test_sqlite_engine_uses_static_pool_for_shared_connection():
    """Verify the SQLite engine shares one DBAPI connection via StaticPool.

    Codex P2 review on PR #88 confirmed that
    ``join_transaction_mode="create_savepoint"`` does not actually open a
    SAVEPOINT for engine-bound sessions on a StaticPool engine, so the
    production app must NOT rely on SAVEPOINT-based session isolation for
    SQLite. The codebase instead wires
    ``_sqlite_platform_session_from_request`` so the platform lane reuses
    the request session (no concurrent Session contention). This test
    pins the StaticPool choice so any future refactor that drops it gets
    caught before it can reintroduce the original "database is locked"
    contention.
    """
    from sqlalchemy.pool import StaticPool

    factory = build_session_factory("sqlite+pysqlite:///:memory:")
    # The sessionmaker keeps a reference to its bound engine, which is
    # the same one build_engine() returns for the URL.
    bound_engine = factory.kw["bind"]
    assert isinstance(bound_engine.pool, StaticPool), (
        "SQLite must use StaticPool so the request session and any "
        "co-tenanted use share one DBAPI connection."
    )


def test_sqlite_overlapping_sessions_serialize_instead_of_colliding_begin():
    """Two threads on StaticPool must serialize writers, not collide on BEGIN.

    Qodo #8 / PR #210: the pysqlite BEGIN recipe makes SAVEPOINT real, but an
    unconditional second BEGIN on the shared DBAPI connection raises
    ``cannot start a transaction within a transaction`` (or lets Session B
    commit Session A's writes). The engine writer lock must let both Sessions
    complete without that OperationalError while preserving nested savepoints.
    """
    import threading

    from sqlalchemy import text

    factory = build_session_factory(
        "sqlite+pysqlite:///file:ums_writer_lock_test?mode=memory&cache=shared&uri=true"
    )
    with factory() as setup:
        setup.execute(text("CREATE TABLE ums_writer_probe (id INTEGER PRIMARY KEY, v TEXT)"))
        setup.commit()

    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def _writer(label: str) -> None:
        """Test helper ``_writer``."""
        try:
            with factory() as session:
                barrier.wait(timeout=5)
                session.execute(
                    text("INSERT INTO ums_writer_probe (v) VALUES (:v)"),
                    {"v": label},
                )
                session.commit()
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=_writer, args=("a",)),
        threading.Thread(target=_writer, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert errors == [], f"overlapping SQLite Sessions failed: {errors!r}"
    with factory() as check:
        values = set(check.execute(text("SELECT v FROM ums_writer_probe")).scalars())
    assert values == {"a", "b"}


def test_sqlite_begin_nested_still_releases_instead_of_committing():
    """Nested SAVEPOINT RELEASE must not durable-commit under the BEGIN recipe."""
    from sqlalchemy import text

    factory = build_session_factory(
        "sqlite+pysqlite:///file:ums_savepoint_release_test?mode=memory&cache=shared&uri=true"
    )
    with factory() as session:
        session.execute(text("CREATE TABLE ums_savepoint_probe (id INTEGER PRIMARY KEY)"))
        session.commit()

    with factory() as session:
        session.begin()
        with session.begin_nested():
            session.execute(text("INSERT INTO ums_savepoint_probe (id) VALUES (1)"))
        session.rollback()

    with factory() as check:
        assert check.execute(text("SELECT count(*) FROM ums_savepoint_probe")).scalar() == 0


def test_postgres_tenant_lane_sets_role_and_trusted_tenant_context():
    """Verify the tenant lane sets app_tenant and the trusted tenant context."""
    url = require_postgres_url()
    _ensure_upgraded(url)
    factory = build_session_factory(url)
    tid = "00000000-0000-0000-0000-000000000001"
    token = TENANT_CTX.set(_tenant(tid))
    try:
        with factory() as session:
            assert str(session.execute(sa.text("SELECT app_current_tenant_id()")).scalar()) == tid
            assert session.execute(sa.text("SELECT current_user")).scalar() == "app_tenant"
    finally:
        TENANT_CTX.reset(token)


def test_postgres_no_context_stays_on_tenant_role_and_unset_context():
    """Verify the tenant lane fails closed without trusted tenant context."""
    url = require_postgres_url()
    _ensure_upgraded(url)
    factory = build_session_factory(url)
    # No TENANT_CTX leaves app_tenant active, but clears the trusted context row
    # so RLS policies reject tenant rows instead of falling back to the owner role.
    with factory() as session:
        assert session.execute(sa.text("SELECT app_current_tenant_id()")).scalar() is None
        assert session.execute(sa.text("SELECT current_user")).scalar() == "app_tenant"


def test_no_context_clears_stale_context_when_clear_helper_is_absent():
    """Missing clear helper must not leave a stale tenant row on pooled backends."""

    class _Result:
        """Test double / helper class ``_Result``."""

        def __init__(self, value=None):
            """Test helper ``__init__``."""
            self._value = value

        def scalar(self):
            """Test helper ``scalar``."""
            return self._value

    class _Connection:
        """Test double / helper class ``_Connection``."""

        dialect = type("Dialect", (), {"name": "postgresql"})()

        def __init__(self):
            """Test helper ``__init__``."""
            self.calls = []

        def exec_driver_sql(self, sql, parameters=None):
            """Test helper ``exec_driver_sql``."""
            self.calls.append((sql, parameters))
            if sql == "SELECT to_regprocedure(%s) IS NOT NULL":
                return _Result(False)
            return _Result()

    session = type("Session", (), {"info": {"ums_db_role": "app_tenant"}})()
    connection = _Connection()
    token = TENANT_CTX.set(None)
    try:
        _apply_tenant_isolation(session, None, connection)
    finally:
        TENANT_CTX.reset(token)

    assert any(
        sql == f"DELETE FROM {TENANT_CONTEXT_TABLE} WHERE backend_pid = pg_backend_pid()"
        for sql, _parameters in connection.calls
    )


def test_platform_lane_uses_app_platform_and_no_tenant_context():
    """Verify the platform lane uses app_platform without tenant context."""
    url = require_postgres_url()
    _ensure_upgraded(url)
    factory = build_platform_session_factory(url)
    with factory() as session:
        assert session.execute(sa.text("SELECT current_user")).scalar() == "app_platform"
        assert session.execute(sa.text("SELECT app_current_tenant_id()")).scalar() is None


def test_pooled_connection_does_not_leak_role_or_context():
    """Verify a reused pooled connection does not retain tenant session state."""
    # Transaction 1 sets tenant lane; transaction 2 on the SAME pooled
    # connection (no context) must see no leaked role/tenant context.
    url = require_postgres_url()
    _ensure_upgraded(url)
    engine = sa.create_engine(url, pool_size=1, max_overflow=0)
    factory = build_session_factory(url, engine=engine)
    tid = "00000000-0000-0000-0000-000000000001"
    token = TENANT_CTX.set(_tenant(tid))
    try:
        with factory() as s1:
            assert s1.execute(sa.text("SELECT current_user")).scalar() == "app_tenant"
            s1.commit()
    finally:
        TENANT_CTX.reset(token)
    # Reuse the pool with no context; role stays restricted and context is empty.
    with factory() as s2:
        assert s2.execute(sa.text("SELECT current_user")).scalar() == "app_tenant"
        assert s2.execute(sa.text("SELECT app_current_tenant_id()")).scalar() is None
    engine.dispose()
