"""Behaviour tests for :mod:`ums_smart_revenue.auth.platform_admin`."""

from contextlib import nullcontext
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.platform_admin import (
    PLATFORM_ADMIN_QUERY_TIMEOUT_MS,
    PlatformAdminDisabledError,
    PlatformAdminNotFoundError,
    PlatformAdminPrincipal,
    PlatformAdminStatus,
    PlatformAdminTransactionError,
    PlatformAdminValidationError,
    Principal,
    SqlAlchemyPlatformAdminLoader,
)
from ums_smart_revenue.auth.policy import (
    can_manage_tenants,
    is_platform_admin,
)
from ums_smart_revenue.db.tenant_models import PlatformAdminORM, TenantBase


class RecordingConnection:
    """Connection double that records transaction-local SQL commands."""

    def __init__(self) -> None:
        """Initialize the recorded command list."""
        self.executed_sql: list[str] = []

    def exec_driver_sql(self, sql: str) -> None:
        """Record raw SQL executed against the connection."""
        self.executed_sql.append(sql)


class FailingPlatformAdminSession:
    """Session double that raises during platform-admin row lookup."""

    def __init__(self, *, dialect_name: str = "sqlite") -> None:
        """Initialize counters used to verify read-hardening behavior."""
        self.connection_count = 0
        self.dialect_name = dialect_name
        self.get_count = 0
        self.recording_connection = RecordingConnection()

    @staticmethod
    def in_transaction() -> bool:
        """Report no active transaction so the loader prepares isolation."""
        return False

    def get_bind(self) -> SimpleNamespace:
        """Return a minimal bind with the dialect name used by the loader."""
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect_name))

    @staticmethod
    def begin() -> nullcontext:
        """Return a no-op context manager for the loader transaction scope."""
        return nullcontext()

    def connection(self, *, execution_options: dict[str, str]) -> RecordingConnection:
        """Accept read isolation options before the failing lookup."""
        assert execution_options == {"isolation_level": "SERIALIZABLE"}
        self.connection_count += 1
        return self.recording_connection

    def get(self, *_args: object, **_kwargs: object) -> None:
        """Raise after isolation setup so tests can observe the read contract."""
        self.get_count += 1
        raise RuntimeError("database unavailable")


# ---------------------------------------------------------------------------
# PlatformAdminPrincipal + PlatformAdminStatus
# ---------------------------------------------------------------------------


def test_status_vocabulary_matches_sql_constraint() -> None:
    """Platform-admin statuses stay aligned with the migration CHECK."""
    assert {s.value for s in PlatformAdminStatus} == {
        "ACTIVE",
        "SUSPENDED",
        "RETIRED",
    }


def test_platform_admin_is_active_only_when_status_is_active() -> None:
    """Only ACTIVE platform admins are usable by platform policy checks."""
    active = PlatformAdminPrincipal(
        admin_id="00000000-0000-0000-0000-000000000010",
        email="root@platform.example",
    )
    suspended = PlatformAdminPrincipal(
        admin_id=active.admin_id,
        email=active.email,
        status=PlatformAdminStatus.SUSPENDED,
    )

    assert active.is_active is True
    assert suspended.is_active is False


# ---------------------------------------------------------------------------
# Policy helpers
# ---------------------------------------------------------------------------


def test_is_platform_admin_rejects_user_principal() -> None:
    """Tenant user principals never satisfy platform-admin predicates."""
    user: Principal = UserPrincipal(user_id="user-1", email="u@example.com")

    assert is_platform_admin(user) is False
    assert can_manage_tenants(user) is False


def test_is_platform_admin_accepts_active_platform_admin() -> None:
    """Active platform admins may manage tenants."""
    admin: Principal = PlatformAdminPrincipal(
        admin_id="00000000-0000-0000-0000-000000000010",
        email="root@platform.example",
    )

    assert is_platform_admin(admin) is True
    assert can_manage_tenants(admin) is True


def test_is_platform_admin_rejects_non_active_status() -> None:
    """Suspended and retired platform admins fail tenant-management checks."""
    for status in (PlatformAdminStatus.SUSPENDED, PlatformAdminStatus.RETIRED):
        admin: Principal = PlatformAdminPrincipal(
            admin_id="00000000-0000-0000-0000-000000000010",
            email="root@platform.example",
            status=status,
        )

        assert is_platform_admin(admin) is False
        assert can_manage_tenants(admin) is False


# ---------------------------------------------------------------------------
# SqlAlchemyPlatformAdminLoader
# ---------------------------------------------------------------------------


