"""Integration tests for connector credential and test-connection API endpoints."""
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.connectors.credentials import (
    _is_duplicate_credential_integrity_error,
    is_external_secret_ref,
)
from ums_smart_revenue.connectors.google.errors import (
    CredentialNotFoundError,
    InactiveCredentialError,
    OAuthRefreshError,
    SecretFetchError,
)
from ums_smart_revenue.db.connector_models import ConnectorRunORM
from ums_smart_revenue.db.org_models import OrgBase
from ums_smart_revenue.db.report_models import ReportBase
from ums_smart_revenue.db.security_models import (
    ApiConnectorCredentialORM,
    AuditLogORM,
    SecurityBase,
    UserORM,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

USER_ID = UUID("00000000-0000-0000-0000-000000004001")


def auth_headers(
    role: str, scope_type: str = "global", scope_id: str | None = None
) -> dict[str, str]:
    """Build trust-gateway auth headers for the given role and optional connector scope."""
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
    """Return the SQLite URL for an isolated per-test connector database under tmp_path."""
    return f"sqlite+pysqlite:///{(tmp_path / 'connectors.db').as_posix()}"


def seed_database(database_url: str) -> None:
    """Create schema tables and seed the test user row for connector endpoint tests."""
    engine = create_engine(database_url)
    try:
        OrgBase.metadata.create_all(engine)
        SecurityBase.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(
                UserORM(
                    id=USER_ID,
                    email="connector-user@example.com",
                    display_name="Connector User",
                )
            )
            session.commit()
    finally:
        engine.dispose()


def test_connector_admin_can_create_credential_reference_without_exposing_secret_ref(
    tmp_path,
):
    """connector_admin can register a secret ref and keep it out of the response."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    external_ref = "secret-manager://ums/youtube-reporting/content-owner-1"

    response = client.post(
        "/connectors/credentials",
        headers=auth_headers("connector_admin"),
        json={
            "connector_key": "youtube_reporting",
            "account_id": "content-owner-1",
            "encrypted_secret_ref": external_ref,
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
    assert credential.encrypted_secret_ref == external_ref
    assert audit_log.event_type == "CONNECTOR_SETTINGS_CHANGED"
    assert audit_log.reason == "Register OAuth credential reference"


def test_connector_credentials_reject_raw_secret_payload(tmp_path):
    """Raw (non-prefixed) secret values are rejected with 422."""
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
    assert (
        response.json()["detail"]
        == "Connector credentials must use an external encrypted secret reference"
    )


def test_external_secret_ref_requires_prefix_and_locator():
    """is_external_secret_ref requires a recognised scheme prefix and a non-blank locator path."""
    assert is_external_secret_ref(
        "secret-manager://ums/youtube-reporting/content-owner-1"
    )
    assert not is_external_secret_ref("")
    assert not is_external_secret_ref("secret-manager://")
    assert not is_external_secret_ref("secret-manager://   ")


def test_connector_credentials_reject_blank_required_strings(tmp_path):
    """Blank-whitespace connector_key is rejected with 422."""
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
    """Non-UUID x-user-id header is rejected with 422 and a clear actor_user_id message."""
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
    """Credential list returns paginated results with correct pagination metadata."""
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
    next_response = client.get(
        "/connectors/credentials?limit=1&offset=1", headers=headers
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["account_id"] == "content-owner-1"
    assert response.json()["pagination"] == {
        "limit": 1,
        "offset": 0,
        "returned": 1,
        "has_more": True,
    }
    assert next_response.status_code == 200
    assert next_response.json()["items"][0]["account_id"] == "content-owner-2"
    assert next_response.json()["pagination"] == {
        "limit": 1,
        "offset": 1,
        "returned": 1,
        "has_more": False,
    }


def test_assistant_cannot_create_connector_credential(tmp_path):
    """assistant_analyst role is denied CREATE with 403 and a clear permission message."""
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
    """revenue_operations_admin can enqueue a connector job and write CONNECTOR_JOB_RUN audit."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/connectors/jobs",
        headers=auth_headers(
            "revenue_operations_admin", "connector", "youtube_reporting"
        ),
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


def test_connector_admin_can_test_connection_ok(tmp_path):
    """connector_admin: ok probe returns 200 status='ok' and writes CONNECTOR_TESTED audit."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    mock_creds = MagicMock()

    with patch(
        "ums_smart_revenue.api.connectors.resolve_connector_credentials",
        return_value=mock_creds,
    ):
        response = client.post(
            "/connectors/credentials/youtube_reporting/content-owner-1/test",
            headers=auth_headers("connector_admin"),
            json={"reason": "Verify OAuth token still valid"},
        )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert response.json()["connector_key"] == "youtube_reporting"
    assert response.json()["account_id"] == "content-owner-1"
    assert response.json()["status"] == "ok"
    assert response.json()["detail"] is None
    assert "audit_event" in response.json()
    assert audit_log.event_type == "CONNECTOR_TESTED"
    assert audit_log.scope_type == "connector"
    assert audit_log.scope_id == "youtube_reporting"
    assert audit_log.reason == "Verify OAuth token still valid"


def test_test_connection_returns_404_for_missing_credential(tmp_path):
    """Missing credential probe returns 404.

    It includes 'not found' detail and writes CONNECTOR_TESTED audit.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    with patch(
        "ums_smart_revenue.api.connectors.resolve_connector_credentials",
        side_effect=CredentialNotFoundError(
            connector_key="youtube_reporting", account_id="no-such-account"
        ),
    ):
        response = client.post(
            "/connectors/credentials/youtube_reporting/no-such-account/test",
            headers=auth_headers("connector_admin"),
            json={"reason": "Diagnose missing credential"},
        )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()
    assert response.json()["status"] == "not_found"
    assert "audit_event" in response.json()
    assert audit_log.event_type == "CONNECTOR_TESTED"
    assert audit_log.scope_id == "youtube_reporting"
    assert audit_log.details["status"] == "not_found"


def test_test_connection_returns_inactive_status_for_inactive_credential(tmp_path):
    """Inactive credential probe returns 200 with status='inactive_credential'."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    with patch(
        "ums_smart_revenue.api.connectors.resolve_connector_credentials",
        side_effect=InactiveCredentialError(
            credential_id="cred-uuid", status="disabled"
        ),
    ):
        response = client.post(
            "/connectors/credentials/youtube_reporting/content-owner-1/test",
            headers=auth_headers("connector_admin"),
            json={"reason": "Check disabled credential"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "inactive_credential"
    assert response.json()["detail"] is not None


def test_test_connection_returns_auth_failed_for_oauth_error(tmp_path):
    """OAuth refresh failure returns 200 with status='auth_failed'."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    with patch(
        "ums_smart_revenue.api.connectors.resolve_connector_credentials",
        side_effect=OAuthRefreshError(inner=Exception("Token has been revoked")),
    ):
        response = client.post(
            "/connectors/credentials/youtube_reporting/content-owner-1/test",
            headers=auth_headers("connector_admin"),
            json={"reason": "Check OAuth state after revoke report"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "auth_failed"
    assert response.json()["detail"] is not None


def test_test_connection_returns_error_for_generic_google_connector_error(tmp_path):
    """Generic GoogleConnectorError (e.g. SecretFetchError) returns 200 with status='error'."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    with patch(
        "ums_smart_revenue.api.connectors.resolve_connector_credentials",
        side_effect=SecretFetchError(
            ref="gcp-secret://project/key",
            inner=Exception("backend unavailable"),
        ),
    ):
        response = client.post(
            "/connectors/credentials/youtube_reporting/content-owner-1/test",
            headers=auth_headers("connector_admin"),
            json={"reason": "Check secret store availability"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "error"
    assert response.json()["detail"] is not None

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()
    assert audit_log.event_type == "CONNECTOR_TESTED"
    assert audit_log.details["status"] == "error"


def test_test_connection_requires_manage_connectors_permission(tmp_path):
    """Non-connector role (assistant_analyst) is rejected with 403."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/connectors/credentials/youtube_reporting/content-owner-1/test",
        headers=auth_headers("assistant_analyst"),
        json={"reason": "Should be denied"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: connectors.manage"


def test_credential_integrity_classifier_uses_duplicate_constraint_only():
    """Integrity classifier returns True only for the unique-constraint violation, not FK errors."""
    # pylint: disable=too-few-public-methods
    class DuplicateDiag:
        """Minimal constraint diagnostic stub for testing the integrity classifier."""

        constraint_name = "uq_api_connector_credentials_connector_account"

    class DuplicateOrigError(Exception):
        """Minimal exception stub simulating a database unique-constraint violation."""

        diag = DuplicateDiag()

    duplicate_error = IntegrityError(
        "insert", {}, DuplicateOrigError("duplicate credential")
    )
    foreign_key_error = IntegrityError(
        "insert", {}, Exception("FOREIGN KEY constraint failed")
    )

    assert _is_duplicate_credential_integrity_error(duplicate_error)
    assert not _is_duplicate_credential_integrity_error(foreign_key_error)


RUN_COUNTS = {
    "reports_attempted": 2,
    "reports_succeeded": 2,
    "reports_failed": 0,
    "rows_upserted_total": 9,
    "rows_upserted_created": 5,
    "rows_upserted_updated": 3,
    "rows_upserted_unchanged": 1,
}


def seed_runs(database_url: str) -> None:
    """Create the connector_runs table and seed three runs for the bootstrap tenant."""
    engine = create_engine(database_url)
    try:
        ReportBase.metadata.create_all(engine)
        tenant = UUID(UMS_TENANT_ID)
        base = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)
        with Session(engine) as session:
            for idx, (key, acct) in enumerate(
                [
                    ("youtube-reporting", "acct-a"),
                    ("youtube-reporting", "acct-b"),
                    ("adsense", "acct-a"),
                ]
            ):
                session.add(
                    ConnectorRunORM(
                        id=uuid4(),
                        tenant_id=tenant,
                        connector_key=key,
                        account_id=acct,
                        report_month="2026-04",
                        triggered_by_user_id=USER_ID,
                        started_at=base.replace(minute=idx),
                        finished_at=base.replace(minute=idx),
                        status="SUCCEEDED",
                        counts_json=dict(RUN_COUNTS),
                        error_summary=None,
                    )
                )
            session.commit()
    finally:
        engine.dispose()


def test_list_runs_returns_envelope_and_item_shape(tmp_path):
    """connector_admin (has VIEW_CONNECTOR_HEALTH) gets the run-history envelope."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    seed_runs(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get("/connectors/runs", headers=auth_headers("connector_admin"))

    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["limit"] == 50
    assert body["pagination"]["returned"] == 3
    assert body["pagination"]["has_more"] is False
    assert body["pagination"]["next_cursor"] is None
    item = body["items"][0]
    assert "tenant_id" not in item
    assert item["status"] == "SUCCEEDED"
    assert item["counts"] == RUN_COUNTS
    assert item["report_month"] == "2026-04"


def test_list_runs_allows_connector_scoped_health_access(tmp_path):
    """connector-scoped connector_admin gets only the permitted connector runs."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    seed_runs(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/connectors/runs",
        headers=auth_headers("connector_admin", "connector", "youtube_reporting"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["returned"] == 2
    assert {item["connector_key"] for item in body["items"]} == {
        "youtube-reporting"
    }


def test_list_runs_rejects_connector_outside_scoped_health_access(tmp_path):
    """Connector-scoped connector_admin cannot request another connector's runs."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    seed_runs(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/connectors/runs",
        headers=auth_headers("connector_admin", "connector", "youtube_reporting"),
        params={"connector_key": "adsense"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: connectors.view_health"


def test_list_runs_forbidden_without_view_connector_health(tmp_path):
    """audit_viewer lacks VIEW_CONNECTOR_HEALTH and is fail-closed with 403."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    seed_runs(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get("/connectors/runs", headers=auth_headers("audit_viewer"))

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: connectors.view_health"


def test_list_runs_honors_filters(tmp_path):
    """connector_key + account_id query filters narrow the run history result."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    seed_runs(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/connectors/runs",
        headers=auth_headers("connector_admin"),
        params={"connector_key": "adsense", "account_id": "acct-a"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["pagination"]["returned"] == 1
    assert body["items"][0]["connector_key"] == "adsense"
    assert body["items"][0]["account_id"] == "acct-a"


def test_list_runs_half_cursor_returns_422(tmp_path):
    """Supplying only cursor_started_at (no cursor_id) is a 422 validation error."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    seed_runs(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/connectors/runs",
        headers=auth_headers("connector_admin"),
        params={"cursor_started_at": "2026-04-01T12:00:00+00:00"},
    )

    assert response.status_code == 422


def test_list_runs_limit_over_cap_returns_422(tmp_path):
    """Limit above the 100 cap is rejected by FastAPI Query validation with 422."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    seed_runs(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/connectors/runs",
        headers=auth_headers("connector_admin"),
        params={"limit": 101},
    )

    assert response.status_code == 422
