from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.permissions import PERMISSION_DEFINITIONS
from ums_smart_revenue.auth.roles import ROLE_DEFINITIONS
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    PermissionORM,
    RoleORM,
    SecurityBase,
    UserORM,
    UserPermissionGrantORM,
    UserRoleAssignmentORM,
)

ADMIN_ID = UUID("00000000-0000-0000-0000-000000018001")
TARGET_ID = UUID("00000000-0000-0000-0000-000000018002")
OTHER_ID = UUID("00000000-0000-0000-0000-000000018003")
COMPANY_SCOPE_ID = UUID("00000000-0000-0000-0000-000000018101")
CHANNEL_SCOPE_ID = UUID("00000000-0000-0000-0000-000000018102")
COMPANY_ID = "company-tv-a"
CHANNEL_ID = "channel-tv-a"


def auth_headers(role: str, user_id: UUID = ADMIN_ID) -> dict[str, str]:
    return {
        "x-user-id": str(user_id),
        "x-user-email": f"{role}@example.com",
        "x-role": role,
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'user-access-read.db').as_posix()}"


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                UserORM(
                    id=ADMIN_ID,
                    email="admin@example.com",
                    display_name="Admin User",
                ),
                UserORM(
                    id=TARGET_ID,
                    email="target@example.com",
                    display_name="Target User",
                ),
                UserORM(
                    id=OTHER_ID,
                    email="disabled@example.com",
                    display_name="Disabled User",
                    status="disabled",
                ),
            ]
        )
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
        session.add_all(
            [
                AccessScopeORM(
                    id=COMPANY_SCOPE_ID,
                    scope_type="company",
                    scope_id=COMPANY_ID,
                    label="company:company-tv-a",
                ),
                AccessScopeORM(
                    id=CHANNEL_SCOPE_ID,
                    scope_type="channel",
                    scope_id=CHANNEL_ID,
                    label="channel:channel-tv-a",
                ),
            ]
        )
        revoked_at = datetime(2026, 5, 1, tzinfo=UTC)
        session.add_all(
            [
                UserRoleAssignmentORM(
                    id=uuid4(),
                    user_id=TARGET_ID,
                    role_key="company_manager",
                    scope_id=COMPANY_SCOPE_ID,
                    assigned_by=ADMIN_ID,
                    reason="Manage TV company",
                    active=True,
                ),
                UserRoleAssignmentORM(
                    id=uuid4(),
                    user_id=TARGET_ID,
                    role_key="assistant_analyst",
                    scope_id=COMPANY_SCOPE_ID,
                    assigned_by=ADMIN_ID,
                    revoked_by=ADMIN_ID,
                    revoked_at=revoked_at,
                    reason="Historical assignment",
                    active=False,
                ),
                UserPermissionGrantORM(
                    id=uuid4(),
                    user_id=TARGET_ID,
                    permission_key="analytics.view_confidence",
                    scope_id=CHANNEL_SCOPE_ID,
                    granted_by=ADMIN_ID,
                    reason="Temporary QA visibility",
                    active=True,
                ),
                UserPermissionGrantORM(
                    id=uuid4(),
                    user_id=TARGET_ID,
                    permission_key="finance.view_revenue",
                    scope_id=COMPANY_SCOPE_ID,
                    granted_by=ADMIN_ID,
                    revoked_by=ADMIN_ID,
                    revoked_at=revoked_at,
                    reason="Historical finance visibility",
                    revoke_reason="No longer needed",
                    active=False,
                ),
            ]
        )
        session.commit()


def test_corporate_admin_lists_user_accounts_with_pagination(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/users",
        headers=auth_headers("corporate_admin"),
        params={"limit": 2, "offset": 0},
    )

    assert response.status_code == 200
    body = response.json()
    assert [item["email"] for item in body["items"]] == [
        "admin@example.com",
        "disabled@example.com",
    ]
    assert body["pagination"] == {
        "limit": 2,
        "offset": 0,
        "returned": 2,
        "has_more": True,
    }


def test_user_account_list_can_filter_by_status(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/users",
        headers=auth_headers("corporate_admin"),
        params={"status": "disabled"},
    )

    assert response.status_code == 200
    assert [item["email"] for item in response.json()["items"]] == [
        "disabled@example.com"
    ]


def test_assistant_cannot_list_user_accounts(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get("/users", headers=auth_headers("assistant_analyst"))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: users.manage"


def test_corporate_admin_reads_active_user_access_profile(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        f"/users/{TARGET_ID}/access",
        headers=auth_headers("corporate_admin"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["user"]["id"] == str(TARGET_ID)
    assert body["user"]["email"] == "target@example.com"
    assert len(body["role_assignments"]) == 1
    role_assignment = body["role_assignments"][0]
    assert role_assignment["id"]
    assert role_assignment["role_key"] == "company_manager"
    assert role_assignment["scope_type"] == "company"
    assert role_assignment["scope_id"] == COMPANY_ID
    assert role_assignment["active"] is True
    assert len(body["direct_permissions"]) == 1
    direct_permission = body["direct_permissions"][0]
    assert direct_permission["id"]
    assert direct_permission["permission_key"] == "analytics.view_confidence"
    assert direct_permission["scope_type"] == "channel"
    assert direct_permission["scope_id"] == CHANNEL_ID
    assert direct_permission["active"] is True


def test_assistant_cannot_probe_user_access_profiles(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/users/not-a-uuid/access",
        headers=auth_headers("assistant_analyst"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: users.manage"
