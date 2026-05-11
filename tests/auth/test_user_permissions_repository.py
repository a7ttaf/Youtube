from __future__ import annotations

from types import TracebackType
from typing import Never
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from ums_smart_revenue.auth.user_permissions import (
    SqlAlchemyUserPermissionGrantRepository,
    UserPermissionGrantNotFoundError,
)
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    UserORM,
    UserPermissionGrantORM,
)

TARGET_ID = UUID("00000000-0000-0000-0000-000000015002")
ACTOR_ID = UUID("00000000-0000-0000-0000-000000015001")


class _ScalarResult:
    def __init__(self, value: object | None) -> None:
        self._value = value

    def one_or_none(self) -> object | None:
        return self._value


class _NestedTransaction:
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
    def __init__(
        self, *, scalars_results: list[object | None], user_exists_results: list[object]
    ) -> None:
        self._scalars_results = list(scalars_results)
        self._user_exists_results = list(user_exists_results)
        self.scope = AccessScopeORM(
            id=uuid4(),
            scope_type="company",
            scope_id="company-tv-a",
            label="company:company-tv-a",
        )

    def get(self, model: type[object], key: object) -> object | None:
        """Simulate an identity-map hit even after the row disappeared in the DB."""
        if model is UserORM:
            return UserORM(
                id=key,
                email=f"{key}@example.com",
                display_name="Cached User",
            )
        if model is AccessScopeORM and key == self.scope.id:
            return self.scope
        return None

    def scalars(self, _stmt: object) -> _ScalarResult:
        return _ScalarResult(self._scalars_results.pop(0))

    def scalar(self, _stmt: object) -> object:
        return self._user_exists_results.pop(0)

    def begin_nested(self) -> _NestedTransaction:
        return _NestedTransaction()

    def add(self, _row: object) -> None:
        pass

    def flush(self) -> Never:
        raise IntegrityError("flush", {}, Exception("FOREIGN KEY constraint failed"))


def test_grant_permission_translates_target_user_fk_race_with_db_backed_lookup():
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
