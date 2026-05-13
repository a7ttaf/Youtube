from fastapi.testclient import TestClient

from ums_smart_revenue.api.channels import current_channel_registry
from ums_smart_revenue.api.groups import current_group_registry
from ums_smart_revenue.api.revenue import current_org_access_index
from ums_smart_revenue.app import create_app
from ums_smart_revenue.org.bootstrap_registry import (
    BOOTSTRAP_COMPANY_NEWS_ID,
    BOOTSTRAP_COMPANY_TV_ID,
    BOOTSTRAP_ORG_INDEX,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry, ChannelGroupRegistry
from ums_smart_revenue.org.channel_registry import (
    ChannelRegistry,
    ChannelRegistryEntry,
    bootstrap_channel_registry,
)


class StaleUpdateRegistry:
    def list_channels(self) -> list[ChannelRegistryEntry]:
        return []

    def get_channel(self, youtube_channel_id: str) -> ChannelRegistryEntry | None:
        return ChannelRegistryEntry(
            youtube_channel_id=youtube_channel_id,
            channel_name="TV A",
            primary_company_id=BOOTSTRAP_COMPANY_TV_ID,
            cms_status="UNKNOWN",
            revenue_required=True,
        )

    def create_channel(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        primary_company_id: str | None,
        cms_status: str,
        revenue_required: bool,
    ) -> ChannelRegistryEntry:
        raise NotImplementedError

    def update_mapping(
        self, *, youtube_channel_id: str, primary_company_id: str | None
    ) -> ChannelRegistryEntry:
        raise KeyError(youtube_channel_id)


class ScopedListRegistry:
    def list_channels(self) -> list[ChannelRegistryEntry]:
        raise AssertionError(
            "unscoped channel listing should not be used for scoped callers"
        )

    def list_channels_by_ids(
        self, youtube_channel_ids: set[str]
    ) -> list[ChannelRegistryEntry]:
        assert youtube_channel_ids == {"channel-tv-a"}
        return [
            ChannelRegistryEntry(
                youtube_channel_id="channel-tv-a",
                channel_name="TV A",
                primary_company_id=BOOTSTRAP_COMPANY_TV_ID,
                cms_status="UNKNOWN",
                revenue_required=True,
            )
        ]


def create_bootstrap_app():
    app = create_app()
    registry = bootstrap_channel_registry()
    app.dependency_overrides[current_channel_registry] = lambda: registry
    app.dependency_overrides[current_org_access_index] = lambda: BOOTSTRAP_ORG_INDEX
    return app


def auth_headers(
    role: str, scope_type: str, scope_id: str | None = None
) -> dict[str, str]:
    headers = {
        "x-user-id": "user-1",
        "x-user-email": "user@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def test_company_manager_lists_only_company_channels():
    app = create_bootstrap_app()
    client = TestClient(app)

    response = client.get(
        "/channels",
        headers=auth_headers("company_manager", "company", BOOTSTRAP_COMPANY_TV_ID),
    )

    assert response.status_code == 200
    assert [channel["youtube_channel_id"] for channel in response.json()] == [
        "channel-tv-a"
    ]


def test_company_channel_listing_uses_scoped_registry_query():
    app = create_bootstrap_app()
    app.dependency_overrides[current_channel_registry] = lambda: ScopedListRegistry()
    client = TestClient(app)

    response = client.get(
        "/channels",
        headers=auth_headers("company_manager", "company", BOOTSTRAP_COMPANY_TV_ID),
    )

    assert response.status_code == 200
    assert [channel["youtube_channel_id"] for channel in response.json()] == [
        "channel-tv-a"
    ]


def test_company_manager_reads_scoped_outside_cms_monitor():
    app = create_bootstrap_app()
    app.dependency_overrides[current_channel_registry] = lambda: ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id="channel-tv-a",
                channel_name="TV A",
                primary_company_id=BOOTSTRAP_COMPANY_TV_ID,
                cms_status="OUTSIDE_CMS",
                revenue_required=True,
                revenue_source_status="MISSING_REVENUE_SOURCE",
            ),
            ChannelRegistryEntry(
                youtube_channel_id="channel-news-a",
                channel_name="News A",
                primary_company_id=BOOTSTRAP_COMPANY_NEWS_ID,
                cms_status="OUTSIDE_CMS",
                revenue_required=True,
                revenue_source_status="OFFICIAL_MANUAL_IMPORT",
            ),
            ChannelRegistryEntry(
                youtube_channel_id="channel-tv-inside",
                channel_name="TV Inside",
                primary_company_id=BOOTSTRAP_COMPANY_TV_ID,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                revenue_source_status="OFFICIAL_CMS_REVENUE",
            ),
        ]
    )
    client = TestClient(app)

    response = client.get(
        "/channels/outside-cms",
        headers=auth_headers("company_manager", "company", BOOTSTRAP_COMPANY_TV_ID),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {
        "outside_cms_channel_count": 1,
        "revenue_required_count": 1,
        "missing_official_revenue_count": 1,
    }
    assert [item["youtube_channel_id"] for item in payload["items"]] == ["channel-tv-a"]
    assert payload["items"][0]["revenue_source_status"] == "MISSING_REVENUE_SOURCE"
    assert payload["items"][0]["missing_official_revenue"] is True
    assert payload["items"][0]["recommended_action"] == (
        "Link channel to CMS or import official manual revenue."
    )


def test_global_outside_cms_monitor_keeps_official_manual_import_visible():
    app = create_bootstrap_app()
    app.dependency_overrides[current_channel_registry] = lambda: ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id="channel-tv-a",
                channel_name="TV A",
                primary_company_id=BOOTSTRAP_COMPANY_TV_ID,
                cms_status="OUTSIDE_CMS",
                revenue_required=True,
                revenue_source_status="MISSING_REVENUE_SOURCE",
            ),
            ChannelRegistryEntry(
                youtube_channel_id="channel-news-a",
                channel_name="News A",
                primary_company_id=BOOTSTRAP_COMPANY_NEWS_ID,
                cms_status="OUTSIDE_CMS",
                revenue_required=True,
                revenue_source_status="OFFICIAL_MANUAL_IMPORT",
            ),
        ]
    )
    client = TestClient(app)

    response = client.get(
        "/channels/outside-cms",
        headers=auth_headers("super_owner", "global"),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {
        "outside_cms_channel_count": 2,
        "revenue_required_count": 2,
        "missing_official_revenue_count": 1,
    }
    manual_import_item = next(
        item
        for item in payload["items"]
        if item["youtube_channel_id"] == "channel-news-a"
    )
    assert manual_import_item["missing_official_revenue"] is False
    assert manual_import_item["recommended_action"] == (
        "Keep manual official revenue import current; CMS linking remains recommended."
    )


def test_company_manager_reads_scoped_channel_issues_without_cross_company_leak():
    app = create_bootstrap_app()
    app.dependency_overrides[current_channel_registry] = lambda: ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id="channel-tv-a",
                channel_name="TV A",
                primary_company_id=BOOTSTRAP_COMPANY_TV_ID,
                cms_status="OUTSIDE_CMS",
                revenue_required=True,
            ),
            ChannelRegistryEntry(
                youtube_channel_id="channel-news-a",
                channel_name="News A",
                primary_company_id=BOOTSTRAP_COMPANY_NEWS_ID,
                cms_status="OUTSIDE_CMS",
                revenue_required=True,
            ),
        ]
    )
    app.dependency_overrides[current_group_registry] = lambda: ChannelGroupRegistry()
    client = TestClient(app)

    response = client.get(
        "/channels/issues",
        headers=auth_headers("company_manager", "company", BOOTSTRAP_COMPANY_TV_ID),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {
        "total_issue_count": 2,
        "channel_count": 1,
        "issue_type_counts": {
            "OUTSIDE_CMS_REVENUE_REQUIRED": 1,
            "REVENUE_REQUIRED_NO_GROUP": 1,
        },
    }
    assert {item["youtube_channel_id"] for item in payload["items"]} == {"channel-tv-a"}
    assert {item["issue_type"] for item in payload["items"]} == {
        "OUTSIDE_CMS_REVENUE_REQUIRED",
        "REVENUE_REQUIRED_NO_GROUP",
    }


def test_global_channel_issues_include_registry_health_summary():
    missing_sector_company_id = "00000000-0000-0000-0000-000000009999"
    app = create_bootstrap_app()
    app.dependency_overrides[current_channel_registry] = lambda: ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id="channel-tv-a",
                channel_name="TV A",
                primary_company_id=BOOTSTRAP_COMPANY_TV_ID,
                cms_status="OUTSIDE_CMS",
                revenue_required=True,
            ),
            ChannelRegistryEntry(
                youtube_channel_id="channel-news-a",
                channel_name="News A",
                primary_company_id=BOOTSTRAP_COMPANY_NEWS_ID,
                cms_status="INSIDE_CMS",
                revenue_required=True,
            ),
            ChannelRegistryEntry(
                youtube_channel_id="channel-orphan",
                channel_name="Orphan",
                primary_company_id=None,
                cms_status="UNKNOWN",
                revenue_required=False,
            ),
            ChannelRegistryEntry(
                youtube_channel_id="channel-missing-sector",
                channel_name="Missing Sector",
                primary_company_id=missing_sector_company_id,
                cms_status="UNKNOWN",
                revenue_required=False,
            ),
        ]
    )
    app.dependency_overrides[current_group_registry] = lambda: ChannelGroupRegistry(
        [
            ChannelGroupEntry(
                id="group-news",
                name="News Group",
                group_type="NEWS_BRAND",
                active=True,
                channel_ids=("channel-news-a",),
            )
        ]
    )
    client = TestClient(app)

    response = client.get(
        "/channels/issues",
        headers=auth_headers("super_owner", "global"),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"] == {
        "total_issue_count": 4,
        "channel_count": 3,
        "issue_type_counts": {
            "MISSING_COMPANY": 1,
            "MISSING_SECTOR": 1,
            "OUTSIDE_CMS_REVENUE_REQUIRED": 1,
            "REVENUE_REQUIRED_NO_GROUP": 1,
        },
    }
    issue_types_by_channel = {
        item["youtube_channel_id"]: item["issue_type"] for item in payload["items"]
    }
    assert issue_types_by_channel["channel-orphan"] == "MISSING_COMPANY"
    assert issue_types_by_channel["channel-missing-sector"] == "MISSING_SECTOR"
    assert "channel-news-a" not in issue_types_by_channel


