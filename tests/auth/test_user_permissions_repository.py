from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import Never
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.user_permissions import (
    SqlAlchemyUserPermissionGrantRepository,
    UserPermissionGrantNotFoundError,
)
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    SecurityBase,
    UserORM,
    UserPermissionGrantORM,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

TARGET_ID = UUID("00000000-0000-0000-0000-000000015002")
ACTOR_ID = UUID("00000000-0000-0000-0000-000000015001")
_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-000000015999")


class _ScalarResult:
    """Wrapper for a scalar result from a query, providing one_or_none() behavior."""

    def __init__(self, value: object | None) -> None:
        self._value = value

    def one_or_none(self) -> object | None:
        """Return the scalar value or None."""
        return self._value


class _NestedTransaction:
    """Context manager stub for nested transactions."""

    def __enter__(self) -> _NestedTransaction:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False


class _FkRaceSession:
    """Session stub that simulates FK races during permission grant writes."""

    def __init__(
        self, *, scalars_results: list[object | None], user_exists_results: list[object]
    ) -> None:
        self._scalars_results = list(scalars_results)
        self._user_exists_results = list(user_exists_results)
        self.scope = AccessScopeORM(
            id=uuid4(),
            tenant_id=_DEFAULT_TENANT_UUID,
            scope_type="company",
            scope_id="company-tv-a",
            label="company:company-tv-a",
        )

    def get(self, model: type[object], key: object) -> object | None:
        """Simulate an identity-map hit even after the row disappeared in the DB."""
        if model is UserORM:
            return UserORM(
                id=key,
                tenant_id=_DEFAULT_TENANT_UUID,
                email=f"{key}@example.com",
                display_name="Cached User",
            )
        if model is AccessScopeORM and key == self.scope.id:
            return self.scope
        return None

    def scalars(self, _stmt: object) -> _ScalarResult:
        """Execute statement and return the next scalar result wrapper."""
        return _ScalarResult(self._scalars_results.pop(0))

    def scalar(self, _stmt: object) -> object:
        """Execute statement and return the next scalar result directly."""
        return self._user_exists_results.pop(0)

    @staticmethod
    def begin_nested() -> _NestedTransaction:
        """Start a nested transaction context."""
        return _NestedTransaction()

    @staticmethod
    def add(_row: object) -> None:
        """Add a row to the session stub (no-op)."""
        return None

    @staticmethod
    def flush() -> Never:
        """Simulate a flush operation raising an IntegrityError for foreign key failures."""
        raise IntegrityError("flush", {}, Exception("FOREIGN KEY constraint failed"))


def _tenant(tenant_id: UUID = OTHER_TENANT_ID) -> Tenant:
    """Create a tenant model for repository tests."""
    now = datetime.now(UTC)
    return Tenant(
        id=tenant_id,
        slug="other",
        display_name="Other Tenant",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=now,
        created_at=now,
        updated_at=now,
    )


def _seed_user(session: Session, user_id: UUID, tenant_id: UUID) -> None:
    """Insert a test user into the database session for the given user and tenant IDs."""
    session.add(
        UserORM(
            id=user_id,
            tenant_id=tenant_id,
            email=f"{user_id}@example.com",
            display_name="Tenant User",
        )
    )


def test_grant_permission_uses_ambient_tenant_context(tmp_path):
    """Ensure that grant_permission uses the ambient tenant context when creating
    permission grants."""
    engine = create_engine(f"sqlite+pysqlite:///{(tmp_path / 'permissions.db').as_posix()}")
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as setup:
        _seed_user(setup, TARGET_ID, OTHER_TENANT_ID)
        _seed_user(setup, ACTOR_ID, OTHER_TENANT_ID)
        setup.commit()

    with Session(engine) as session:
        token = TENANT_CTX.set(_tenant())
        try:
            entry = SqlAlchemyUserPermissionGrantRepository(session).grant_permission(
                user_id=str(TARGET_ID),
                permission_key="analytics.view_confidence",
                scope_type="company",
                scope_id="company-tv-a",
                granted_by=str(ACTOR_ID),
                reason="Tenant-scoped permission grant",
            )
            session.commit()
        finally:
            TENANT_CTX.reset(token)

        row = session.scalars(
            select(UserPermissionGrantORM).where(UserPermissionGrantORM.id == UUID(entry.id))
        ).one()
        assert row.tenant_id == OTHER_TENANT_ID


def test_grant_permission_translates_target_user_fk_race_with_db_backed_lookup():
    """Test that grant_permission raises UserPermissionGrantNotFoundError when
    target user FK race occurs."""
    session = _FkRaceSession(
        scalars_results=[
            AccessScopeORM(
                id=uuid4(),
                scope_type="company",
                scope_id="company-tv-a",
                label="company:company-tv-a",
            ),
            None,
            None,
        ],
        user_exists_results=[None],
    )
    repository = SqlAlchemyUserPermissionGrantRepository(session)

    with pytest.raises(UserPermissionGrantNotFoundError, match="user_id not found"):
        repository.grant_permission(
            user_id=str(TARGET_ID),
            permission_key="analytics.view_confidence",
            scope_type="company",
            scope_id="company-tv-a",
            granted_by=str(ACTOR_ID),
            reason="Concurrent target deletion",
        )


def test_grant_permission_translates_actor_fk_race_with_db_backed_lookup():
    """Test that grant_permission raises UserPermissionGrantNotFoundError when
    actor FK race occurs."""
    session = _FkRaceSession(
        scalars_results=[
            AccessScopeORM(
                id=uuid4(),
                scope_type="company",
                scope_id="company-tv-a",
                label="company:company-tv-a",
            ),
            None,
            None,
        ],
        user_exists_results=[TARGET_ID, None],
    )
    repository = SqlAlchemyUserPermissionGrantRepository(session)

    with pytest.raises(UserPermissionGrantNotFoundError, match="granted_by not found"):
        repository.grant_permission(
            user_id=str(TARGET_ID),
            permission_key="analytics.view_confidence",
            scope_type="company",
            scope_id="company-tv-a",
            granted_by=str(ACTOR_ID),
            reason="Concurrent actor deletion",
        )


def test_revoke_permission_translates_actor_fk_race_with_db_backed_lookup():
    """Test that revoke_permission raises UserPermissionGrantNotFoundError when
    actor FK race occurs during revoke."""
    scope_id = uuid4()
    grant = UserPermissionGrantORM(
        id=uuid4(),
        user_id=TARGET_ID,
        permission_key="analytics.view_confidence",
        scope_id=scope_id,
        granted_by=ACTOR_ID,
        reason="Initial grant",
        active=True,
    )
    session = _FkRaceSession(scalars_results=[grant], user_exists_results=[None])
    session.scope.id = scope_id
    repository = SqlAlchemyUserPermissionGrantRepository(session)

    with pytest.raises(UserPermissionGrantNotFoundError, match="revoked_by not found"):
        repository.revoke_permission(
            user_id=str(TARGET_ID),
            grant_id=str(grant.id),
            revoked_by=str(ACTOR_ID),
            reason="Concurrent actor deletion",
        )
