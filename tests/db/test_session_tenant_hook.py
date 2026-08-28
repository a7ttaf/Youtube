# ============================================================================
# Purpose: Session factory tenant-lane / platform-lane hooks and SQLite
#   StaticPool writer-lock serialization.
# Database/ORM: SQLite StaticPool engines; optional disposable Postgres via
#   UMS_TEST_DATABASE_URL for RLS role assertions.
# Standards: Fail-closed lock release; no suppressions.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/db/session.py -> subject.
# ============================================================================
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


def test_sqlite_writer_lock_releases_when_checkin_runs_on_another_thread():
    """A cross-thread session close must release the StaticPool writer lock.

    Round-28 Qodo pair: FastAPI's synchronous yielded-session dependency has
    no thread-affinity guarantee, so the pool checkin/reset events can fire on
    a different thread than the one that emitted BEGIN. The previous
    thread-identity release guard made the non-reentrant lock permanently held
    in that case. Ownership is now the owning checkout's connection record, so
    the owning checkout's close from ANY thread releases, and a subsequent
    Session must be able to begin.
    """
    import threading

    from sqlalchemy import text

    factory = build_session_factory(
        "sqlite+pysqlite:///file:ums_cross_thread_release?mode=memory&cache=shared&uri=true"
    )
    with factory() as setup:
        setup.execute(text("CREATE TABLE ums_cross_thread_probe (v TEXT)"))
        setup.commit()

    began = threading.Event()
    closed = threading.Event()
    errors: list[Exception] = []
    holder: dict[str, object] = {}

    def _begin_on_thread_a() -> None:
        """Begin a transaction on thread A and leave the checkout open."""
        try:
            session = factory()
            holder["session"] = session
            session.execute(
                text("INSERT INTO ums_cross_thread_probe (v) VALUES ('a')")
            )
            began.set()
            # The connection is returned from thread B, NOT from this thread:
            # exactly the dependency-teardown shape that stranded the old
            # thread-identity release guard. This thread idles until B's close
            # has completed.
            closed.wait(timeout=10)
        except Exception as exc:  # pragma: no cover - surfaces as assertion below
            errors.append(exc)
            began.set()
            closed.set()

    def _close_on_thread_b() -> None:
        """Return the owning checkout's connection from thread B."""
        try:
            session = holder["session"]
            session.close()
        except Exception as exc:  # pragma: no cover - surfaces as assertion below
            errors.append(exc)
        finally:
            closed.set()

    thread_a = threading.Thread(target=_begin_on_thread_a)
    thread_b = threading.Thread(target=_close_on_thread_b)
    thread_a.start()
    assert began.wait(timeout=10), "thread A never began its transaction"
    thread_b.start()
    assert closed.wait(timeout=10), "thread B never closed the session"
    thread_a.join(timeout=10)
    thread_b.join(timeout=10)
    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert errors == [], f"cross-thread close failed: {errors!r}"

    # A subsequent Session must be able to BEGIN: the lock is free.
    release_check: dict[str, object] = {}

    def _verify_successor_can_begin() -> None:
        """Begin a fresh transaction; hang here if the lock leaked."""
        try:
            with factory() as successor:
                successor.execute(
                    text("INSERT INTO ums_cross_thread_probe (v) VALUES ('b')")
                )
                successor.commit()
            release_check["ok"] = True
        except Exception as exc:  # pragma: no cover - surfaces as assertion below
            release_check["error"] = repr(exc)

    verifier = threading.Thread(target=_verify_successor_can_begin, daemon=True)
    verifier.start()
    verifier.join(timeout=10)
    assert not verifier.is_alive(), (
        "the writer lock leaked across the cross-thread close: a successor "
        "Session could not begin"
    )
    assert release_check == {"ok": True}, f"successor failed: {release_check!r}"
    # Thread A's INSERT was intentionally left uncommitted, so the cross-thread
    # close correctly rolled it back via reset-on-return; the successor's
    # committed row is the only durable one.
    with factory() as check:
        values = set(check.execute(text("SELECT v FROM ums_cross_thread_probe")).scalars())
    assert values == {"b"}


def test_sqlite_owning_record_reset_then_checkin_releases_exactly_once():
    """The owning checkout's reset+checkin close must release exactly once.

    Round-28: a normal COMMIT leaves ``in_transaction == False`` while the pool
    reset event still fires BEFORE the reset-on-return rollback, and SQLAlchemy
    can emit reset/checkin pairs where no checkin follows the reset at all.
    Releasing on both events must therefore stay idempotent: a stray second
    release of ``threading.Lock`` raises ``RuntimeError: release unlocked
    lock``, and a lock handed over while a predecessor's reset is still pending
    would roll back the successor's BEGIN. After the owning session closes,
    successor Sessions must begin, commit, and close cleanly.
    """
    from sqlalchemy import text

    factory = build_session_factory(
        "sqlite+pysqlite:///file:ums_reset_checkin_once?mode=memory&cache=shared&uri=true"
    )
    with factory() as setup:
        setup.execute(text("CREATE TABLE ums_reset_probe (v TEXT)"))
        setup.commit()

    # Owning checkout: INSERT + COMMIT (reset fires with in_transaction False),
    # then close -- both events target the same owning record. Exactly one
    # release may happen across the pair.
    with factory() as owner:
        owner.execute(text("INSERT INTO ums_reset_probe (v) VALUES ('owner')"))
        owner.commit()

    # Successor Sessions must begin and commit cleanly; a corrupted lock state
    # (double release, or a release that lands mid-reset) surfaces here.
    for index in range(3):
        with factory() as successor:
            successor.execute(
                text("INSERT INTO ums_reset_probe (v) VALUES (:v)"), {"v": f"s{index}"}
            )
            successor.commit()

    with factory() as check:
        values = set(check.execute(text("SELECT v FROM ums_reset_probe")).scalars())
    assert values == {"owner", "s0", "s1", "s2"}


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
