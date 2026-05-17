"""Tenant-bound audit repository and sink behaviour tests."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_log import (
    MAX_AUDIT_LOG_PAGE_SIZE,
    AuditLogValidationError,
    SqlAlchemyAuditLogRepository,
)
from ums_smart_revenue.auth.audit_service import record_audit_event
from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

DEFAULT_TENANT_ID = UUID(UMS_TENANT_ID)
SECOND_TENANT_ID = UUID("00000000-0000-0000-0000-000000000022")
DEFAULT_USER_ID = UUID("00000000-0000-0000-0000-000000022001")
SECOND_USER_ID = UUID("00000000-0000-0000-0000-000000022002")
CREATED_AT = datetime(2026, 5, 17, 20, 0, tzinfo=UTC)


def build_session() -> Session:
    """Create an isolated in-memory auth schema for tenant-scope tests."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    SecurityBase.metadata.create_all(engine)
    return Session(engine)


def seed_audit_rows(session: Session) -> None:
    """Insert audit rows for two tenant ids into the same test database."""
    session.add_all(
        [
            AuditLogORM(
                id=uuid4(),
                tenant_id=DEFAULT_TENANT_ID,
                user_id=None,
                event_type="DEFAULT_TENANT_EVENT",
                entity_type="audit_test",
                entity_id="default",
                scope_type="global",
                scope_id=None,
                reason=None,
                details={},
                sensitive=False,
                created_at=CREATED_AT,
            ),
            AuditLogORM(
                id=uuid4(),
                tenant_id=SECOND_TENANT_ID,
                user_id=None,
                event_type="SECOND_TENANT_EVENT",
                entity_type="audit_test",
                entity_id="second",
                scope_type="global",
                scope_id=None,
                reason=None,
                details={},
                sensitive=False,
                created_at=CREATED_AT,
            ),
        ]
    )
    session.commit()


def test_audit_repository_filters_to_explicit_tenant_id() -> None:
    """Verify explicit tenant injection isolates audit log reads."""
    session = build_session()
    seed_audit_rows(session)

    page = SqlAlchemyAuditLogRepository(
        session, tenant_id=SECOND_TENANT_ID
    ).list_events(limit=10)

    assert [item.event_type for item in page.items] == ["SECOND_TENANT_EVENT"]


def test_audit_repository_uses_default_tenant_without_context() -> None:
    """Verify bootstrap callers fall back to the default tenant id."""
    session = build_session()
    seed_audit_rows(session)

    page = SqlAlchemyAuditLogRepository(session).list_events(limit=10)

    assert [item.event_type for item in page.items] == ["DEFAULT_TENANT_EVENT"]


def test_audit_repository_returns_empty_page_for_empty_tenant() -> None:
    """Verify tenant filters return an empty page instead of leaking rows."""
    session = build_session()
    seed_audit_rows(session)

    page = SqlAlchemyAuditLogRepository(session, tenant_id=uuid4()).list_events(
        limit=10
    )

    assert page.items == []
    assert page.has_more is False
    assert page.next_cursor is None


def test_audit_repository_explicit_tenant_overrides_request_context() -> None:
    """Verify constructor tenant ids win over any ambient request context."""
    session = build_session()
    seed_audit_rows(session)
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        page = SqlAlchemyAuditLogRepository(
            session, tenant_id=DEFAULT_TENANT_ID
        ).list_events(limit=10)
    finally:
        TENANT_CTX.reset(token)

    assert [item.event_type for item in page.items] == ["DEFAULT_TENANT_EVENT"]


def test_audit_repository_uses_request_tenant_context_by_default() -> None:
    """Verify ambient tenant context scopes reads before API wiring lands."""
    session = build_session()
    seed_audit_rows(session)
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        page = SqlAlchemyAuditLogRepository(session).list_events(limit=10)
    finally:
        TENANT_CTX.reset(token)

    assert [item.event_type for item in page.items] == ["SECOND_TENANT_EVENT"]


