from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import ROLE_DEFINITIONS, RoleKey
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.security_models import (
    ApiConnectorCredentialORM,
    AuditLogORM,
    RoleORM,
    SecurityBase,
    UserORM,
    UserRoleAssignmentORM,
)

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


def seed_fresh_database_with_role_catalog(database_url: str) -> None:
    """Create the migrated security shape and role catalog without a user row."""
    engine = create_engine(database_url)
    try:
        OrgBase.metadata.create_all(engine)
        SecurityBase.metadata.create_all(engine)
        with Session(engine) as session:
            session.add_all(
                RoleORM(
                    key=definition.role.value,
                    label=definition.label,
                    description=definition.description,
                    service_only=definition.service_only,
                )
                for definition in ROLE_DEFINITIONS.values()
            )
            session.commit()
    finally:
        engine.dispose()


def test_audited_operator_setup_precedes_numeric_version_credential_registration(
    tmp_path,
):
    """Lock the fresh-database operator, role, then credential runbook order."""
    database_url = build_database_url(tmp_path)
    seed_fresh_database_with_role_catalog(database_url)
    client = TestClient(create_app(database_url=database_url))

    bootstrap_headers = auth_headers(
        "super_owner",
        user_id="00000000-0000-0000-0000-000000014999",
    )
    operator_response = client.post(
        "/users",
        headers=bootstrap_headers,
        json={
            "email": "local-connector-operator@example.com",
            "display_name": "Local Connector Operator",
            "reason": "Provision connector operator for local credential smoke",
        },
    )

    assert operator_response.status_code == 201
    operator = operator_response.json()
    assert operator["status"] == "active"
    assert operator["is_service_account"] is False
    assert operator["audit_event"]["event_type"] == "USER_ACCOUNT_CHANGED"

    operator_headers = auth_headers("super_owner", user_id=operator["id"])
    role_response = client.post(
        f"/users/{operator['id']}/roles",
        headers=operator_headers,
        json={
            "role_key": "connector_admin",
            "scope_type": "global",
            "reason": "Grant connector administration for local credential smoke",
        },
    )

    assert role_response.status_code == 201
    assert role_response.json()["role_key"] == "connector_admin"
    assert role_response.json()["audit_event"]["event_type"] == "USER_ROLE_CHANGED"

    numeric_version_ref = "secret-manager://projects/ums-local/secrets/ums-google-oauth/versions/17"
    credential_response = client.post(
        "/connectors/credentials",
        headers=auth_headers("connector_admin", user_id=operator["id"]),
        json={
            "connector_key": "youtube-analytics",
            "account_id": "content-owner-1",
            "encrypted_secret_ref": numeric_version_ref,
            "reason": "Register owner-approved Google credential reference for smoke",
        },
    )

    assert credential_response.status_code == 201
    assert credential_response.json()["has_secret_ref"] is True
    assert "encrypted_secret_ref" not in credential_response.json()

    engine = create_engine(database_url)
    try:
        with Session(engine) as session:
            credential = session.scalars(select(ApiConnectorCredentialORM)).one()
            role_assignment = session.scalars(select(UserRoleAssignmentORM)).one()
            audit_rows = session.scalars(select(AuditLogORM).order_by(AuditLogORM.created_at)).all()
        assert credential.encrypted_secret_ref == numeric_version_ref
        assert credential.created_by == UUID(operator["id"])
        assert credential.updated_by == UUID(operator["id"])
        assert role_assignment.role_key == "connector_admin"
        assert role_assignment.assigned_by == UUID(operator["id"])
        assert len(audit_rows) == 3
        assert [row.event_type for row in audit_rows] == [
            "USER_ACCOUNT_CHANGED",
            "USER_ROLE_CHANGED",
            "CONNECTOR_SETTINGS_CHANGED",
        ]
        assert audit_rows[0].created_at < audit_rows[1].created_at < audit_rows[2].created_at
        audit_by_event = {row.event_type: row for row in audit_rows}
        user_change_audit = audit_by_event["USER_ACCOUNT_CHANGED"]
        assert user_change_audit.user_id is None
        assert user_change_audit.details["actor_user_id"] == bootstrap_headers["x-user-id"]
        assert audit_by_event["USER_ROLE_CHANGED"].user_id == UUID(operator["id"])
        assert audit_by_event["CONNECTOR_SETTINGS_CHANGED"].user_id == UUID(operator["id"])
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
