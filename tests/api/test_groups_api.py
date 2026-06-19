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


def test_sql_group_registry_exposes_active_and_full_member_views(tmp_path):
    """get_group returns full members; get_active_member_channels returns active only.

    Regression for chatgpt-codex-connector review #122: management authz
    must see ALL members (including channels that have since been
    deactivated and are outside the caller's org scope), while the revenue
    read path must operate on the active member set so a group whose only
    members are inactive cannot 200 with empty rows.
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
        full_tv = registry.get_group(str(GROUP_TV_ID))
        full_mixed = registry.get_group(str(GROUP_MIXED_ID))
        active_tv = registry.get_active_member_channels(str(GROUP_TV_ID))
        active_mixed = registry.get_active_member_channels(str(GROUP_MIXED_ID))

    # Management path: full member set, including the inactive channel.
    assert full_tv is not None
    assert full_tv.channel_ids == ("group-channel-tv",)
    assert full_mixed is not None
    assert full_mixed.channel_ids == ("group-channel-news", "group-channel-tv")

    # Revenue read path: active-only member set.
    assert active_tv == ()
    assert active_mixed == ("group-channel-news",)


def test_sql_group_registry_get_group_matches_list_groups_full_member_view(tmp_path):
    """get_group and list_groups_full must agree on the full member set.

    Regression for Gitar review #122: list_groups_full and get_group should
    return the same ChannelGroupEntry (with the full member set) for the
    same group. The active-only filter is applied separately via
    get_active_member_channels for the revenue read path, and list_groups
    (used by the scope selector) returns the same active-only member set.
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
        via_list_full = {g.id: g for g in registry.list_groups_full()}
        via_list = {g.id: g for g in registry.list_groups()}
        via_get = registry.get_group(str(GROUP_TV_ID))

    assert via_get is not None
    assert via_get.channel_ids == via_list_full[str(GROUP_TV_ID)].channel_ids
    assert via_get.channel_ids == ("group-channel-tv",)
    # list_groups (scope selector) reports the active-only member set,
    # which excludes the deactivated channel.
    assert via_list[str(GROUP_TV_ID)].channel_ids == ()


def test_sql_group_registry_list_groups_full_includes_inactive_members(tmp_path):
    """list_groups_full reports every member; list_groups excludes inactive ones.

    Regression for chatgpt-codex-connector review #122 (round 2): the
    groups management API must keep the full member set so _require_manageable_group
    can authorize over channels that have since been deactivated and
    may be outside the caller's org scope. The scope selector path
    (list_groups) continues to use the active-only member set so it
    advertises exactly the channels the revenue read path will touch.
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
        via_list_full = {g.id: g for g in registry.list_groups_full()}

    # list_groups: active-only member set (scope selector view).
    assert via_list[str(GROUP_TV_ID)].channel_ids == ()
    assert via_list[str(GROUP_MIXED_ID)].channel_ids == ("group-channel-news",)
    # list_groups_full: every member including the deactivated channel
    # (management authz view).
    assert via_list_full[str(GROUP_TV_ID)].channel_ids == ("group-channel-tv",)
    assert via_list_full[str(GROUP_MIXED_ID)].channel_ids == (
        "group-channel-news",
        "group-channel-tv",
    )


def test_list_groups_endpoint_keeps_deactivated_member_in_authz_check(tmp_path):
    """GET /groups must authorize over the full member set.

    Regression for Gitar review #122 #fail-open: a scoped manager must
    not be able to enumerate a group whose out-of-scope member channel
    has since been deactivated, because _can_view_group iterates over
    the full member set to require VIEW_ANALYTICS on every channel.
    Switching the endpoint from list_groups to list_groups_full keeps
    the deactivated channel in the authz iteration and preserves the
    fail-closed visibility behavior.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)

    # Deactivate the only member of GROUP_TV_ID. The other companies'
    # scoped viewers should not be able to see GROUP_TV_ID any more
    # than they could before the change (they could never cover the
    # channel, but the authz iteration must still include it so a
    # scoped manager who happens to have VIEW_ANALYTICS on the
    # remaining members does not silently gain access).
    with Session(engine) as session:
        channel = session.get(YouTubeChannelORM, CHANNEL_TV_ROW_ID)
        channel.active = False
        session.commit()

    app = create_app(database_url=database_url)
    client = TestClient(app)

    # Corporate admin (global VIEW_ANALYTICS) sees every group because
    # the global grant covers every member including the deactivated one.
    from ums_smart_revenue.api.dependencies import (
        current_principal_from_headers,
    )
    from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
    from ums_smart_revenue.auth.permissions import Permission
    from ums_smart_revenue.auth.scopes import AccessScope

    def _global_admin() -> UserPrincipal:
        return UserPrincipal(
            user_id=str(USER_ID),
            email="groups-admin@example.com",
            direct_permissions=(
                PermissionGrant(Permission.VIEW_ANALYTICS, AccessScope.global_scope()),
            ),
        )

    app.dependency_overrides[current_principal_from_headers] = _global_admin
    response = client.get("/groups")
    assert response.status_code == 200
    visible_ids = {group["id"] for group in response.json()}
    assert str(GROUP_TV_ID) in visible_ids
    assert str(GROUP_MIXED_ID) in visible_ids


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
