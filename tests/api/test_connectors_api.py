from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.security_models import ApiConnectorCredentialORM, AuditLogORM, SecurityBase, UserORM


USER_ID = UUID("00000000-0000-0000-0000-000000004001")


def auth_headers(role: str, scope_type: str = "global", scope_id: str | None = None) -> dict[str, str]:
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "connector-user@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'connectors.db').as_posix()}"


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserORM(id=USER_ID, email="connector-user@example.com", display_name="Connector User"))
        session.commit()


def test_connector_admin_can_create_credential_reference_without_exposing_secret_ref(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/connectors/credentials",
        headers=auth_headers("connector_admin"),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "encrypted_secret_ref": "secret-manager://ums/youtube-reporting/content-owner-1",
            "reason": "Register OAuth credential reference",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        credential = session.scalars(select(ApiConnectorCredentialORM)).one()
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 201
    assert response.json()["connector_key"] == "youtube_reporting"
    assert response.json()["has_secret_ref"] is True
    assert "encrypted_secret_ref" not in response.json()
    assert credential.encrypted_secret_ref == "secret-manager://ums/youtube-reporting/content-owner-1"
    assert audit_log.event_type == "CONNECTOR_SETTINGS_CHANGED"
    assert audit_log.reason == "Register OAuth credential reference"


def test_connector_credentials_reject_raw_secret_payload(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/connectors/credentials",
        headers=auth_headers("connector_admin"),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "encrypted_secret_ref": "plain-google-password",
            "reason": "Invalid raw secret",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Connector credentials must use an external encrypted secret reference"


def test_connector_credentials_reject_blank_required_strings(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/connectors/credentials",
        headers=auth_headers("connector_admin"),
        json={
            "connector_key": "   ",
            "account_id": "content-owner-1",
            "encrypted_secret_ref": "secret-manager://ums/youtube-reporting/content-owner-1",
            "reason": "Register OAuth credential reference",
        },
    )

    assert response.status_code == 422


def test_assistant_cannot_create_connector_credential(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/connectors/credentials",
        headers=auth_headers("assistant_analyst"),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "encrypted_secret_ref": "secret-manager://ums/youtube-reporting/content-owner-1",
            "reason": "Should be denied",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: connectors.manage"


def test_revenue_operations_admin_can_request_connector_job_and_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/connectors/jobs",
        headers=auth_headers("revenue_operations_admin", "connector", "youtube_reporting"),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "reason": "Manual retry after report availability delay",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 202
    assert response.json()["connector_key"] == "youtube_reporting"
    assert response.json()["execution_status"] == "recorded_not_executed"
    assert audit_log.event_type == "CONNECTOR_JOB_RUN"
    assert audit_log.scope_type == "connector"
    assert audit_log.scope_id == "youtube_reporting"
