"""Unit tests for the ``platform_lane`` single-session elevation helper.

These pin the contract the connector run path depends on:

* SQLite (and any non-Postgres dialect) -> complete no-op: no SET LOCAL ROLE
  statements are emitted, so the helper is transparent to the test tier.
* Postgres tenant-lane session (``session.info`` marks ``app_tenant``) ->
  ``SET LOCAL ROLE "app_platform"`` on enter, ``SET LOCAL ROLE "app_tenant"``
  on exit. The enter path first touches ``session.connection()`` so the
  ``after_begin`` hook has already pinned the configured lane before the
  elevation lands.
* Postgres platform-lane session (``session.info`` marks ``app_platform``) ->
  elevation on enter but NO restore on exit (the session is already privileged;
  restoring ``app_tenant`` would wrongly demote it).
* The exit restore runs even when the body raises, so a failure inside the
  block cannot strand a tenant-lane session in the elevated role.

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
from ums_smart_revenue.db.session import _SESSION_ROLE_KEY


class _StubConnection:
    """Record ``exec_driver_sql`` calls and report a fixed dialect name."""

    def __init__(self, dialect_name: str) -> None:
        self.dialect = type("Dialect", (), {"name": dialect_name})()
        self.calls: list[str] = []

    def exec_driver_sql(self, sql: str, parameters=None):
        self.calls.append(sql)
        return None


class _StubSession:
    """Minimal session exposing ``info`` and ``connection()`` for the helper."""

    def __init__(self, *, role: str, dialect_name: str) -> None:
        self.info = {_SESSION_ROLE_KEY: role}
        self._connection = _StubConnection(dialect_name)
        self.connection_calls = 0

    def connection(self) -> _StubConnection:
        self.connection_calls += 1
        return self._connection


def test_platform_lane_is_noop_off_postgres() -> None:
    """On SQLite the helper emits no role statements (no Postgres-only SQL)."""
    session = _StubSession(role=APP_TENANT_ROLE, dialect_name="sqlite")
    with platform_lane(session):
        pass
    assert session._connection.calls == []


def test_platform_lane_elevates_then_restores_tenant_lane() -> None:
    """A tenant-lane Postgres session elevates on enter and restores on exit."""
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


def test_platform_lane_does_not_demote_platform_lane_session() -> None:
    """A platform-lane session elevates but is NOT restored to app_tenant."""
    session = _StubSession(role=APP_PLATFORM_ROLE, dialect_name="postgresql")
    with platform_lane(session):
        pass
    assert session._connection.calls == [
        f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"'
    ]


def test_platform_lane_restores_tenant_role_on_exception() -> None:
    """An exception inside the block still restores the tenant lane on exit."""
    session = _StubSession(role=APP_TENANT_ROLE, dialect_name="postgresql")
    with pytest.raises(ValueError, match="boom"):
        with platform_lane(session):
            raise ValueError("boom")
    assert session._connection.calls == [
        f'SET LOCAL ROLE "{APP_PLATFORM_ROLE}"',
        f'SET LOCAL ROLE "{APP_TENANT_ROLE}"',
    ]


def test_platform_lane_noop_off_postgres_on_exception() -> None:
    """An exception on SQLite still emits no role statements."""
    session = _StubSession(role=APP_TENANT_ROLE, dialect_name="sqlite")
    with pytest.raises(ValueError, match="boom"):
        with platform_lane(session):
            raise ValueError("boom")
    assert session._connection.calls == []
