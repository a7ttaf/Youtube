"""Unit tests for the ``platform_lane`` single-session elevation helper.

These pin the contract the connector run path depends on (MEASURED semantics --
the spec'd Finding 4 behavior correction; the prior unconditional-restore tests
were rewritten here on purpose):

* SQLite (and any non-Postgres dialect) -> complete no-op: no SET LOCAL ROLE
  statements are emitted, so the helper is transparent to the test tier.
* Postgres tenant-lane session (``session.info`` marks ``app_tenant``) with the
  entry transaction still active and healthy on exit -> ``SET LOCAL ROLE
  "app_platform"`` on enter, ``SET LOCAL ROLE "app_tenant"`` on exit. The enter
  path first touches ``session.connection()`` so the ``after_begin`` hook has
  already pinned the configured lane before the elevation lands.
* Postgres tenant-lane session whose body committed/rolled back (so the session
  is no longer in a transaction on exit) -> NO restore: issuing it would
  autobegin an idle-in-transaction connection just to set a role the next
  after_begin re-pins anyway.
* Postgres tenant-lane session whose body swallowed a DB error (transaction in
  error state) but exited cleanly -> NO restore and NO raise: a SET LOCAL ROLE
  on an aborted transaction would surface InFailedSqlTransaction out of __exit__.
* Postgres platform-lane session (``session.info`` marks ``app_platform``) ->
  elevation on enter but NO restore on exit (the session is already privileged;
  restoring ``app_tenant`` would wrongly demote it).
* On a body exception the restore is skipped (success-gated); only the flag pop
  runs. The caller's rollback ends the transaction and the next after_begin
  re-pins the tenant lane.

The unit tier uses lightweight connection/session stubs (mirroring
``tests/db/test_session_tenant_hook.py::test_no_context_clears_stale_context``)
so the role-statement contract is observable without a live Postgres backend;
the end-to-end Postgres proof lives in
``tests/connectors/runs/test_run_one_rls_postgres.py``.
"""
from __future__ import annotations

import pytest

from ums_smart_revenue.db.lane import platform_lane
from ums_smart_revenue.db.rls import APP_PLATFORM_ROLE, APP_TENANT_ROLE
from ums_smart_revenue.db.session import (
    _PLATFORM_LANE_ACTIVE_KEY,
    _SESSION_ROLE_KEY,
)


class _StubTxnStatus:
    """Mimic ``psycopg.pq.TransactionStatus`` enough for the .name compare."""

    def __init__(self, name: str) -> None:
        self.name = name


class _StubDriverInfo:
    """Expose ``transaction_status`` like psycopg's connection.info."""

    def __init__(self, status_name: str) -> None:
        self.transaction_status = _StubTxnStatus(status_name)


class _StubDriverConnection:
    """Mimic the psycopg driver_connection carrying ``info``."""

    def __init__(self, status_name: str) -> None:
        self.info = _StubDriverInfo(status_name)


class _StubDbapiConnection:
    """Mimic the SQLAlchemy connection.connection (DBAPI wrapper)."""

    def __init__(self, status_name: str) -> None:
        self.driver_connection = _StubDriverConnection(status_name)


class _StubConnection:
    """Record ``exec_driver_sql`` calls and report a fixed dialect name."""

    def __init__(self, dialect_name: str, *, txn_status: str = "INTRANS") -> None:
        self.dialect = type("Dialect", (), {"name": dialect_name})()
        self.calls: list[str] = []
        self.connection = _StubDbapiConnection(txn_status)

    def exec_driver_sql(self, sql: str, parameters=None):
        self.calls.append(sql)
        return None


class _StubSession:
    """Minimal session exposing ``info`` and ``connection()`` for the helper.

    ``in_transaction`` defaults to True (the healthy entry-transaction case);
    ``txn_status`` drives the simulated psycopg transaction-status name the
    Finding 4 guard reads (``INTRANS`` = healthy, ``INERROR`` = aborted).
    """

    def __init__(
        self,
        *,
        role: str,
        dialect_name: str,
        in_transaction: bool = True,
        txn_status: str = "INTRANS",
    ) -> None:
        self.info = {_SESSION_ROLE_KEY: role}
        self._connection = _StubConnection(dialect_name, txn_status=txn_status)
        self.connection_calls = 0
        self._in_transaction = in_transaction

    def connection(self) -> _StubConnection:
        self.connection_calls += 1
        return self._connection

    def in_transaction(self) -> bool:
        return self._in_transaction