def test_loader_returns_active_admin() -> None:
    """The SQL loader returns an immutable principal for an ACTIVE row."""
    engine = _build_engine()
    admin_id = uuid4()
    _seed_admin(engine, admin_id=admin_id)

    with Session(engine) as session:
        principal = SqlAlchemyPlatformAdminLoader(session).load(admin_id=str(admin_id))

    assert isinstance(principal, PlatformAdminPrincipal)
    assert principal.admin_id == str(admin_id)
    assert principal.status == PlatformAdminStatus.ACTIVE
    assert principal.is_active is True


def test_loader_uses_serializable_postgres_reads() -> None:
    """Postgres platform-admin reads request serializable isolation and timeout."""
    session = FailingPlatformAdminSession(dialect_name="postgresql")

    with pytest.raises(RuntimeError):
        SqlAlchemyPlatformAdminLoader(session).load(admin_id="00000000-0000-0000-0000-000000000010")

    assert PLATFORM_ADMIN_QUERY_TIMEOUT_MS == 5_000
    assert session.connection_count == 1
    assert session.recording_connection.executed_sql == [
        f"SET LOCAL statement_timeout = {PLATFORM_ADMIN_QUERY_TIMEOUT_MS}"
    ]
    assert session.get_count == 1


def test_loader_rejects_active_transaction_to_avoid_race_reads() -> None:
    """Caller-owned transactions are rejected to avoid mixed-isolation reads."""
    engine = _build_engine()

    with (
        Session(engine) as session,
        session.begin(),
        pytest.raises(PlatformAdminTransactionError),
    ):
        SqlAlchemyPlatformAdminLoader(session).load(admin_id=str(uuid4()))


def test_loader_rejects_missing_admin() -> None:
    """Unknown platform-admin ids fail closed."""
    engine = _build_engine()

    with Session(engine) as session, pytest.raises(PlatformAdminNotFoundError):
        SqlAlchemyPlatformAdminLoader(session).load(admin_id=str(uuid4()))


def test_loader_rejects_suspended_admin() -> None:
    """Suspended platform admins cannot authenticate."""
    engine = _build_engine()
    admin_id = uuid4()
    _seed_admin(engine, admin_id=admin_id, status=PlatformAdminStatus.SUSPENDED)

    with Session(engine) as session, pytest.raises(PlatformAdminDisabledError):
        SqlAlchemyPlatformAdminLoader(session).load(admin_id=str(admin_id))


def test_loader_rejects_retired_admin() -> None:
    """Retired platform admins cannot authenticate."""
    engine = _build_engine()
    admin_id = uuid4()
    _seed_admin(engine, admin_id=admin_id, status=PlatformAdminStatus.RETIRED)

    with Session(engine) as session, pytest.raises(PlatformAdminDisabledError):
        SqlAlchemyPlatformAdminLoader(session).load(admin_id=str(admin_id))


def test_loader_rejects_invalid_uuid() -> None:
    """Malformed admin ids are rejected before database access."""
    engine = _build_engine()

    with Session(engine) as session:
        loader = SqlAlchemyPlatformAdminLoader(session)
        with pytest.raises(PlatformAdminValidationError):
            loader.load(admin_id="not-a-uuid")


def test_loader_rejects_non_string_admin_id() -> None:
    """Non-string admin ids are rejected as untrusted principal input."""
    engine = _build_engine()

    with Session(engine) as session:
        loader = SqlAlchemyPlatformAdminLoader(session)
        with pytest.raises(PlatformAdminValidationError):
            loader.load(admin_id=123)  # type: ignore[arg-type]


# Note: the loader also defends against a row with an unrecognised status
# value via PlatformAdminDataValidationError, but the case is unreachable
# in practice because the CHECK constraint `ck_platform_admins_status`
# (asserted in tests/db/test_tenants_migration.py) rejects unknown values
# at the database layer. Adding a test for that defence would require
# disabling the CHECK first, which is not supported by SQLite's stdlib
# driver. The defence stays in code as a belt-and-braces guard against
# future schema relaxations.


# ---------------------------------------------------------------------------
# Test plumbing
# ---------------------------------------------------------------------------


def _build_engine() -> Engine:
    """Create an in-memory tenant schema for platform-admin loader tests."""
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_uuid(dbapi_connection, _connection_record) -> None:
        """Install a SQLite replacement for the Postgres UUID default."""
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: str(uuid4()))

    TenantBase.metadata.create_all(engine)
    return engine


def _seed_admin(
    engine: Engine,
    *,
    admin_id: UUID,
    status: PlatformAdminStatus = PlatformAdminStatus.ACTIVE,
) -> None:
    """Insert a platform-admin row for SQL loader scenarios."""
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add(
            PlatformAdminORM(
                id=admin_id,
                email=f"admin-{admin_id}@platform.example",
                display_name="Test Admin",
                status=status.value,
                created_at=now,
                updated_at=now,
            )
        )