def test_audit_repository_rejects_invalid_tenant_id() -> None:
    """Verify malformed constructor tenant ids fail closed."""
    session = build_session()

    with pytest.raises(AuditLogValidationError, match="tenant_id must be a valid UUID"):
        SqlAlchemyAuditLogRepository(session, tenant_id="not-a-uuid")


def test_audit_repository_rejects_invalid_cursor_uuid() -> None:
    """Verify cursor UUID parsing fails before running the query."""
    session = build_session()
    seed_audit_rows(session)

    with pytest.raises(AuditLogValidationError, match="cursor_id must be a valid UUID"):
        SqlAlchemyAuditLogRepository(session).list_events(
            cursor_created_at=CREATED_AT,
            cursor_id="not-a-uuid",
        )


@pytest.mark.parametrize("limit", [1, MAX_AUDIT_LOG_PAGE_SIZE])
def test_audit_repository_accepts_pagination_boundaries(limit: int) -> None:
    """Verify documented audit page-size boundaries remain accepted."""
    session = build_session()
    seed_audit_rows(session)

    page = SqlAlchemyAuditLogRepository(session).list_events(limit=limit)

    assert page.limit == limit


def test_audit_repository_rejects_page_size_above_boundary() -> None:
    """Verify audit page-size validation rejects the first invalid value."""
    session = build_session()

    with pytest.raises(AuditLogValidationError, match="limit must be between"):
        SqlAlchemyAuditLogRepository(session).list_events(
            limit=MAX_AUDIT_LOG_PAGE_SIZE + 1
        )


def test_sql_audit_sink_uses_request_tenant_context_by_default() -> None:
    """Verify ambient tenant context scopes SQL audit writes."""
    session = build_session()
    session.add(
        UserORM(
            id=SECOND_USER_ID,
            tenant_id=SECOND_TENANT_ID,
            email="tenant-two@example.com",
            display_name="Tenant Two User",
            status="active",
        )
    )
    session.commit()
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        record_audit_event(
            sink=SqlAlchemyAuditSink(session),
            actor=_principal(SECOND_USER_ID),
            event_type=AuditEventType.CHANNEL_UPDATED,
            entity_type="youtube_channel",
            entity_id="channel-tenant-two",
            scope=AccessScope.company("company-tenant-two"),
            reason="Tenant-scoped audit write",
            details={},
        )
    finally:
        TENANT_CTX.reset(token)
    session.commit()

    audit_log = session.scalars(select(AuditLogORM)).one()
    assert audit_log.tenant_id == SECOND_TENANT_ID
    assert audit_log.user_id == SECOND_USER_ID


def test_sql_audit_sink_explicit_tenant_overrides_request_context() -> None:
    """Verify explicit tenant injection also isolates audit writes."""
    session = build_session()
    session.add(
        UserORM(
            id=DEFAULT_USER_ID,
            tenant_id=DEFAULT_TENANT_ID,
            email="default@example.com",
            display_name="Default Tenant User",
            status="active",
        )
    )
    session.commit()
    token = TENANT_CTX.set(_tenant(SECOND_TENANT_ID, slug="second"))
    try:
        record_audit_event(
            sink=SqlAlchemyAuditSink(session, tenant_id=DEFAULT_TENANT_ID),
            actor=_principal(DEFAULT_USER_ID),
            event_type=AuditEventType.CHANNEL_UPDATED,
            entity_type="youtube_channel",
            entity_id="channel-default",
            scope=AccessScope.company("company-default"),
            reason="Explicit audit write",
            details={},
        )
    finally:
        TENANT_CTX.reset(token)
    session.commit()

    audit_log = session.scalars(select(AuditLogORM)).one()
    assert audit_log.tenant_id == DEFAULT_TENANT_ID
    assert audit_log.user_id == DEFAULT_USER_ID


def _principal(user_id: UUID) -> UserPrincipal:
    """Build a principal with enough authority to create audit records."""
    return UserPrincipal(
        user_id=str(user_id),
        email="admin@example.com",
        role_assignments=[
            RoleAssignment(
                role=RoleKey.CORPORATE_ADMIN,
                scope=AccessScope.global_scope(),
            )
        ],
    )


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