def test_platform_lane_is_noop_off_postgres() -> None:
    """On SQLite the helper emits no role statements (no Postgres-only SQL)."""
    session = _StubSession(role=APP_TENANT_ROLE, dialect_name="sqlite")
    with platform_lane(session):
        pass
    assert session._connection.calls == []


def test_platform_lane_elevates_then_restores_tenant_lane() -> None:
    """A tenant-lane session restores when the entry txn is active + healthy."""
    session = _StubSession(role=APP_TENANT_ROLE, dialect_name="postgresql")
    with platform_lane(session):
        # Inside the block only the elevation has been issued.
        assert session._connection.calls == [
            f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"'
        ]
    assert session._connection.calls == [
        f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"',
        f'SET LOCAL ROLE "{APP_TENANT_ROLE}"',
    ]
    # The transaction must be ensured-begun before the elevation lands.
    assert session.connection_calls >= 1


def test_platform_lane_post_commit_clean_exit_issues_no_restore() -> None:
    """A body that committed (no active txn on exit) must NOT autobegin/restore.

    Finding 4(a): session.connection() would autobegin an idle-in-transaction
    connection just to set a role the next after_begin re-pins anyway. Only the
    enter-time elevation should be on the call log.
    """
    session = _StubSession(
        role=APP_TENANT_ROLE, dialect_name="postgresql", in_transaction=False
    )
    with platform_lane(session):
        pass
    assert session._connection.calls == [
        f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"'
    ]


def test_platform_lane_swallowed_error_clean_exit_skips_restore() -> None:
    """A swallowed DB error (aborted txn) clean exit must not raise/restore.

    Finding 4(b): issuing SET LOCAL ROLE on an INERROR transaction would raise
    InFailedSqlTransaction out of __exit__. The guard reads the psycopg
    transaction status name and skips the restore.
    """
    session = _StubSession(
        role=APP_TENANT_ROLE,
        dialect_name="postgresql",
        in_transaction=True,
        txn_status="INERROR",
    )
    # Body swallows its own DB error and exits cleanly -> no raise from __exit__.
    with platform_lane(session):
        pass
    assert session._connection.calls == [
        f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"'
    ]


def test_platform_lane_does_not_demote_platform_lane_session() -> None:
    """A platform-lane session elevates but is NOT restored to app_tenant."""
    session = _StubSession(role=APP_PLATFORM_ROLE, dialect_name="postgresql")
    with platform_lane(session):
        pass
    assert session._connection.calls == [
        f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"'
    ]


def test_platform_lane_skips_role_restore_on_exception() -> None:
    """On exception the role restore is skipped (the txn may be aborted).

    Issuing SET LOCAL ROLE on an aborted transaction raises "current
    transaction is aborted" and would mask the real error. The caller's
    rollback ends the transaction and the next after_begin re-pins the
    configured lane, so the restore is intentionally not attempted here
    (mirrors finance/committed_allocation.py, which restores only after a
    successful child write). The active-lane flag is still cleared.
    """
    session = _StubSession(role=APP_TENANT_ROLE, dialect_name="postgresql")
    with pytest.raises(ValueError, match="boom"):
        with platform_lane(session):
            raise ValueError("boom")
    # Only the elevation was issued; no restore statement on the failure path.
    assert session._connection.calls == [
        f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"'
    ]


def test_platform_lane_noop_off_postgres_on_exception() -> None:
    """An exception on SQLite still emits no role statements."""
    session = _StubSession(role=APP_TENANT_ROLE, dialect_name="sqlite")
    with pytest.raises(ValueError, match="boom"):
        with platform_lane(session):
            raise ValueError("boom")
    assert session._connection.calls == []


def test_platform_lane_sets_active_flag_inside_and_clears_after() -> None:
    """The active-lane flag is set inside the block and cleared on every exit.

    The after_begin hook reads this flag so a nested SAVEPOINT does not re-pin
    the tenant lane mid-block; the flag must never leak past the block (clean
    exit or exception).
    """
    session = _StubSession(role=APP_TENANT_ROLE, dialect_name="postgresql")
    assert _PLATFORM_LANE_ACTIVE_KEY not in session.info
    with platform_lane(session):
        assert session.info.get(_PLATFORM_LANE_ACTIVE_KEY) is True
    assert _PLATFORM_LANE_ACTIVE_KEY not in session.info

    with pytest.raises(ValueError, match="boom"):
        with platform_lane(session):
            assert session.info.get(_PLATFORM_LANE_ACTIVE_KEY) is True
            raise ValueError("boom")
    assert _PLATFORM_LANE_ACTIVE_KEY not in session.info
