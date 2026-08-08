from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

SECTOR_ID = UUID("00000000-0000-0000-0000-000000001101")
COMPANY_ID = UUID("00000000-0000-0000-0000-000000001201")
COMPANY_REMAP_ID = UUID("00000000-0000-0000-0000-000000001202")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-000000001301")
USER_ID = UUID("00000000-0000-0000-0000-000000001401")


def auth_headers(role: str, scope_type: str, scope_id: str | None = None) -> dict[str, str]:
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "sql-user@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'ums.db').as_posix()}"


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                OrgUnitORM(id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV", active=True),
                OrgUnitORM(
                    id=COMPANY_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="TV Company",
                    active=True,
                ),
                OrgUnitORM(
                    id=COMPANY_REMAP_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="TV Remap",
                    active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_ROW_ID,
                    youtube_channel_id="sql-channel-tv-a",
                    channel_name="SQL TV A",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                UserORM(
                    id=USER_ID,
                    email="sql-user@example.com",
                    display_name="SQL User",
                    status="active",
                ),
            ]
        )
        session.commit()


def test_app_can_use_sql_backed_channel_dependencies(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    list_response = client.get(
        "/channels",
        headers=auth_headers("company_manager", "company", str(COMPANY_ID)),
    )
    create_response = client.post(
        "/channels",
        headers=auth_headers("data_steward", "company", str(COMPANY_ID)),
        json={
            "youtube_channel_id": "sql-channel-tv-b",
            "channel_name": "SQL TV B",
            "primary_company_id": str(COMPANY_ID),
            "cms_status": "UNKNOWN",
            "revenue_required": True,
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        persisted = session.scalars(
            select(YouTubeChannelORM).where(
                YouTubeChannelORM.youtube_channel_id == "sql-channel-tv-b"
            )
        ).one()

    assert list_response.status_code == 200
    assert [channel["youtube_channel_id"] for channel in list_response.json()] == [
        "sql-channel-tv-a"
    ]
    assert create_response.status_code == 201
    assert persisted.primary_org_unit_id == COMPANY_ID


def test_database_backed_channel_mapping_persists_audit_log(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.patch(
        "/channels/sql-channel-tv-a/mapping",
        headers=auth_headers("corporate_admin", "global"),
        json={
            "primary_company_id": str(COMPANY_REMAP_ID),
            "reason": "Validate persistent audit trail",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()
        channel = session.scalars(
            select(YouTubeChannelORM).where(
                YouTubeChannelORM.youtube_channel_id == "sql-channel-tv-a"
            )
        ).one()

    assert response.status_code == 200
    assert response.json()["primary_company_id"] == str(COMPANY_REMAP_ID)
    assert response.json()["audit_event"]["event_type"] == "CHANNEL_UPDATED"
    assert channel.primary_org_unit_id == COMPANY_REMAP_ID
    assert audit_log.user_id == USER_ID
    assert audit_log.entity_id == "sql-channel-tv-a"
    assert audit_log.reason == "Validate persistent audit trail"
    assert audit_log.is_sensitive is True


def test_app_uses_database_url_from_environment(tmp_path, monkeypatch):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    monkeypatch.setenv("UMS_DATABASE_URL", database_url)
    client = TestClient(create_app())

    response = client.get(
        "/channels",
        headers=auth_headers("company_manager", "company", str(COMPANY_ID)),
    )

    assert response.status_code == 200
    assert [channel["youtube_channel_id"] for channel in response.json()] == ["sql-channel-tv-a"]
