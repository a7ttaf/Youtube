from uuid import UUID

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.org_models import (
    ChannelGroupMemberORM,
    ChannelGroupORM,
    OrgBase,
    OrgUnitORM,
    YouTubeChannelORM,
)
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.org.sql_channel_groups import (
    SqlAlchemyChannelGroupRegistry,
    _is_duplicate_group_member_integrity_error,
)

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
                OrgUnitORM(
                    id=SECTOR_ID,
                    parent_id=None,
                    type="SECTOR",
                    name="All Sectors",
                    active=True,
                ),
                OrgUnitORM(
                    id=COMPANY_TV_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="TV Company",
                    active=True,
                ),
                OrgUnitORM(
                    id=COMPANY_NEWS_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="News Company",
                    active=True,
                ),
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
                ChannelGroupORM(
                    id=GROUP_TV_ID,
                    name="TV Brand Group",
                    group_type="TV_BRAND",
                    active=True,
                ),
                ChannelGroupORM(
                    id=GROUP_MIXED_ID,
                    name="Mixed Corporate Group",
                    group_type="CUSTOM_GROUP",
                    active=True,
                ),
                ChannelGroupMemberORM(
                    group_id=GROUP_TV_ID,
                    channel_id=CHANNEL_TV_ROW_ID,
                ),
                ChannelGroupMemberORM(
                    group_id=GROUP_MIXED_ID,
                    channel_id=CHANNEL_TV_ROW_ID,
                ),
                ChannelGroupMemberORM(
                    group_id=GROUP_MIXED_ID,
                    channel_id=CHANNEL_NEWS_ROW_ID,
                ),
                UserORM(
                    id=USER_ID,
                    email="groups-user@example.com",
                    display_name="Groups User",
                    status="active",
                ),
            ]
        )
        session.commit()


def test_company_manager_lists_only_groups_fully_inside_scope(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/groups",
        headers=auth_headers("company_manager", "company", str(COMPANY_TV_ID)),
    )

    assert response.status_code == 200
    assert [group["name"] for group in response.json()] == ["TV Brand Group"]
    assert response.json()[0]["channel_ids"] == ["group-channel-tv"]


