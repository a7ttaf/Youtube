from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.security_models import SecurityBase, UserORM

USER_ID = UUID("00000000-0000-0000-0000-000000014001")


def auth_headers(
    role: str,
    scope_type: str = "global",
    scope_id: str | None = None,
    *,
    user_id: str | UUID = USER_ID,
) -> dict[str, str]:
    headers = {
        "x-user-id": str(user_id),
        "x-user-email": "connector-review-user@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'connector-review.db').as_posix()}"


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        OrgBase.metadata.create_all(engine)
        SecurityBase.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(
                UserORM(
                    id=USER_ID,
                    email="connector-review-user@example.com",
                    display_name="Connector Review User",
                )
            )
            session.commit()
    finally:
        engine.dispose()


def test_connector_credentials_reject_unknown_actor_id(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/connectors/credentials",
        headers=auth_headers(
            "connector_admin",
            user_id="00000000-0000-0000-0000-000000014999",
        ),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "encrypted_secret_ref": ("secret-manager://ums/youtube-reporting/content-owner-1"),
            "reason": "Register OAuth credential reference",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == ("actor_user_id does not reference an existing user")


def test_connector_scoped_admin_lists_only_their_connector_credentials(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("connector_admin")

    for connector_key in ("youtube_reporting", "adsense"):
        create_response = client.post(
            "/connectors/credentials",
            headers=headers,
            json={
                "connector_key": connector_key,
                "account_id": "content-owner-1",
                "encrypted_secret_ref": (f"secret-manager://ums/{connector_key}/content-owner-1"),
                "reason": "Register OAuth credential reference",
            },
        )
        assert create_response.status_code == 201

    response = client.get(
        "/connectors/credentials",
        headers=auth_headers(
            "connector_admin",
            "connector",
            "youtube_reporting",
        ),
    )

    assert response.status_code == 200
    assert [item["connector_key"] for item in response.json()["items"]] == ["youtube_reporting"]


def test_disabled_connector_admin_cannot_list_connector_credentials(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="disabled-connector-admin@example.com",
        role_assignments=(
            RoleAssignment(
                RoleKey.CONNECTOR_ADMIN,
                AccessScope.connector("youtube_reporting"),
            ),
        ),
        disabled=True,
    )
    client = TestClient(app)

    response = client.get("/connectors/credentials")

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: connectors.manage"
