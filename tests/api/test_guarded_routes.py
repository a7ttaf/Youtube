from fastapi.testclient import TestClient

from ums_smart_revenue.app import create_app


def auth_headers(role: str, scope_type: str, scope_id: str | None = None) -> dict[str, str]:
    headers = {
        "x-user-id": "user-1",
        "x-user-email": "user@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def test_guarded_revenue_route_rejects_assistant_without_finance_access():
    client = TestClient(create_app())

    response = client.get(
        "/revenue/channels/channel-tv-a/authorization-check",
        headers=auth_headers("assistant_analyst", "company", "company-tv-a"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_guarded_revenue_route_allows_scoped_finance_viewer():
    client = TestClient(create_app())

    response = client.get(
        "/revenue/channels/channel-tv-a/authorization-check",
        headers=auth_headers("finance_viewer", "company", "company-tv-a"),
    )

    assert response.status_code == 200
    assert response.json() == {
        "authorized": True,
        "channel_id": "channel-tv-a",
        "permission": "finance.view_revenue",
    }


def test_guarded_revenue_route_rejects_finance_viewer_outside_scope():
    client = TestClient(create_app())

    response = client.get(
        "/revenue/channels/channel-news-a/authorization-check",
        headers=auth_headers("finance_viewer", "company", "company-tv-a"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_guarded_route_requires_auth_headers():
    client = TestClient(create_app())

    response = client.get("/revenue/channels/channel-tv-a/authorization-check")

    assert response.status_code == 401
    assert response.json()["detail"] == "Missing authentication headers"

