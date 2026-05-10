from fastapi.testclient import TestClient

from ums_smart_revenue.api.channels import current_channel_registry
from ums_smart_revenue.api.revenue import current_org_access_index
from ums_smart_revenue.app import create_app
from ums_smart_revenue.org.bootstrap_registry import (
    BOOTSTRAP_COMPANY_NEWS_ID,
    BOOTSTRAP_COMPANY_TV_ID,
    BOOTSTRAP_ORG_INDEX,
)
from ums_smart_revenue.org.channel_registry import ChannelRegistryEntry, bootstrap_channel_registry


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

    def update_mapping(self, *, youtube_channel_id: str, primary_company_id: str | None) -> ChannelRegistryEntry:
        raise KeyError(youtube_channel_id)


def create_bootstrap_app():
    app = create_app()
    registry = bootstrap_channel_registry()
    app.dependency_overrides[current_channel_registry] = lambda: registry
    app.dependency_overrides[current_org_access_index] = lambda: BOOTSTRAP_ORG_INDEX
    return app


def auth_headers(role: str, scope_type: str, scope_id: str | None = None) -> dict[str, str]:
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

    response = client.get("/channels", headers=auth_headers("company_manager", "company", BOOTSTRAP_COMPANY_TV_ID))

    assert response.status_code == 200
    assert [channel["youtube_channel_id"] for channel in response.json()] == ["channel-tv-a"]


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
        json={"primary_company_id": BOOTSTRAP_COMPANY_TV_ID, "reason": "Fix wrong owner"},
    )

    assert missing_reason.status_code == 422
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Missing permission: registry.manage_org_mapping"


def test_mapping_change_authorizes_before_not_found():
    client = TestClient(create_bootstrap_app())

    response = client.patch(
        "/channels/missing-channel/mapping",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={"primary_company_id": BOOTSTRAP_COMPANY_TV_ID, "reason": "Attempt unauthorized lookup"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: registry.manage_org_mapping"


def test_mapping_change_is_audited_for_corporate_admin():
    client = TestClient(create_bootstrap_app())

    response = client.patch(
        "/channels/channel-tv-a/mapping",
        headers=auth_headers("corporate_admin", "global"),
        json={"primary_company_id": BOOTSTRAP_COMPANY_NEWS_ID, "reason": "Corporate remap after ownership transfer"},
    )

    assert response.status_code == 200
    assert response.json()["primary_company_id"] == BOOTSTRAP_COMPANY_NEWS_ID
    assert response.json()["audit_event"]["event_type"] == "CHANNEL_UPDATED"
    assert response.json()["audit_event"]["reason"] == "Corporate remap after ownership transfer"


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
        json={"primary_company_id": BOOTSTRAP_COMPANY_NEWS_ID, "reason": "Concurrent registry cleanup"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Channel not found"

