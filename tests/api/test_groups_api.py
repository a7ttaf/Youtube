from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.org_models import ChannelGroupMemberORM, ChannelGroupORM, OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM


SECTOR_ID = UUID("00000000-0000-0000-0000-000000003101")
COMPANY_TV_ID = UUID("00000000-0000-0000-0000-000000003201")
COMPANY_NEWS_ID = UUID("00000000-0000-0000-0000-000000003202")
CHANNEL_TV_ROW_ID = UUID("00000000-0000-0000-0000-000000003301")
CHANNEL_NEWS_ROW_ID = UUID("00000000-0000-0000-0000-000000003302")
GROUP_TV_ID = UUID("00000000-0000-0000-0000-000000003401")
GROUP_MIXED_ID = UUID("00000000-0000-0000-0000-000000003402")
USER_ID = UUID("00000000-0000-0000-0000-000000003501")


def auth_headers(role: str, scope_type: str, scope_id: str | None = None) -> dict[str, str]:
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "groups-user@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / 'groups.db').as_posix()}"


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                OrgUnitORM(id=SECTOR_ID, parent_id=None, type="SECTOR", name="All Sectors", active=True),
                OrgUnitORM(id=COMPANY_TV_ID, parent_id=SECTOR_ID, type="COMPANY", name="TV Company", active=True),
                OrgUnitORM(id=COMPANY_NEWS_ID, parent_id=SECTOR_ID, type="COMPANY", name="News Company", active=True),
                YouTubeChannelORM(
                    id=CHANNEL_TV_ROW_ID,
                    youtube_channel_id="group-channel-tv",
                    channel_name="Group TV",
                    primary_org_unit_id=COMPANY_TV_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_NEWS_ROW_ID,
                    youtube_channel_id="group-channel-news",
                    channel_name="Group News",
                    primary_org_unit_id=COMPANY_NEWS_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                ChannelGroupORM(id=GROUP_TV_ID, name="TV Brand Group", group_type="TV_BRAND", active=True),
                ChannelGroupORM(id=GROUP_MIXED_ID, name="Mixed Corporate Group", group_type="CUSTOM_GROUP", active=True),
                ChannelGroupMemberORM(group_id=GROUP_TV_ID, channel_id=CHANNEL_TV_ROW_ID),
                ChannelGroupMemberORM(group_id=GROUP_MIXED_ID, channel_id=CHANNEL_TV_ROW_ID),
                ChannelGroupMemberORM(group_id=GROUP_MIXED_ID, channel_id=CHANNEL_NEWS_ROW_ID),
                UserORM(id=USER_ID, email="groups-user@example.com", display_name="Groups User", status="active"),
            ]
        )
        session.commit()


def test_company_manager_lists_only_groups_fully_inside_scope(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get("/groups", headers=auth_headers("company_manager", "company", str(COMPANY_TV_ID)))

    assert response.status_code == 200
    assert [group["name"] for group in response.json()] == ["TV Brand Group"]
    assert response.json()[0]["channel_ids"] == ["group-channel-tv"]


def test_data_steward_can_create_group_inside_scope_and_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/groups",
        headers=auth_headers("data_steward", "company", str(COMPANY_TV_ID)),
        json={
            "name": "TV Finance Review",
            "group_type": "FINANCE_GROUP",
            "channel_ids": ["group-channel-tv"],
            "reason": "Create monthly finance review group",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        group = session.scalars(select(ChannelGroupORM).where(ChannelGroupORM.name == "TV Finance Review")).one()
        audit_log = session.scalars(select(AuditLogORM).where(AuditLogORM.entity_id == str(group.id))).one()

    assert response.status_code == 201
    assert response.json()["channel_ids"] == ["group-channel-tv"]
    assert audit_log.event_type == "GROUP_UPDATED"
    assert audit_log.reason == "Create monthly finance review group"
    assert audit_log.sensitive is True


def test_data_steward_cannot_create_group_with_other_company_channel(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/groups",
        headers=auth_headers("data_steward", "company", str(COMPANY_TV_ID)),
        json={
            "name": "Cross Company Review",
            "group_type": "CUSTOM_GROUP",
            "channel_ids": ["group-channel-tv", "group-channel-news"],
            "reason": "Should be denied",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: registry.manage_groups"


def test_corporate_admin_can_add_and_remove_group_members_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    add_response = client.post(
        f"/groups/{GROUP_TV_ID}/members",
        headers=auth_headers("corporate_admin", "global"),
        json={"channel_ids": ["group-channel-news"], "reason": "Add news channel for corporate group review"},
    )
    remove_response = client.delete(
        f"/groups/{GROUP_TV_ID}/members/group-channel-tv",
        headers=auth_headers("corporate_admin", "global"),
        params={"reason": "Remove TV channel after review"},
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_events = session.scalars(
            select(AuditLogORM)
            .where(AuditLogORM.entity_id == str(GROUP_TV_ID))
            .order_by(AuditLogORM.created_at, AuditLogORM.reason)
        ).all()
        member_channel_ids = session.scalars(
            select(YouTubeChannelORM.youtube_channel_id)
            .join(ChannelGroupMemberORM, ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id)
            .where(ChannelGroupMemberORM.group_id == GROUP_TV_ID)
            .order_by(YouTubeChannelORM.youtube_channel_id)
        ).all()

    assert add_response.status_code == 200
    assert add_response.json()["channel_ids"] == ["group-channel-news", "group-channel-tv"]
    assert remove_response.status_code == 200
    assert remove_response.json()["channel_ids"] == ["group-channel-news"]
    assert member_channel_ids == ["group-channel-news"]
    assert [event.reason for event in audit_events] == [
        "Add news channel for corporate group review",
        "Remove TV channel after review",
    ]


def test_malformed_group_id_returns_not_found(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.delete(
        "/groups/not-a-uuid/members/group-channel-tv",
        headers=auth_headers("corporate_admin", "global"),
        params={"reason": "Reject malformed group id"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Group not found"
