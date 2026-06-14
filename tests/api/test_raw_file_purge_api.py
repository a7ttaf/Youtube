from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

import ums_smart_revenue.db.session as db_session
from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.report_models import RawReportFileORM, ReportBase
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.reports.raw_files import (
    RawReportFileConflictError,
    SqlAlchemyRawReportFileRepository,
)

USER_ID = UUID("00000000-0000-0000-0000-000000010001")


def auth_headers(
    role: str, scope_type: str = "global", scope_id: str | None = None
) -> dict[str, str]:
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "raw-report-user@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


@pytest.fixture
def app_env(monkeypatch):
    """Drive the real app over one shared in-memory SQLite connection.

    A file-backed SQLite engine locks under the app's connection pool during
    the audit write on this machine (pre-existing; also affects
    test_raw_report_files_api). A StaticPool single connection avoids the lock
    while still exercising the live route, app wiring, and audit persistence.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SecurityBase.metadata.create_all(engine)
    ReportBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            UserORM(
                id=USER_ID,
                email="raw-report-user@example.com",
                display_name="Raw Report User",
            )
        )
        session.commit()

    db_session._engine_cache.pop("sqlite://", None)
    monkeypatch.setattr(db_session, "build_engine", lambda url: engine)
    client = TestClient(create_app(database_url="sqlite://"))
    yield client, engine
    db_session._engine_cache.pop("sqlite://", None)
    engine.dispose()


def _register(client: TestClient) -> str:
    response = client.post(
        "/reports/raw-files",
        headers=auth_headers("system_integration_user", "connector", "youtube_reporting"),
        json={
            "source": "youtube_reporting",
            "report_type": "YOUTUBE_CMS_REVENUE",
            "report_month": "2026-03",
            "storage_uri": "s3://ums-raw-reports/youtube/2026-03/cms.csv",
            "checksum": "sha256:83f8b7d92d8a",
            "parse_status": "DOWNLOADED",
            "reason": "Register downloaded report",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def test_register_rejects_purged_parse_status(app_env):
    client, engine = app_env

    response = client.post(
        "/reports/raw-files",
        headers=auth_headers("system_integration_user", "connector", "youtube_reporting"),
        json={
            "source": "youtube_reporting",
            "report_type": "YOUTUBE_CMS_REVENUE",
            "report_month": "2026-03",
            "storage_uri": "s3://ums-raw-reports/youtube/2026-03/cms.csv",
            "checksum": "sha256:83f8b7d92d8a",
            "parse_status": "PURGED",
            "reason": "Register downloaded report",
        },
    )

    assert response.status_code == 422
    with Session(engine) as session:
        rows = session.scalars(select(RawReportFileORM)).all()
    assert rows == []


def test_connector_admin_purges_raw_report_file_with_audit(app_env):
    client, engine = app_env
    raw_file_id = _register(client)

    response = client.request(
        "DELETE",
        f"/reports/raw-files/{raw_file_id}",
        headers=auth_headers("connector_admin", "connector", "youtube_reporting"),
        json={"reason": "Operator-requested deletion"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["parse_status"] == "PURGED"
    assert body["storage_uri"] == ""
    assert body["purged_by"] is not None
    assert body["audit_event"]["event_type"] == "REPORT_PURGED"

    with Session(engine) as session:
        row = session.scalars(select(RawReportFileORM)).one()
        purge_log = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == "REPORT_PURGED")
        ).one()
    assert row.parse_status == "PURGED"
    assert row.file_url == ""
    assert row.checksum == "sha256:83f8b7d92d8a"
    assert purge_log.reason == "Operator-requested deletion"
    assert purge_log.sensitive is True


def test_purge_conflict_returns_409(app_env, monkeypatch):
    client, _ = app_env
    raw_file_id = _register(client)

    def raise_conflict(self, *, raw_file_id, actor_user_id, reason):
        raise RawReportFileConflictError("Raw report file purge was not applied")

    monkeypatch.setattr(SqlAlchemyRawReportFileRepository, "purge_file", raise_conflict)

    response = client.request(
        "DELETE",
        f"/reports/raw-files/{raw_file_id}",
        headers=auth_headers("connector_admin", "connector", "youtube_reporting"),
        json={"reason": "Operator-requested deletion"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Raw report file purge was not applied"


def test_purge_missing_permission_returns_403(app_env):
    client, _ = app_env
    raw_file_id = _register(client)

    response = client.request(
        "DELETE",
        f"/reports/raw-files/{raw_file_id}",
        headers=auth_headers("system_integration_user", "connector", "youtube_reporting"),
        json={"reason": "Operator-requested deletion"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: connectors.manage"


def test_purge_missing_reason_returns_422(app_env):
    client, _ = app_env
    raw_file_id = _register(client)

    response = client.request(
        "DELETE",
        f"/reports/raw-files/{raw_file_id}",
        headers=auth_headers("connector_admin", "connector", "youtube_reporting"),
        json={"reason": "   "},
    )

    assert response.status_code == 422


def test_purge_unknown_id_returns_404(app_env):
    client, _ = app_env

    response = client.request(
        "DELETE",
        f"/reports/raw-files/{uuid4()}",
        headers=auth_headers("connector_admin", "connector", "youtube_reporting"),
        json={"reason": "Operator-requested deletion"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Raw report file not found"


def test_repurge_returns_409(app_env):
    client, _ = app_env
    raw_file_id = _register(client)

    first = client.request(
        "DELETE",
        f"/reports/raw-files/{raw_file_id}",
        headers=auth_headers("connector_admin", "connector", "youtube_reporting"),
        json={"reason": "first"},
    )
    assert first.status_code == 200

    second = client.request(
        "DELETE",
        f"/reports/raw-files/{raw_file_id}",
        headers=auth_headers("connector_admin", "connector", "youtube_reporting"),
        json={"reason": "second"},
    )
    assert second.status_code == 409
