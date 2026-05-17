"""Tenant-bound user account repository behaviour tests."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.users import (
    SqlAlchemyUserAccountRepository,
    UserAccountNotFoundError,
)
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    PermissionORM,
    RoleORM,
    SecurityBase,
    UserORM,
    UserPermissionGrantORM,
    UserRoleAssignmentORM,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

DEFAULT_TENANT_ID = UUID(UMS_TENANT_ID)
SECOND_TENANT_ID = UUID("00000000-0000-0000-0000-000000000023")
DEFAULT_USER_ID = UUID("00000000-0000-0000-0000-000000023001")
SECOND_USER_ID = UUID("00000000-0000-0000-0000-000000023002")
DEFAULT_SCOPE_ID = UUID("00000000-0000-0000-0000-000000023101")
SECOND_SCOPE_ID = UUID("00000000-0000-0000-0000-000000023102")
SECOND_PERMISSION_SCOPE_ID = UUID("00000000-0000-0000-0000-000000023103")
CREATED_AT = datetime(2026, 5, 17, 21, 0, tzinfo=UTC)


def build_session() -> Session:
    """Create an isolated in-memory auth schema for tenant-scope tests."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    SecurityBase.metadata.create_all(engine)
    return Session(engine)


def seed_users(session: Session) -> None:
    """Insert users for two tenants into one repository test database."""
    session.add_all(
        [
            UserORM(
                id=DEFAULT_USER_ID,
                tenant_id=DEFAULT_TENANT_ID,
                email="shared@example.com",
                display_name="Default User",
            ),
            UserORM(
                id=SECOND_USER_ID,
                tenant_id=SECOND_TENANT_ID,
                email="second@example.com",
                display_name="Second User",
            ),
        ]
    )
    session.commit()


def test_user_repository_persists_current_tenant_on_create() -> None:
    """Verify ambient tenant context scopes account creation."""
    session = build_session()
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        created = SqlAlchemyUserAccountRepository(session).create_user(
            email="created@example.com",
            display_name="Created User",
            is_service_account=False,
        )
    finally:
        TENANT_CTX.reset(token)
    session.commit()

    row = session.scalars(select(UserORM).where(UserORM.id == UUID(created.id))).one()
    assert row.tenant_id == SECOND_TENANT_ID


def test_user_repository_filters_list_users_by_current_tenant() -> None:
    """Verify tenant-scoped list queries do not leak other tenants."""
    session = build_session()
    seed_users(session)
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        items, has_more, next_cursor = SqlAlchemyUserAccountRepository(
            session
        ).list_users(limit=10)
    finally:
        TENANT_CTX.reset(token)

    assert [item.id for item in items] == [str(SECOND_USER_ID)]
    assert has_more is False
    assert next_cursor is None


def test_user_repository_email_conflict_is_tenant_scoped() -> None:
    """Verify an email used by another tenant does not block creation."""
    session = build_session()
    seed_users(session)
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        created = SqlAlchemyUserAccountRepository(session).create_user(
            email="shared@example.com",
            display_name="Second Shared User",
            is_service_account=False,
        )
    finally:
        TENANT_CTX.reset(token)
    session.commit()

    row = session.scalars(select(UserORM).where(UserORM.id == UUID(created.id))).one()
    assert row.tenant_id == SECOND_TENANT_ID
    assert row.email == "shared@example.com"


def test_user_repository_explicit_tenant_overrides_request_context() -> None:
    """Verify constructor tenant ids win over ambient request context."""
    session = build_session()
    seed_users(session)
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        account = SqlAlchemyUserAccountRepository(
            session, tenant_id=DEFAULT_TENANT_ID
        ).get_user(user_id=str(DEFAULT_USER_ID))
    finally:
        TENANT_CTX.reset(token)

    assert account.id == str(DEFAULT_USER_ID)


def test_user_repository_rejects_cross_tenant_user_lookup() -> None:
    """Verify get_user cannot return a row from another tenant."""
    session = build_session()
    seed_users(session)
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        with pytest.raises(UserAccountNotFoundError, match="User not found"):
            SqlAlchemyUserAccountRepository(session).get_user(
                user_id=str(DEFAULT_USER_ID)
            )
    finally:
        TENANT_CTX.reset(token)


def test_user_repository_access_profile_filters_by_current_tenant() -> None:
    """Verify access-profile joins stay inside the active tenant boundary."""
    session = build_session()
    seed_users(session)
    _seed_access_profile_rows(session)
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        profile = SqlAlchemyUserAccountRepository(session).get_access_profile(
            user_id=str(SECOND_USER_ID)
        )
    finally:
        TENANT_CTX.reset(token)

    assert profile.user.id == str(SECOND_USER_ID)
    assert [assignment.scope_id for assignment in profile.role_assignments] == [
        "second-company"
    ]
    assert [grant.scope_id for grant in profile.direct_permissions] == [
        "second-channel"
    ]


def _seed_access_profile_rows(session: Session) -> None:
    """Insert role, permission, and scope rows for two tenant boundaries."""
    session.add_all(
        [
            RoleORM(
                key="company_manager",
                label="Company Manager",
                description="Manage company access",
                service_only=False,
            ),
            PermissionORM(
                key="analytics.view_confidence",
                label="View confidence",
                sensitive=False,
                audit_on_use=False,
            ),
            AccessScopeORM(
                id=DEFAULT_SCOPE_ID,
                tenant_id=DEFAULT_TENANT_ID,
                scope_type="company",
                scope_id="default-company",
                label="company:default-company",
            ),
            AccessScopeORM(
                id=SECOND_SCOPE_ID,
                tenant_id=SECOND_TENANT_ID,
                scope_type="company",
                scope_id="second-company",
                label="company:second-company",
            ),
            AccessScopeORM(
                id=SECOND_PERMISSION_SCOPE_ID,
                tenant_id=SECOND_TENANT_ID,
                scope_type="channel",
                scope_id="second-channel",
                label="channel:second-channel",
            ),
            UserRoleAssignmentORM(
                id=uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                user_id=DEFAULT_USER_ID,
                role_key="company_manager",
                scope_id=DEFAULT_SCOPE_ID,
                assigned_by=DEFAULT_USER_ID,
                reason="Default tenant role",
                active=True,
            ),
            UserRoleAssignmentORM(
                id=uuid4(),
                tenant_id=SECOND_TENANT_ID,
                user_id=SECOND_USER_ID,
                role_key="company_manager",
                scope_id=SECOND_SCOPE_ID,
                assigned_by=SECOND_USER_ID,
                reason="Second tenant role",
                active=True,
            ),
            UserPermissionGrantORM(
                id=uuid4(),
                tenant_id=SECOND_TENANT_ID,
                user_id=SECOND_USER_ID,
                permission_key="analytics.view_confidence",
                scope_id=SECOND_PERMISSION_SCOPE_ID,
                granted_by=SECOND_USER_ID,
                reason="Second tenant permission",
                active=True,
            ),
        ]
    )
    session.commit()


def _tenant(tenant_id: UUID, *, slug: str) -> Tenant:
    """Build an active tenant context object for request-scope assertions."""
    return Tenant(
        id=tenant_id,
        slug=slug,
        display_name=f"{slug.title()} Tenant",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=CREATED_AT,
        created_at=CREATED_AT,
        updated_at=CREATED_AT,
    )
