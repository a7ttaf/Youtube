# ============================================================================
# Purpose: Session factory tenant-lane / platform-lane hooks and one-slot SQLite
#   checkout serialization.
# Database/ORM: SQLite QueuePool engines; optional disposable Postgres via
#   UMS_TEST_DATABASE_URL for RLS role assertions.
# Standards: Fail-closed lock release; no suppressions.
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/db/session.py -> subject.
# ============================================================================
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from threading import Event

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import InvalidRequestError
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
    pins the one-slot QueuePool choice so a future refactor cannot reintroduce
    either multiple SQLite writers or StaticPool's overlapping fairy resets.
    """
    from sqlalchemy.pool import QueuePool

    factory = build_session_factory("sqlite+pysqlite:///:memory:")
    # The sessionmaker keeps a reference to its bound engine, which is
    # the same one build_engine() returns for the URL.
    bound_engine = factory.kw["bind"]
    assert isinstance(bound_engine.pool, QueuePool), (
        "SQLite must use QueuePool so one ConnectionRecord cannot be checked "
        "out by overlapping connection fairies."
    )
    assert bound_engine.pool.size() == 1


def test_sqlite_overlapping_sessions_serialize_instead_of_colliding_begin():
    """Two SQLite Sessions must serialize checkouts, not collide on BEGIN.

    Qodo #8 / PR #210: the pysqlite BEGIN recipe makes SAVEPOINT real, but an
    unconditional second BEGIN on the shared DBAPI connection raises
    ``cannot start a transaction within a transaction`` (or lets Session B
    commit Session A's writes). The one-slot pool must let both Sessions
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
        else "sqlite+pysqlite:///file:pr224_concurrent?mode=memory&cache=shared&uri=true"
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
        # FIX: StaticPool let request two reach the same live DBAPI transaction;
        # its failed BEGIN/cleanup could roll request one's data back. The
        # one-slot QueuePool must hold request two before checkout until request
        # one commits and returns the physical connection.
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


def test_sqlite_checkout_returns_when_session_closes_on_another_thread():
    """A cross-thread Session close must return the sole SQLite checkout.

    Round-28 Qodo pair: FastAPI's synchronous yielded-session dependency has
    no thread-affinity guarantee, so the pool checkin/reset events can fire on
    a different thread than the one that emitted BEGIN. The previous
    thread-identity release guard made the old non-reentrant lock permanently
    held in that case. QueuePool itself owns checkout return, so a close from
    any thread must let a subsequent Session acquire the sole connection.
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
            session.execute(text("INSERT INTO ums_cross_thread_probe (v) VALUES ('a')"))
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

    # A subsequent Session must be able to acquire the returned connection.
    release_check: dict[str, object] = {}

    def _verify_successor_can_begin() -> None:
        """Begin a fresh transaction; hang here if checkout return failed."""
        try:
            with factory() as successor:
                successor.execute(text("INSERT INTO ums_cross_thread_probe (v) VALUES ('b')"))
                successor.commit()
            release_check["ok"] = True
        except Exception as exc:  # pragma: no cover - surfaces as assertion below
            release_check["error"] = repr(exc)

    verifier = threading.Thread(target=_verify_successor_can_begin, daemon=True)
    verifier.start()
    verifier.join(timeout=10)
    assert not verifier.is_alive(), (
        "the sole SQLite checkout was not returned by the cross-thread close"
    )
    assert release_check == {"ok": True}, f"successor failed: {release_check!r}"
    # Thread A's INSERT was intentionally left uncommitted, so the cross-thread
    # close correctly rolled it back via reset-on-return; the successor's
    # committed row is the only durable one.
    with factory() as check:
        values = set(check.execute(text("SELECT v FROM ums_cross_thread_probe")).scalars())
    assert values == {"b"}


def test_sqlite_reset_then_checkin_returns_connection_cleanly():
    """Reset plus checkin must return the one-slot connection cleanly.

    Round-28: a normal COMMIT leaves ``in_transaction == False`` while the pool
    reset event still fires BEFORE the reset-on-return rollback, and SQLAlchemy
    can emit reset/checkin pairs where no checkin follows the reset at all.
    The prior custom lock tried to infer ownership across both events, which
    could double-release or hand the shared connection to a successor before a
    stale rollback completed. QueuePool performs one ordered return; successor
    Sessions must begin, commit, and close cleanly.
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
    # QueuePool must make one clean connection return across the pair.
    with factory() as owner:
        owner.execute(text("INSERT INTO ums_reset_probe (v) VALUES ('owner')"))
        owner.commit()

    # Successor Sessions must begin and commit cleanly; a corrupted lock state
    # (failed return or stale reset) surfaces here.
    for index in range(3):
        with factory() as successor:
            successor.execute(
                text("INSERT INTO ums_reset_probe (v) VALUES (:v)"), {"v": f"s{index}"}
            )
            successor.commit()

    with factory() as check:
        values = set(check.execute(text("SELECT v FROM ums_reset_probe")).scalars())
    assert values == {"owner", "s0", "s1", "s2"}


def test_sqlite_checkout_identity_survives_cross_thread_first_begin():
    """A checkout on thread A may first BEGIN and close on thread B.

    FastAPI may create a synchronous dependency on one worker thread and run
    its first database operation or teardown on another. The pool must permit
    that checkout's first BEGIN and return without any thread-affine state.
    """
    import threading

    from sqlalchemy import text

    factory = build_session_factory(
        "sqlite+pysqlite:///file:ums_cross_thread_first_begin?mode=memory&cache=shared&uri=true"
    )
    engine = factory.kw["bind"]
    with engine.begin() as setup:
        setup.execute(text("CREATE TABLE ums_cross_begin_probe (v TEXT)"))

    # Checkout happens on the pytest thread; first BEGIN, commit, and checkin
    # happen on the worker. A successor checkout in that worker detects a
    # stranded pool return without blocking the pytest process indefinitely.
    connection = engine.connect()
    outcome: dict[str, object] = {}

    def _begin_and_close_on_another_thread() -> None:
        """Use and return a checkout created by the parent thread."""
        try:
            connection.execute(text("INSERT INTO ums_cross_begin_probe (v) VALUES ('cross')"))
            connection.commit()
            connection.close()
            with engine.begin() as successor:
                successor.execute(
                    text("INSERT INTO ums_cross_begin_probe (v) VALUES ('successor')")
                )
            outcome["ok"] = True
        except Exception as exc:  # pragma: no cover - surfaced below
            outcome["error"] = repr(exc)

    worker = threading.Thread(target=_begin_and_close_on_another_thread, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), (
        "checkout on thread A followed by BEGIN/checkin on thread B stranded "
        "the one-slot SQLite pool"
    )
    assert outcome == {"ok": True}, f"cross-thread checkout failed: {outcome!r}"

    with engine.connect() as check:
        values = set(check.execute(text("SELECT v FROM ums_cross_begin_probe")).scalars())
    assert values == {"cross", "successor"}


def test_sqlite_same_checkout_can_rebegin_without_new_checkout():
    """One checked-out Connection may commit and BEGIN again without deadlock.

    Core ``Connection.commit()`` ends the SQLite transaction but deliberately
    keeps the same pool checkout open. Its next implicit BEGIN must use that
    retained exclusive connection without attempting another pool checkout.
    """
    import threading

    from sqlalchemy import text

    factory = build_session_factory(
        "sqlite+pysqlite:///file:ums_same_checkout_rebegin?mode=memory&cache=shared&uri=true"
    )
    engine = factory.kw["bind"]
    with engine.begin() as setup:
        setup.execute(text("CREATE TABLE ums_rebegin_probe (v TEXT)"))

    outcome: dict[str, object] = {}

    def _commit_twice_on_one_checkout() -> None:
        """Run two outer transactions without returning the checkout."""
        try:
            with engine.connect() as connection:
                connection.execute(text("INSERT INTO ums_rebegin_probe (v) VALUES ('first')"))
                connection.commit()
                connection.execute(text("INSERT INTO ums_rebegin_probe (v) VALUES ('second')"))
                connection.commit()
            outcome["ok"] = True
        except Exception as exc:  # pragma: no cover - surfaced below
            outcome["error"] = repr(exc)

    worker = threading.Thread(target=_commit_twice_on_one_checkout, daemon=True)
    worker.start()
    worker.join(timeout=5)
    assert not worker.is_alive(), "the retained SQLite checkout deadlocked during its second BEGIN"
    assert outcome == {"ok": True}, f"same-checkout re-BEGIN failed: {outcome!r}"

    with engine.connect() as check:
        values = set(check.execute(text("SELECT v FROM ums_rebegin_probe")).scalars())
    assert values == {"first", "second"}


def test_sqlite_second_checkout_waits_for_live_owner_return():
    """A second checkout cannot overlap an owner's live transaction.

    This is the safety property StaticPool could not provide: the pool itself
    must withhold its sole ConnectionRecord until the owner finishes reset and
    checkin, so no non-owner fairy can roll back or commit the owner's writes.
    """
    import threading

    from sqlalchemy import text

    factory = build_session_factory(
        "sqlite+pysqlite:///file:ums_live_checkout_exclusive?mode=memory&cache=shared&uri=true"
    )
    engine = factory.kw["bind"]
    with engine.begin() as setup:
        setup.execute(text("CREATE TABLE ums_live_checkout_probe (v TEXT)"))

    owner = engine.connect()
    owner.execute(text("INSERT INTO ums_live_checkout_probe (v) VALUES ('owner')"))
    attempting_checkout = threading.Event()
    checked_out = threading.Event()
    outcome: dict[str, object] = {}

    def _waiting_successor() -> None:
        """Acquire only after the live owner returns the sole connection."""
        try:
            attempting_checkout.set()
            with engine.connect() as successor:
                checked_out.set()
                successor.execute(
                    text("INSERT INTO ums_live_checkout_probe (v) VALUES ('successor')")
                )
                successor.commit()
            outcome["ok"] = True
        except Exception as exc:  # pragma: no cover - surfaced below
            outcome["error"] = repr(exc)
            checked_out.set()

    worker = threading.Thread(target=_waiting_successor, daemon=True)
    worker.start()
    assert attempting_checkout.wait(timeout=5), "successor never attempted checkout"
    checked_out_before_return = checked_out.wait(timeout=1)
    owner.commit()
    owner.close()
    worker.join(timeout=5)
    assert not worker.is_alive(), "successor checkout hung after owner return"
    assert not checked_out_before_return, "SQLite allowed overlapping live checkouts"
    assert outcome == {"ok": True}, f"successor failed: {outcome!r}"

    with engine.connect() as check:
        values = set(check.execute(text("SELECT v FROM ums_live_checkout_probe")).scalars())
    assert values == {"owner", "successor"}


def test_sqlite_second_checkout_waits_while_owner_is_idle_after_commit():
    """COMMIT does not release a Core Connection's exclusive checkout.

    The old StaticPool lock was vulnerable while the owner's DBAPI transaction
    was quiescent: another fairy could reset the shared record, release by the
    wrong identity, and enable a stale rollback race. QueuePool must keep later
    checkouts blocked until the idle owner closes its Connection.
    """
    import threading

    from sqlalchemy import text

    factory = build_session_factory(
        "sqlite+pysqlite:///file:ums_idle_checkout_exclusive?mode=memory&cache=shared&uri=true"
    )
    engine = factory.kw["bind"]
    with engine.begin() as setup:
        setup.execute(text("CREATE TABLE ums_idle_checkout_probe (v TEXT)"))

    owner = engine.connect()
    owner.execute(text("INSERT INTO ums_idle_checkout_probe (v) VALUES ('owner')"))
    owner.commit()
    attempting_checkout = threading.Event()
    checked_out = threading.Event()
    outcome: dict[str, object] = {}

    def _waiting_successor() -> None:
        """Wait for an idle but still checked-out owner Connection."""
        try:
            attempting_checkout.set()
            with engine.connect() as successor:
                checked_out.set()
                successor.execute(
                    text("INSERT INTO ums_idle_checkout_probe (v) VALUES ('successor')")
                )
                successor.commit()
            outcome["ok"] = True
        except Exception as exc:  # pragma: no cover - surfaced below
            outcome["error"] = repr(exc)
            checked_out.set()

    worker = threading.Thread(target=_waiting_successor, daemon=True)
    worker.start()
    assert attempting_checkout.wait(timeout=5), "successor never attempted checkout"
    checked_out_before_return = checked_out.wait(timeout=1)
    owner.close()
    worker.join(timeout=5)
    assert not worker.is_alive(), "successor checkout hung after idle owner close"
    assert not checked_out_before_return, "SQLite allowed overlap with an idle owner checkout"
    assert outcome == {"ok": True}, f"successor failed: {outcome!r}"

    with engine.connect() as check:
        values = set(check.execute(text("SELECT v FROM ums_idle_checkout_probe")).scalars())
    assert values == {"owner", "successor"}


def test_sqlite_same_thread_second_session_refuses_before_checkout_without_rollback(
    tmp_path,
):
    """Reject a self-deadlocking Session without touching its owner's transaction.

    A one-slot QueuePool serializes different threads safely, but a second
    Session on the owner thread cannot wait for itself to return the only
    connection. The refusal must occur before another pool checkout and the
    rejected Session's rollback/close must not reach the owner's DBAPI handle.
    """
    from sqlalchemy import event, text

    database_url = f"sqlite+pysqlite:///{(tmp_path / 'same-thread-owner.db').as_posix()}"
    factory = build_session_factory(database_url)
    engine = factory.kw["bind"]
    with engine.begin() as setup:
        setup.execute(text("CREATE TABLE ums_same_thread_probe (v TEXT)"))

    checkout_count = 0

    def _count_checkout(*_args) -> None:
        """Count physical pool checkouts, not Session transaction markers."""
        nonlocal checkout_count
        checkout_count += 1

    event.listen(engine, "checkout", _count_checkout)
    try:
        owner = factory()
        owner.execute(text("INSERT INTO ums_same_thread_probe (v) VALUES ('owner')"))
        owner_checkout_count = checkout_count
        assert owner_checkout_count == 1

        rejected = factory()
        with pytest.raises(
            InvalidRequestError,
            match="second SQLite Session cannot acquire",
        ):
            rejected.execute(text("SELECT count(*) FROM ums_same_thread_probe"))
        rejected.rollback()
        rejected.close()

        assert checkout_count == owner_checkout_count, (
            "the rejected Session reached QueuePool and created a second checkout fairy"
        )
        assert owner.in_transaction(), "the rejected Session rolled back its owner"
        owner.commit()
        owner.close()
    finally:
        event.remove(engine, "checkout", _count_checkout)

    with engine.connect() as check:
        values = set(check.execute(text("SELECT v FROM ums_same_thread_probe")).scalars())
    assert values == {"owner"}


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
