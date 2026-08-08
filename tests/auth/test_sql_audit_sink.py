from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import record_audit_event
from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

USER_ID = UUID("00000000-0000-0000-0000-000000002001")
DEFAULT_TENANT_ID = UUID(UMS_TENANT_ID)
OTHER_TENANT_ID = UUID("00000000-0000-0000-0000-000000002999")


def build_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    SecurityBase.metadata.create_all(engine)
    return Session(engine)


def principal() -> UserPrincipal:
    return UserPrincipal(
        user_id=str(USER_ID),
        email="admin@example.com",
        role_assignments=[
            RoleAssignment(role=RoleKey.CORPORATE_ADMIN, scope=AccessScope.global_scope())
        ],
    )


def test_sql_audit_sink_persists_sensitive_audit_record():
    session = build_session()
    session.add(
        UserORM(id=USER_ID, email="admin@example.com", display_name="Admin", status="active")
    )
    session.commit()
    sink = SqlAlchemyAuditSink(session)

    record_audit_event(
        sink=sink,
        actor=principal(),
        event_type=AuditEventType.CHANNEL_UPDATED,
        entity_type="youtube_channel",
        entity_id="channel-tv-a",
        scope=AccessScope.company("company-tv-a"),
        reason="Correct ownership mapping",
        details={"old_primary_company_id": "company-old", "new_primary_company_id": "company-tv-a"},
    )
    session.commit()

    audit_log = session.scalars(select(AuditLogORM)).one()
    assert audit_log.user_id == USER_ID
    assert audit_log.event_type == "CHANNEL_UPDATED"
    assert audit_log.reason == "Correct ownership mapping"
    assert audit_log.is_sensitive is True
    assert audit_log.details["old_primary_company_id"] == "company-old"


def test_sql_audit_sink_tolerates_malformed_actor_id_with_details():
    session = build_session()
    sink = SqlAlchemyAuditSink(session)

    record_audit_event(
        sink=sink,
        actor=UserPrincipal(
            user_id="not-a-uuid",
            email="admin@example.com",
            role_assignments=[
                RoleAssignment(role=RoleKey.CORPORATE_ADMIN, scope=AccessScope.global_scope())
            ],
        ),
        event_type=AuditEventType.CHANNEL_UPDATED,
        entity_type="youtube_channel",
        entity_id="channel-tv-a",
        scope=AccessScope.company("company-tv-a"),
        reason="Persist audit for malformed actor",
        details={},
    )
    session.commit()

    audit_log = session.scalars(select(AuditLogORM)).one()
    assert audit_log.user_id is None
    assert audit_log.details["actor_user_id"] == "not-a-uuid"
    assert audit_log.reason == "Persist audit for malformed actor"


def test_sql_audit_sink_tolerates_unknown_valid_actor_id():
    session = build_session()
    sink = SqlAlchemyAuditSink(session)

    record_audit_event(
        sink=sink,
        actor=principal(),
        event_type=AuditEventType.CHANNEL_UPDATED,
        entity_type="youtube_channel",
        entity_id="channel-tv-a",
        scope=AccessScope.company("company-tv-a"),
        reason="Persist audit without local actor row",
        details={"old_primary_company_id": "company-old"},
    )
    session.commit()

    audit_log = session.scalars(select(AuditLogORM)).one()
    assert audit_log.user_id is None
    assert audit_log.reason == "Persist audit without local actor row"
    assert audit_log.details["actor_user_id"] == str(USER_ID)


def test_sql_audit_sink_overwrites_caller_supplied_permission_key():
    """The durable permission key is canonical, never a caller-provided value.

    ``details["permission"]`` must always be the effective permission
    record_audit_event derived; a caller (or spoofed) details entry shadowing
    it would make permission-based audit filtering lie (PR #159 review).
    """
    session = build_session()
    sink = SqlAlchemyAuditSink(session)

    record = record_audit_event(
        sink=sink,
        actor=principal(),
        event_type=AuditEventType.CHANNEL_UPDATED,
        entity_type="youtube_channel",
        entity_id="channel-tv-a",
        scope=AccessScope.company("company-tv-a"),
        reason="Spoofed permission key must not persist",
        details={"permission": "spoofed.permission"},
    )
    session.commit()

    audit_log = session.scalars(select(AuditLogORM)).one()
    assert record.permission is not None
    assert audit_log.details["permission"] == record.permission
    assert audit_log.details["permission"] != "spoofed.permission"


def test_sql_audit_sink_treats_other_tenant_actor_as_metadata():
    session = build_session()
    session.add(
        UserORM(
            id=USER_ID,
            tenant_id=OTHER_TENANT_ID,
            email="admin@example.com",
            display_name="Admin",
            status="active",
        )
    )
    session.commit()
    sink = SqlAlchemyAuditSink(session, tenant_id=DEFAULT_TENANT_ID)

    record_audit_event(
        sink=sink,
        actor=principal(),
        event_type=AuditEventType.CHANNEL_UPDATED,
        entity_type="youtube_channel",
        entity_id="channel-tv-a",
        scope=AccessScope.company("company-tv-a"),
        reason="Persist audit for cross-tenant actor",
        details={},
    )
    session.commit()

    audit_log = session.scalars(select(AuditLogORM)).one()
    assert audit_log.user_id is None
    assert audit_log.details["actor_user_id"] == str(USER_ID)
