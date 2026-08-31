from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Event

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from tests.db._postgres_helpers import require_postgres_url

from ums_smart_revenue.db.rls import TENANT_CONTEXT_TABLE
from ums_smart_revenue.db.session import (
    _apply_tenant_isolation,
    build_platform_session_factory,
    build_session_factory,
    session_dependency,
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


def test_sqlite_engine_uses_one_slot_queue_pool():
    """Verify SQLite retains one connection without overlapping checkouts.

    Codex P2 review on PR #88 confirmed that
    ``join_transaction_mode="create_savepoint"`` does not actually open a
    SAVEPOINT for engine-bound sessions, so the
    production app must NOT rely on SAVEPOINT-based session isolation for
    SQLite. The codebase instead wires
    ``_sqlite_platform_session_from_request`` so the platform lane reuses
    the request session (no concurrent Session contention). This test
    pins the one-slot QueuePool so a future refactor cannot reintroduce
    multiple writers or StaticPool's overlapping-transaction rollback race.
    """
    from sqlalchemy.pool import QueuePool

    factory = build_session_factory("sqlite+pysqlite:///:memory:")
    # The sessionmaker keeps a reference to its bound engine, which is
    # the same one build_engine() returns for the URL.
    bound_engine = factory.kw["bind"]
    assert isinstance(bound_engine.pool, QueuePool), (
        "SQLite must use QueuePool so one connection cannot be checked out "
        "by overlapping request Sessions."
    )
    assert bound_engine.pool.size() == 1


def test_sqlite_request_rollback_undoes_released_repository_savepoint(tmp_path):
    """Keep nested repository writes subordinate to request rollback on SQLite."""
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'request-rollback.db').as_posix()}"
    factory = build_session_factory(database_url)
    engine = factory.kw["bind"]
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE request_rollback_probe (id INTEGER PRIMARY KEY)")

    request_session = session_dependency(factory)()
    session = next(request_session)
    with session.begin_nested():
        session.execute(sa.text("INSERT INTO request_rollback_probe (id) VALUES (1)"))

    with pytest.raises(RuntimeError, match="audit write failed"):
        request_session.throw(RuntimeError("audit write failed"))

    with engine.connect() as connection:
        persisted_rows = connection.execute(
            sa.text("SELECT count(*) FROM request_rollback_probe")
        ).scalar()
    assert persisted_rows == 0


@pytest.mark.parametrize("database_kind", ["file", "memory"])
def test_sqlite_concurrent_request_rollback_cannot_erase_committed_owner(
    tmp_path,
    database_kind,
):
    """Serialize request checkouts so one rollback cannot erase another write."""
    database_url = (
        f"sqlite+pysqlite:///{(tmp_path / 'concurrent-requests.db').as_posix()}"
        if database_kind == "file"
        else "sqlite+pysqlite:///file:pr223_concurrent?mode=memory&cache=shared&uri=true"
    )
    factory = build_session_factory(database_url)
    engine = factory.kw["bind"]
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE concurrent_probe (id INTEGER PRIMARY KEY)")

    first_request = session_dependency(factory)()
    first_session = next(first_request)
    with first_session.begin_nested():
        first_session.execute(sa.text("INSERT INTO concurrent_probe (id) VALUES (1)"))

    second_attempting = Event()
    second_acquired = Event()

    def fail_second_request_after_write() -> None:
        """Acquire after request one, write, then exercise dependency rollback."""
        second_request = session_dependency(factory)()
        second_attempting.set()
        second_session = next(second_request)
        second_acquired.set()
        with second_session.begin_nested():
            second_session.execute(sa.text("INSERT INTO concurrent_probe (id) VALUES (2)"))
        with pytest.raises(RuntimeError, match="second request failed"):
            second_request.throw(RuntimeError("second request failed"))

    with ThreadPoolExecutor(max_workers=1) as executor:
        second_result = executor.submit(fail_second_request_after_write)
        assert second_attempting.wait(timeout=5)
        # The old StaticPool implementation completed immediately with
        # ``cannot start a transaction within a transaction`` and rolled back
        # request one's shared DBAPI transaction. QueuePool must keep request
        # two waiting until request one returns the sole checkout.
        assert not second_acquired.wait(timeout=0.25)
        with pytest.raises(FutureTimeoutError):
            second_result.result(timeout=0)
        with pytest.raises(StopIteration):
            next(first_request)
        second_result.result(timeout=5)

    with engine.connect() as connection:
        persisted_ids = connection.execute(
            sa.text("SELECT id FROM concurrent_probe ORDER BY id")
        ).scalars()
        assert list(persisted_ids) == [1]
    engine.dispose()


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
        def __init__(self, value=None):
            self._value = value

        def scalar(self):
            return self._value

    class _Connection:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def __init__(self):
            self.calls = []

        def exec_driver_sql(self, sql, parameters=None):
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
