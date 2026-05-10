from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.connectors.credentials import (
    _is_duplicate_credential_integrity_error,
    is_external_secret_ref,
)
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
    invalid_ref = "plain-google-credential"

    response = client.post(
        "/connectors/credentials",
        headers=auth_headers("connector_admin"),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "encrypted_secret_ref": invalid_ref,
            "reason": "Invalid raw secret",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Connector credentials must use an external encrypted secret reference"


def test_external_secret_ref_requires_prefix_and_locator():
    assert is_external_secret_ref("secret-manager://ums/youtube-reporting/content-owner-1")
    assert not is_external_secret_ref("")
    assert not is_external_secret_ref("secret-manager://")
    assert not is_external_secret_ref("secret-manager://   ")


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


def test_connector_credentials_reject_malformed_actor_id(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("connector_admin")
    headers["x-user-id"] = "not-a-uuid"

    response = client.post(
        "/connectors/credentials",
        headers=headers,
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "encrypted_secret_ref": "secret-manager://ums/youtube-reporting/content-owner-1",
            "reason": "Register OAuth credential reference",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "actor_user_id must be a valid UUID"


def test_connector_credentials_list_is_paginated(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("connector_admin")

    for account_id in ("content-owner-1", "content-owner-2"):
        create_response = client.post(
            "/connectors/credentials",
            headers=headers,
            json={
                "connector_key": "youtube_reporting",
                "account_id": account_id,
                "encrypted_secret_ref": f"secret-manager://ums/youtube-reporting/{account_id}",
                "reason": "Register OAuth credential reference",
            },
        )
        assert create_response.status_code == 201

    response = client.get("/connectors/credentials?limit=1&offset=0", headers=headers)
    next_response = client.get("/connectors/credentials?limit=1&offset=1", headers=headers)

    assert response.status_code == 200
    assert response.json()["items"][0]["account_id"] == "content-owner-1"
    assert response.json()["pagination"] == {"limit": 1, "offset": 0, "returned": 1, "has_more": True}
    assert next_response.status_code == 200
    assert next_response.json()["items"][0]["account_id"] == "content-owner-2"
    assert next_response.json()["pagination"] == {"limit": 1, "offset": 1, "returned": 1, "has_more": False}


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


def test_connector_credential_integrity_error_classifier_uses_duplicate_constraint_only():
    class DuplicateDiag:
        constraint_name = "uq_api_connector_credentials_connector_account"

    class DuplicateOrig(Exception):
        diag = DuplicateDiag()

    duplicate_error = IntegrityError("insert", {}, DuplicateOrig("duplicate credential"))
    foreign_key_error = IntegrityError("insert", {}, Exception("FOREIGN KEY constraint failed"))

    assert _is_duplicate_credential_integrity_error(duplicate_error)
    assert not _is_duplicate_credential_integrity_error(foreign_key_error)
