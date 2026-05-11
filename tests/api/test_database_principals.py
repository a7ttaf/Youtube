from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.permissions import PERMISSION_DEFINITIONS
from ums_smart_revenue.auth.roles import ROLE_DEFINITIONS
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    AuditLogORM,
    PermissionORM,
    RoleORM,
    SecurityBase,
    UserORM,
    UserPermissionGrantORM,
    UserRoleAssignmentORM,
)


ACTOR_ID = UUID("00000000-0000-0000-0000-000000016001")
TARGET_ID = UUID("00000000-0000-0000-0000-000000016002")
GLOBAL_SCOPE_ID = UUID("00000000-0000-0000-0000-000000016101")


def auth_headers(user_id: UUID = ACTOR_ID, *, claimed_role: str = "assistant_analyst") -> dict[str, str]:
    return {
        "x-user-id": str(user_id),
        "x-user-email": "ignored-header@example.com",
        "x-role": claimed_role,
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'database-principals.db').as_posix()}"


def seed_security_catalog(session: Session) -> AccessScopeORM:
    scope = AccessScopeORM(id=GLOBAL_SCOPE_ID, scope_type="global", label="Global")
    session.add(scope)
    for definition in ROLE_DEFINITIONS.values():
        session.add(
            RoleORM(
                key=definition.role.value,
                label=definition.label,
                description=definition.description,
                service_only=definition.service_only,
            )
        )
    for definition in PERMISSION_DEFINITIONS.values():
        session.add(
            PermissionORM(
                key=definition.permission.value,
                label=definition.label,
                sensitive=definition.sensitive,
                audit_on_use=definition.audit_on_use,
            )
        )
    return scope


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as session:
        seed_security_catalog(session)
        session.add_all(
            [
                UserORM(id=ACTOR_ID, email="actor@example.com", display_name="Actor User"),
                UserORM(id=TARGET_ID, email="target@example.com", display_name="Target User"),
            ]
        )
        session.commit()


def add_role_assignment(database_url: str, *, user_id: UUID, role_key: str) -> None:
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            UserRoleAssignmentORM(
                id=uuid4(),
                user_id=user_id,
                role_key=role_key,
                scope_id=GLOBAL_SCOPE_ID,
                assigned_by=user_id,
                reason="Seed DB-backed principal role",
                active=True,
            )
        )
        session.commit()


def add_direct_permission(database_url: str, *, user_id: UUID, permission_key: str) -> None:
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            UserPermissionGrantORM(
                id=uuid4(),
                user_id=user_id,
                permission_key=permission_key,
                scope_id=GLOBAL_SCOPE_ID,
                granted_by=user_id,
                reason="Seed DB-backed principal permission",
                active=True,
            )
        )
        session.commit()


def test_database_principal_uses_stored_role_instead_of_claimed_header_role(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    add_role_assignment(database_url, user_id=ACTOR_ID, role_key="corporate_admin")
    client = TestClient(create_app(database_url=database_url, authz_source="database"))

    response = client.post(
        f"/users/{TARGET_ID}/roles",
        headers=auth_headers(claimed_role="assistant_analyst"),
        json={
            "role_key": "assistant_analyst",
            "scope_type": "global",
            "reason": "DB role should authorize this assignment",
        },
    )

    assert response.status_code == 201
    assert response.json()["role_key"] == "assistant_analyst"
    assert response.json()["assigned_by"] == str(ACTOR_ID)


def test_database_principal_loads_direct_permission_grants(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    add_direct_permission(database_url, user_id=ACTOR_ID, permission_key="audit.view")
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            AuditLogORM(
                id=uuid4(),
                user_id=ACTOR_ID,
                event_type="CHANNEL_UPDATED",
                entity_type="youtube_channel",
                entity_id="channel-1",
                scope_type="global",
                details={"field": "company_id"},
                sensitive=True,
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url, authz_source="database"))

    response = client.get("/audit/events?limit=10", headers=auth_headers(claimed_role="assistant_analyst"))

    assert response.status_code == 200
    assert [item["event_type"] for item in response.json()["items"]] == ["CHANNEL_UPDATED"]


def test_database_principal_rejects_disabled_user_even_with_super_owner_header(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        actor = session.get(UserORM, ACTOR_ID)
        assert actor is not None
        actor.status = "disabled"
        session.commit()
    add_role_assignment(database_url, user_id=ACTOR_ID, role_key="super_owner")
    client = TestClient(create_app(database_url=database_url, authz_source="database"))

    response = client.get("/security/roles", headers=auth_headers(claimed_role="super_owner"))

    assert response.status_code == 403
    assert response.json()["detail"] == "User is disabled"


def test_database_principal_rejects_unregistered_user(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url, authz_source="database"))

    response = client.get("/security/roles", headers=auth_headers(user_id=uuid4(), claimed_role="super_owner"))

    assert response.status_code == 403
    assert response.json()["detail"] == "User is not registered"
