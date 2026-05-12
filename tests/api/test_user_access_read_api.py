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
EARLY_SORT_ID = UUID("00000000-0000-0000-0000-000000018004")
MISSING_ID = UUID("00000000-0000-0000-0000-000000018099")
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


def insert_user(
    database_url: str,
    *,
    user_id: UUID,
    email: str,
    display_name: str = "Inserted User",
) -> None:
    """Insert an account between paginated API requests."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            UserORM(
                id=user_id,
                email=email,
                display_name=display_name,
            )
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
        "next_cursor": {"email": "disabled@example.com", "id": str(OTHER_ID)},
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


def test_user_account_list_returns_empty_page_for_large_offset(tmp_path):
    """Large offsets return an empty bounded page instead of an error."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/users",
        headers=auth_headers("corporate_admin"),
        params={"limit": 10, "offset": 100},
    )

    assert response.status_code == 200
    assert response.json() == {
        "items": [],
        "pagination": {
            "limit": 10,
            "offset": 100,
            "returned": 0,
            "has_more": False,
            "next_cursor": None,
        },
    }


def test_user_account_list_cursor_is_stable_when_new_users_arrive(tmp_path):
    """Cursor pagination continues after the prior row when earlier users arrive."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    first_page = client.get(
        "/users",
        headers=auth_headers("corporate_admin"),
        params={"limit": 1},
    )
    assert first_page.status_code == 200
    next_cursor = first_page.json()["pagination"]["next_cursor"]

    insert_user(
        database_url,
        user_id=EARLY_SORT_ID,
        email="aaa-inserted@example.com",
        display_name="Inserted Before Cursor",
    )
    second_page = client.get(
        "/users",
        headers=auth_headers("corporate_admin"),
        params={
            "limit": 1,
            "cursor_email": next_cursor["email"],
            "cursor_id": next_cursor["id"],
        },
    )

    assert second_page.status_code == 200
    assert [item["email"] for item in first_page.json()["items"]] == [
        "admin@example.com"
    ]
    assert [item["email"] for item in second_page.json()["items"]] == [
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


def test_corporate_admin_gets_404_for_missing_access_profile(tmp_path):
    """Authorized access-profile reads report missing users as not found."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        f"/users/{MISSING_ID}/access",
        headers=auth_headers("corporate_admin"),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "User not found"


def test_corporate_admin_gets_422_for_invalid_access_profile_uuid(tmp_path):
    """Authorized access-profile reads validate malformed target UUIDs."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/users/not-a-uuid/access",
        headers=auth_headers("corporate_admin"),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "user_id must be a valid UUID"


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
