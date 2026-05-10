from fastapi.testclient import TestClient

from ums_smart_revenue.app import create_app
from ums_smart_revenue.org.channel_registry import bootstrap_channel_registry


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
    app = create_app()
    app.dependency_overrides.clear()
    client = TestClient(app)

    response = client.get("/channels", headers=auth_headers("company_manager", "company", "company-tv-a"))

    assert response.status_code == 200
    assert [channel["youtube_channel_id"] for channel in response.json()] == ["channel-tv-a"]


def test_assistant_cannot_create_channel():
    client = TestClient(create_app())

    response = client.post(
        "/channels",
        headers=auth_headers("assistant_analyst", "company", "company-tv-a"),
        json={
            "youtube_channel_id": "channel-new",
            "channel_name": "New Channel",
            "primary_company_id": "company-tv-a",
            "cms_status": "UNKNOWN",
            "revenue_required": True,
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: registry.manage_channels"


def test_data_steward_can_create_channel_inside_assigned_company():
    client = TestClient(create_app())

    response = client.post(
        "/channels",
        headers=auth_headers("data_steward", "company", "company-tv-a"),
        json={
            "youtube_channel_id": "channel-new",
            "channel_name": "New Channel",
            "primary_company_id": "company-tv-a",
            "cms_status": "UNKNOWN",
            "revenue_required": True,
        },
    )

    assert response.status_code == 201
    assert response.json()["youtube_channel_id"] == "channel-new"
    assert response.json()["primary_company_id"] == "company-tv-a"


def test_data_steward_cannot_create_channel_in_other_company():
    client = TestClient(create_app())

    response = client.post(
        "/channels",
        headers=auth_headers("data_steward", "company", "company-tv-a"),
        json={
            "youtube_channel_id": "channel-news-new",
            "channel_name": "News New Channel",
            "primary_company_id": "company-news-a",
            "cms_status": "UNKNOWN",
            "revenue_required": True,
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: registry.manage_channels"


def test_mapping_change_requires_reason_and_permission():
    client = TestClient(create_app())

    missing_reason = client.patch(
        "/channels/channel-tv-a/mapping",
        headers=auth_headers("data_steward", "company", "company-tv-a"),
        json={"primary_company_id": "company-tv-a"},
    )
    denied = client.patch(
        "/channels/channel-news-a/mapping",
        headers=auth_headers("data_steward", "company", "company-tv-a"),
        json={"primary_company_id": "company-tv-a", "reason": "Fix wrong owner"},
    )

    assert missing_reason.status_code == 422
    assert denied.status_code == 403
    assert denied.json()["detail"] == "Missing permission: registry.manage_org_mapping"


def test_mapping_change_is_audited_for_corporate_admin():
    client = TestClient(create_app())

    response = client.patch(
        "/channels/channel-tv-a/mapping",
        headers=auth_headers("corporate_admin", "global"),
        json={"primary_company_id": "company-news-a", "reason": "Corporate remap after ownership transfer"},
    )

    assert response.status_code == 200
    assert response.json()["primary_company_id"] == "company-news-a"
    assert response.json()["audit_event"]["event_type"] == "CHANNEL_UPDATED"
    assert response.json()["audit_event"]["reason"] == "Corporate remap after ownership transfer"


def test_registry_factory_returns_fresh_state_per_app():
    registry_one = bootstrap_channel_registry()
    registry_two = bootstrap_channel_registry()

    registry_one.create_channel(
        youtube_channel_id="channel-temp",
        channel_name="Temp",
        primary_company_id="company-tv-a",
        cms_status="UNKNOWN",
        revenue_required=True,
    )

    assert registry_two.get_channel("channel-temp") is None