def test_assistant_cannot_create_channel():
    client = TestClient(create_bootstrap_app())

    response = client.post(
        "/channels",
        headers=auth_headers("assistant_analyst", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={
            "youtube_channel_id": "channel-new",
            "channel_name": "New Channel",
            "primary_company_id": BOOTSTRAP_COMPANY_TV_ID,
            "cms_status": "UNKNOWN",
            "revenue_required": True,
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: registry.manage_channels"


def test_data_steward_can_create_channel_inside_assigned_company():
    client = TestClient(create_bootstrap_app())

    response = client.post(
        "/channels",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={
            "youtube_channel_id": "channel-new",
            "channel_name": "New Channel",
            "primary_company_id": BOOTSTRAP_COMPANY_TV_ID,
            "cms_status": "UNKNOWN",
            "revenue_required": True,
        },
    )

    assert response.status_code == 201
    assert response.json()["youtube_channel_id"] == "channel-new"
    assert response.json()["primary_company_id"] == BOOTSTRAP_COMPANY_TV_ID


def test_channel_requests_reject_blank_strings():
    client = TestClient(create_bootstrap_app())

    create_response = client.post(
        "/channels",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={
            "youtube_channel_id": "   ",
            "channel_name": "New Channel",
            "primary_company_id": BOOTSTRAP_COMPANY_TV_ID,
            "cms_status": "UNKNOWN",
            "revenue_required": True,
        },
    )
    mapping_response = client.patch(
        "/channels/channel-tv-a/mapping",
        headers=auth_headers("corporate_admin", "global"),
        json={"primary_company_id": BOOTSTRAP_COMPANY_NEWS_ID, "reason": "   "},
    )

    assert create_response.status_code == 422
    assert mapping_response.status_code == 422


def test_data_steward_cannot_create_channel_in_other_company():
    client = TestClient(create_bootstrap_app())

    response = client.post(
        "/channels",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={
            "youtube_channel_id": "channel-news-new",
            "channel_name": "News New Channel",
            "primary_company_id": BOOTSTRAP_COMPANY_NEWS_ID,
            "cms_status": "UNKNOWN",
            "revenue_required": True,
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: registry.manage_channels"


def test_mapping_change_requires_reason_and_permission():
    client = TestClient(create_bootstrap_app())

    missing_reason = client.patch(
        "/channels/channel-tv-a/mapping",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={"primary_company_id": BOOTSTRAP_COMPANY_TV_ID},
    )
    denied = client.patch(
        "/channels/channel-news-a/mapping",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={
            "primary_company_id": BOOTSTRAP_COMPANY_TV_ID,
            "reason": "Fix wrong owner",
        },
    )

    assert missing_reason.status_code == 422
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Missing permission: registry.manage_org_mapping"


def test_mapping_change_authorizes_before_not_found():
    client = TestClient(create_bootstrap_app())

    response = client.patch(
        "/channels/missing-channel/mapping",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={
            "primary_company_id": BOOTSTRAP_COMPANY_TV_ID,
            "reason": "Attempt unauthorized lookup",
        },
    )

    assert response.status_code == 403
    assert (
        response.json()["detail"] == "Missing permission: registry.manage_org_mapping"
    )


def test_mapping_change_is_audited_for_corporate_admin():
    client = TestClient(create_bootstrap_app())

    response = client.patch(
        "/channels/channel-tv-a/mapping",
        headers=auth_headers("corporate_admin", "global"),
        json={
            "primary_company_id": BOOTSTRAP_COMPANY_NEWS_ID,
            "reason": "Corporate remap after ownership transfer",
        },
    )

    assert response.status_code == 200
    assert response.json()["primary_company_id"] == BOOTSTRAP_COMPANY_NEWS_ID
    assert response.json()["audit_event"]["event_type"] == "CHANNEL_UPDATED"
    assert (
        response.json()["audit_event"]["reason"]
        == "Corporate remap after ownership transfer"
    )


def test_registry_factory_returns_fresh_state_per_app():
    registry_one = bootstrap_channel_registry()
    registry_two = bootstrap_channel_registry()

    registry_one.create_channel(
        youtube_channel_id="channel-temp",
        channel_name="Temp",
        primary_company_id=BOOTSTRAP_COMPANY_TV_ID,
        cms_status="UNKNOWN",
        revenue_required=True,
    )

    assert registry_two.get_channel("channel-temp") is None


def test_mapping_change_preserves_404_if_channel_disappears_before_update():
    app = create_bootstrap_app()
    app.dependency_overrides[current_channel_registry] = lambda: StaleUpdateRegistry()
    client = TestClient(app)

    response = client.patch(
        "/channels/channel-tv-a/mapping",
        headers=auth_headers("corporate_admin", "global"),
        json={
            "primary_company_id": BOOTSTRAP_COMPANY_NEWS_ID,
            "reason": "Concurrent registry cleanup",
        },
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Channel not found"
