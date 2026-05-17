"""Behaviour tests for :mod:`ums_smart_revenue.auth.platform_admin`."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.platform_admin import (
    PlatformAdminDisabledError,
    PlatformAdminNotFoundError,
    PlatformAdminPrincipal,
    PlatformAdminStatus,
    PlatformAdminValidationError,
    Principal,
    SqlAlchemyPlatformAdminLoader,
)
from ums_smart_revenue.auth.policy import (
    can_manage_tenants,
    is_platform_admin,
)
from ums_smart_revenue.db.tenant_models import PlatformAdminORM, TenantBase


# ---------------------------------------------------------------------------
# PlatformAdminPrincipal + PlatformAdminStatus
# ---------------------------------------------------------------------------


def test_status_vocabulary_matches_sql_constraint():
    assert {s.value for s in PlatformAdminStatus} == {
        "ACTIVE",
        "SUSPENDED",
        "RETIRED",
    }


def test_platform_admin_is_active_only_when_status_is_active():
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


def test_is_platform_admin_rejects_user_principal():
    user: Principal = UserPrincipal(user_id="user-1", email="u@example.com")

    assert is_platform_admin(user) is False
    assert can_manage_tenants(user) is False


def test_is_platform_admin_accepts_active_platform_admin():
    admin: Principal = PlatformAdminPrincipal(
        admin_id="00000000-0000-0000-0000-000000000010",
        email="root@platform.example",
    )

    assert is_platform_admin(admin) is True
    assert can_manage_tenants(admin) is True


def test_is_platform_admin_rejects_non_active_status():
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


def test_loader_returns_active_admin():
    engine = _build_engine()
    admin_id = uuid4()
    _seed_admin(engine, admin_id=admin_id)

    with Session(engine) as session:
        principal = SqlAlchemyPlatformAdminLoader(session).load(
            admin_id=str(admin_id)
        )

    assert isinstance(principal, PlatformAdminPrincipal)
    assert principal.admin_id == str(admin_id)
    assert principal.status == PlatformAdminStatus.ACTIVE
    assert principal.is_active is True


def test_loader_rejects_missing_admin():
    engine = _build_engine()

    with Session(engine) as session:
        with pytest.raises(PlatformAdminNotFoundError):
            SqlAlchemyPlatformAdminLoader(session).load(admin_id=str(uuid4()))


def test_loader_rejects_suspended_admin():
    engine = _build_engine()
    admin_id = uuid4()
    _seed_admin(engine, admin_id=admin_id, status=PlatformAdminStatus.SUSPENDED)

    with Session(engine) as session:
        with pytest.raises(PlatformAdminDisabledError):
            SqlAlchemyPlatformAdminLoader(session).load(admin_id=str(admin_id))


def test_loader_rejects_retired_admin():
    engine = _build_engine()
    admin_id = uuid4()
    _seed_admin(engine, admin_id=admin_id, status=PlatformAdminStatus.RETIRED)

    with Session(engine) as session:
        with pytest.raises(PlatformAdminDisabledError):
            SqlAlchemyPlatformAdminLoader(session).load(admin_id=str(admin_id))


def test_loader_rejects_invalid_uuid():
    engine = _build_engine()

    with Session(engine) as session:
        loader = SqlAlchemyPlatformAdminLoader(session)
        with pytest.raises(PlatformAdminValidationError):
            loader.load(admin_id="not-a-uuid")


def test_loader_rejects_non_string_admin_id():
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


def _build_engine():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _enable_uuid(dbapi_connection, _connection_record) -> None:
        dbapi_connection.create_function("gen_random_uuid", 0, lambda: str(uuid4()))

    TenantBase.metadata.create_all(engine)
    return engine


def _seed_admin(
    engine,
    *,
    admin_id: UUID,
    status: PlatformAdminStatus = PlatformAdminStatus.ACTIVE,
) -> None:
    now = datetime.now(timezone.utc)
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