def test_groups_endpoint_fails_closed_without_sql_registry():
    client = TestClient(create_app())

    response = client.get(
        "/groups",
        headers=auth_headers("corporate_admin", "global"),
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "database session not configured"


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
        group = session.scalars(
            select(ChannelGroupORM).where(ChannelGroupORM.name == "TV Finance Review")
        ).one()
        audit_log = session.scalars(
            select(AuditLogORM).where(AuditLogORM.entity_id == str(group.id))
        ).one()

    assert response.status_code == 201
    assert response.json()["channel_ids"] == ["group-channel-tv"]
    assert audit_log.event_type == "GROUP_UPDATED"
    assert audit_log.reason == "Create monthly finance review group"
    assert audit_log.sensitive is True


def test_create_group_rejects_unknown_channel_id(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/groups",
        headers=auth_headers("corporate_admin", "global"),
        json={
            "name": "Unknown Channel Review",
            "group_type": "CUSTOM_GROUP",
            "channel_ids": ["missing-channel"],
            "reason": "Reject unknown channel",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Channel not found: missing-channel"


def test_create_group_rejects_blank_audit_reason(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/groups",
        headers=auth_headers("corporate_admin", "global"),
        json={
            "name": "Blank Reason Review",
            "group_type": "CUSTOM_GROUP",
            "channel_ids": ["group-channel-tv"],
            "reason": "   ",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "Value error, must not be blank"


def test_update_group_rejects_blank_audit_reason(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.patch(
        f"/groups/{GROUP_TV_ID}",
        headers=auth_headers("corporate_admin", "global"),
        json={"name": "Renamed TV Group", "reason": "   "},
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "Value error, must not be blank"


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


def test_data_steward_cannot_probe_group_outside_scope(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.patch(
        f"/groups/{GROUP_MIXED_ID}",
        headers=auth_headers("data_steward", "company", str(COMPANY_TV_ID)),
        json={"name": "Probe Mixed Group", "reason": "Attempt scoped update"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Group not found"


def test_corporate_admin_can_add_and_remove_group_members_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    add_response = client.post(
        f"/groups/{GROUP_TV_ID}/members",
        headers=auth_headers("corporate_admin", "global"),
        json={
            "channel_ids": ["group-channel-news"],
            "reason": "Add news channel for corporate group review",
        },
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
            .join(
                ChannelGroupMemberORM,
                ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id,
            )
            .where(ChannelGroupMemberORM.group_id == GROUP_TV_ID)
            .order_by(YouTubeChannelORM.youtube_channel_id)
        ).all()

    assert add_response.status_code == 200
    assert add_response.json()["channel_ids"] == [
        "group-channel-news",
        "group-channel-tv",
    ]
    assert remove_response.status_code == 200
    assert remove_response.json()["channel_ids"] == ["group-channel-news"]
    assert member_channel_ids == ["group-channel-news"]
    assert [event.reason for event in audit_events] == [
        "Add news channel for corporate group review",
        "Remove TV channel after review",
    ]


def test_add_group_members_rejects_unknown_channel_id(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/groups/{GROUP_TV_ID}/members",
        headers=auth_headers("corporate_admin", "global"),
        json={
            "channel_ids": ["missing-channel"],
            "reason": "Reject unknown channel",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Channel not found: missing-channel"


def test_add_group_members_rejects_blank_audit_reason(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        f"/groups/{GROUP_TV_ID}/members",
        headers=auth_headers("corporate_admin", "global"),
        json={
            "channel_ids": ["group-channel-news"],
            "reason": "   ",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == "Value error, must not be blank"


def test_remove_group_member_rejects_blank_audit_reason(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.delete(
        f"/groups/{GROUP_TV_ID}/members/group-channel-tv",
        headers=auth_headers("corporate_admin", "global"),
        params={"reason": "   "},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "reason must not be blank"


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


def test_sql_group_registry_excludes_inactive_member_channels(tmp_path):
    """A group whose only member channel has been deactivated is reported as empty.

    Regression for Qodo review #122: the revenue scope selector must not offer
    a group that resolves to zero active member channels, otherwise a
    net-revenue read against that group 200s with an empty body instead of
    being omitted like other dead options.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)

    # Deactivate the only member channel of GROUP_TV_ID.
    with Session(engine) as session:
        channel = session.get(YouTubeChannelORM, CHANNEL_TV_ROW_ID)
        channel.active = False
        session.commit()

    with Session(engine) as session:
        registry = SqlAlchemyChannelGroupRegistry(session)
        groups = {group.id: group for group in registry.list_groups()}

    assert str(GROUP_TV_ID) in groups
    assert groups[str(GROUP_TV_ID)].channel_ids == ()

    # The mixed group still resolves to its remaining active member.
    assert str(GROUP_MIXED_ID) in groups
    assert groups[str(GROUP_MIXED_ID)].channel_ids == ("group-channel-news",)


def test_sql_group_registry_get_group_matches_list_groups_active_filter(tmp_path):
    """get_group and list_groups must agree on which member channels are active.

    Regression for Gitar review #122: the list_groups path filters
    YouTubeChannelORM.active in _channel_ids_by_group, but the get_group
    fallback in _to_entry previously did not, causing the scope selector and
    the revenue read path to operate on different member sets for the same
    group.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)

    with Session(engine) as session:
        channel = session.get(YouTubeChannelORM, CHANNEL_TV_ROW_ID)
        channel.active = False
        session.commit()

    with Session(engine) as session:
        registry = SqlAlchemyChannelGroupRegistry(session)
        via_list = {g.id: g for g in registry.list_groups()}
        via_get = registry.get_group(str(GROUP_TV_ID))

    assert via_get is not None
    assert via_get.channel_ids == via_list[str(GROUP_TV_ID)].channel_ids
    assert via_get.channel_ids == ()


def test_sql_group_add_members_treats_duplicate_race_as_idempotent(tmp_path, monkeypatch):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)

    with Session(engine) as session:
        registry = SqlAlchemyChannelGroupRegistry(session)
        original_flush = session.flush
        injected_duplicate = False

        def flush_with_concurrent_duplicate(*args, **kwargs):
            nonlocal injected_duplicate
            if not injected_duplicate:
                injected_duplicate = True
                with Session(engine) as other_session:
                    other_session.add(
                        ChannelGroupMemberORM(
                            group_id=GROUP_TV_ID,
                            channel_id=CHANNEL_NEWS_ROW_ID,
                        )
                    )
                    other_session.commit()
            return original_flush(*args, **kwargs)

        monkeypatch.setattr(session, "flush", flush_with_concurrent_duplicate)

        updated = registry.add_members(
            group_id=str(GROUP_TV_ID), channel_ids=["group-channel-news"]
        )

    assert updated.channel_ids == ("group-channel-news", "group-channel-tv")


def test_group_member_integrity_error_classifier_matches_composite_primary_key():
    duplicate_error = IntegrityError(
        "insert",
        {},
        Exception(
            "UNIQUE constraint failed: "
            "channel_group_members.group_id, channel_group_members.channel_id"
        ),
    )
    foreign_key_error = IntegrityError(
        "insert",
        {},
        Exception("FOREIGN KEY constraint failed"),
    )

    assert _is_duplicate_group_member_integrity_error(duplicate_error)
    assert not _is_duplicate_group_member_integrity_error(foreign_key_error)
