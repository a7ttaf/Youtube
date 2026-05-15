from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.report_models import RawReportFileORM, ReportBase
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

USER_ID = UUID("00000000-0000-0000-0000-000000010001")


def auth_headers(role: str, scope_type: str = "global", scope_id: str | None = None) -> dict[str, str]:
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


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'raw-report-files.db').as_posix()}"


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    ReportBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(UserORM(id=USER_ID, email="raw-report-user@example.com", display_name="Raw Report User"))
        session.commit()


def test_system_integration_user_registers_raw_report_file_metadata_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

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
            "reason": "Register downloaded official YouTube CMS revenue report",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        raw_file = session.scalars(select(RawReportFileORM)).one()
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 201
    assert response.json()["source"] == "youtube_reporting"
    assert response.json()["report_month"] == "2026-03"
    assert response.json()["storage_uri"] == "s3://ums-raw-reports/youtube/2026-03/cms.csv"
    assert response.json()["audit_event"]["event_type"] == "REPORT_IMPORTED"
    assert raw_file.checksum == "sha256:83f8b7d92d8a"
    assert audit_log.event_type == "REPORT_IMPORTED"
    assert audit_log.scope_type == "connector"
    assert audit_log.scope_id == "youtube_reporting"
    assert audit_log.sensitive is True


def test_connector_admin_reads_raw_report_file_metadata_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
        "/reports/raw-files",
        headers=auth_headers("system_integration_user", "connector", "youtube_reporting"),
        json={
            "source": "youtube_reporting",
            "report_type": "YOUTUBE_CMS_REVENUE",
            "report_month": "2026-03",
            "storage_uri": "s3://ums-raw-reports/youtube/2026-03/cms.csv",
            "checksum": "sha256:83f8b7d92d8a",
            "parse_status": "DOWNLOADED",
            "reason": "Register downloaded official YouTube CMS revenue report",
        },
    )

    response = client.get(
        f"/reports/raw-files/{create_response.json()['id']}",
        headers=auth_headers("connector_admin", "connector", "youtube_reporting"),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_logs = session.scalars(select(AuditLogORM).order_by(AuditLogORM.event_type)).all()

    assert response.status_code == 200
    assert response.json()["checksum"] == "sha256:83f8b7d92d8a"
    assert {log.event_type for log in audit_logs} == {"RAW_FILE_VIEWED", "REPORT_IMPORTED"}


def test_assistant_cannot_view_raw_report_files(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/reports/raw-files?source=youtube_reporting",
        headers=auth_headers("assistant_analyst", "connector", "youtube_reporting"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: raw_files.view"


def test_raw_report_file_registration_rejects_inline_or_local_storage_reference(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/reports/raw-files",
        headers=auth_headers("system_integration_user", "connector", "youtube_reporting"),
        json={
            "source": "youtube_reporting",
            "report_type": "YOUTUBE_CMS_REVENUE",
            "report_month": "2026-03",
            "storage_uri": "C:/Users/Mrmah/Downloads/cms.csv",
            "checksum": "sha256:83f8b7d92d8a",
            "parse_status": "DOWNLOADED",
            "reason": "Attempt to register a local file path",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "storage_uri must point to approved object storage, not raw inline report content"
    )


def test_raw_report_file_registration_rejects_duplicate_artifact_metadata(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    payload = {
        "source": "youtube_reporting",
        "report_type": "YOUTUBE_CMS_REVENUE",
        "report_month": "2026-03",
        "storage_uri": "s3://ums-raw-reports/youtube/2026-03/cms.csv",
        "checksum": "sha256:83f8b7d92d8a",
        "parse_status": "DOWNLOADED",
        "reason": "Register downloaded official YouTube CMS revenue report",
    }

    first = client.post(
        "/reports/raw-files",
        headers=auth_headers("system_integration_user", "connector", "youtube_reporting"),
        json=payload,
    )
    duplicate = client.post(
        "/reports/raw-files",
        headers=auth_headers("system_integration_user", "connector", "youtube_reporting"),
        json=payload,
    )

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Raw report file metadata already exists"


def test_connector_admin_lists_raw_report_files_for_authorized_source_only(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    for source, checksum in (("youtube_reporting", "sha256:83f8b7d92d8a"), ("adsense", "sha256:66b0e83cf04a")):
        create_response = client.post(
            "/reports/raw-files",
            headers=auth_headers("system_integration_user", "connector", source),
            json={
                "source": source,
                "report_type": "YOUTUBE_CMS_REVENUE" if source == "youtube_reporting" else "ADSENSE_PAYMENT",
                "report_month": "2026-03",
                "storage_uri": f"s3://ums-raw-reports/{source}/2026-03/report.csv",
                "checksum": checksum,
                "parse_status": "DOWNLOADED",
                "reason": f"Register {source} report",
            },
        )
        assert create_response.status_code == 201

    response = client.get(
        "/reports/raw-files?source=youtube_reporting&limit=1&offset=0",
        headers=auth_headers("connector_admin", "connector", "youtube_reporting"),
    )
    denied = client.get(
        "/reports/raw-files?source=adsense",
        headers=auth_headers("connector_admin", "connector", "youtube_reporting"),
    )

    assert response.status_code == 200
    assert response.json()["items"][0]["source"] == "youtube_reporting"
    assert response.json()["pagination"] == {"limit": 1, "offset": 0, "returned": 1, "has_more": False}
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Missing permission: raw_files.view"


def test_connector_admin_cannot_view_other_connector_raw_file(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
        "/reports/raw-files",
        headers=auth_headers("system_integration_user", "connector", "adsense"),
        json={
            "source": "adsense",
            "report_type": "ADSENSE_PAYMENT",
            "report_month": "2026-03",
            "storage_uri": "s3://ums-raw-reports/adsense/2026-03/payment.csv",
            "checksum": "sha256:66b0e83cf04a",
            "parse_status": "DOWNLOADED",
            "reason": "Register downloaded AdSense payment report",
        },
    )

    response = client.get(
        f"/reports/raw-files/{create_response.json()['id']}",
        headers=auth_headers("connector_admin", "connector", "youtube_reporting"),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Raw report file not found"


def test_user_without_raw_file_permission_cannot_probe_raw_file_ids(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/reports/raw-files/not-a-uuid",
        headers=auth_headers("assistant_analyst", "connector", "youtube_reporting"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: raw_files.view"
