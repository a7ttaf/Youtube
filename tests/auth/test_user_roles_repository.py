from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.user_roles import (
    SqlAlchemyUserRoleAssignmentRepository,
    UserRoleAssignmentNotFoundError,
)
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    SecurityBase,
    UserORM,
    UserRoleAssignmentORM,
)
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-000000095999")
TARGET_USER_ID = UUID("00000000-0000-0000-0000-000000095001")
ACTOR_USER_ID = UUID("00000000-0000-0000-0000-000000095002")
DEFAULT_ACTOR_USER_ID = UUID("00000000-0000-0000-0000-000000095003")


def _build_db(tmp_path) -> str:
    """Create a throwaway SQLite engine database for the repository under test."""
    url = f"sqlite+pysqlite:///{(tmp_path / 'repo.db').as_posix()}"
    engine = create_engine(url)
    SecurityBase.metadata.create_all(engine)
    return url


def _tenant(tenant_id: UUID = OTHER_TENANT_ID) -> Tenant:
    """Build the ACTIVE tenant fixture the ambient tenant context carries."""
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
    """Insert one user row so actor/target foreign keys resolve."""
    session.add(
        UserORM(
            id=user_id,
            tenant_id=tenant_id,
            email=f"{user_id}@example.com",
            display_name="Tenant User",
        )
    )


def test_assign_role_uses_ambient_tenant_context(tmp_path):
    """The assignment must be written under the ambient tenant context, not
    guessed from parameters.
    """
    url = _build_db(tmp_path)
    engine = create_engine(url)
    with Session(engine) as setup:
        _seed_user(setup, TARGET_USER_ID, OTHER_TENANT_ID)
        _seed_user(setup, ACTOR_USER_ID, OTHER_TENANT_ID)
        setup.commit()

    with Session(engine) as session:
        token = TENANT_CTX.set(_tenant())
        try:
            entry = SqlAlchemyUserRoleAssignmentRepository(session).assign_role(
                user_id=str(TARGET_USER_ID),
                role_key="assistant_analyst",
                scope_type="company",
                scope_id="company-tv-a",
                assigned_by=str(ACTOR_USER_ID),
                reason="Tenant-scoped role assignment",
            )
            session.commit()
        finally:
            TENANT_CTX.reset(token)

        row = session.get(UserRoleAssignmentORM, UUID(entry.id))
        assert row is not None
        assert row.tenant_id == OTHER_TENANT_ID


def test_assign_role_adds_the_row_inside_its_savepoint(tmp_path):
    """The assignment must be pending-not-yet-flushed when BEGIN NESTED opens.

    Opening a savepoint flushes pending session state into the OUTER
    transaction, so an add left outside let a losing concurrent INSERT abort
    the whole bootstrap transaction (PendingRollbackError) instead of surfacing
    the typed conflict here (PR #210 review round 5).
    """
    url = _build_db(tmp_path)
    engine = create_engine(url)
    with Session(engine) as setup:
        _seed_user(setup, TARGET_USER_ID, OTHER_TENANT_ID)
        _seed_user(setup, ACTOR_USER_ID, OTHER_TENANT_ID)
        setup.commit()

    pending_assignments_at_savepoint_open: list[int] = []

    with Session(engine) as session:
        token = TENANT_CTX.set(_tenant())
        original_begin_nested = session.begin_nested

        def _record_pending(*args, **kwargs):
            """Count pending assignment rows at the moment a savepoint opens."""
            pending_assignments_at_savepoint_open.append(
                sum(
                    isinstance(obj, UserRoleAssignmentORM)
                    for obj in list(session.new)
                )
            )
            return original_begin_nested(*args, **kwargs)

        try:
            with patch.object(session, "begin_nested", side_effect=_record_pending):
                entry = SqlAlchemyUserRoleAssignmentRepository(session).assign_role(
                    user_id=str(TARGET_USER_ID),
                    role_key="assistant_analyst",
                    scope_type="company",
                    scope_id="company-tv-a",
                    assigned_by=str(ACTOR_USER_ID),
                    reason="Savepoint-ordering contract",
                )
            session.commit()
        finally:
            TENANT_CTX.reset(token)

    assert UUID(entry.id)
    assert pending_assignments_at_savepoint_open, "a savepoint was expected to open"
    assert max(pending_assignments_at_savepoint_open) == 0


def test_assign_role_rejects_actor_user_from_another_tenant(tmp_path):
    """An actor outside the assignment's tenant fails closed."""
    url = _build_db(tmp_path)
    engine = create_engine(url)
    with Session(engine) as setup:
        _seed_user(setup, TARGET_USER_ID, OTHER_TENANT_ID)
        _seed_user(setup, DEFAULT_ACTOR_USER_ID, UUID(int=1))
        setup.commit()

    with Session(engine) as session:
        token = TENANT_CTX.set(_tenant())
        try:
            with pytest.raises(UserRoleAssignmentNotFoundError, match="Actor user"):
                SqlAlchemyUserRoleAssignmentRepository(session).assign_role(
                    user_id=str(TARGET_USER_ID),
                    role_key="assistant_analyst",
                    scope_type="company",
                    scope_id="company-tv-a",
                    assigned_by=str(DEFAULT_ACTOR_USER_ID),
                    reason="Cross-tenant actor must fail closed",
                )
        finally:
            TENANT_CTX.reset(token)


def test_get_or_create_scope_concurrent_insert_race_is_recovered(tmp_path):
    """When a concurrent writer inserts the same scope between our SELECT and
    INSERT, _get_or_create_scope must catch the IntegrityError and return the
    existing row instead of propagating an unhandled IntegrityError (which
    would cause a 500).
    """
    url = _build_db(tmp_path)
    engine = create_engine(url)

    # A concurrent writer commits this scope before we try to insert it.
    with Session(engine) as setup:
        setup.add(
            AccessScopeORM(
                id=uuid4(),
                scope_type="company",
                scope_id="race-co",
                label="company:race-co",
            )
        )
        setup.commit()

    with Session(engine) as session:
        repo = SqlAlchemyUserRoleAssignmentRepository(session)

        original_scalars = session.scalars
        call_count = [0]

        def intercept(stmt, *args, **kwargs):
            """Feed the race window: hide pre-existing rows from early reads."""
            call_count[0] += 1
            if call_count[0] == 1:
                # Simulate the race window: our existence check ran before
                # the concurrent commit, so we see no existing scope and
                # proceed to INSERT.
                mock = MagicMock()
                mock.one_or_none.return_value = None
                return mock
            return original_scalars(stmt, *args, **kwargs)

        with patch.object(session, "scalars", side_effect=intercept):
            result = repo._get_or_create_scope(scope_type="company", scope_id="race-co")

    assert result.scope_type == "company"
    assert result.scope_id == "race-co"
