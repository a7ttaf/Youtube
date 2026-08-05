"""YouTube groups client tests (CMS group sync fetch client)."""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from ums_smart_revenue.connectors.google import youtube_groups_client as groups_module
from ums_smart_revenue.connectors.google.errors import GoogleApiResponseError
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient
from ums_smart_revenue.connectors.google.youtube_groups_client import (
    CmsGroup,
    CmsGroupMembers,
    YouTubeGroupsClient,
)


def _next_json_response(pages: Iterator[dict[str, object]]) -> httpx.Response:
    try:
        payload = next(pages)
    except StopIteration as exc:
        raise AssertionError("unexpected extra HTTP request") from exc
    return httpx.Response(200, content=json.dumps(payload).encode())


def _group_item(group_id: str, title: str, *, item_type: str = "youtube#channel") -> dict:
    """Build one raw groups.list item, defaulting to a channel-type group."""
    return {
        "id": group_id,
        "snippet": {"title": title},
        "contentDetails": {"itemType": item_type},
    }


def test_list_groups_single_page(mock_credentials) -> None:
    payload = {"items": [_group_item("g1", "TV")]}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)
    groups = client.list_groups(account_id="content-owner-1")
    assert groups == [CmsGroup("g1", "TV")]


def test_list_groups_paginates_and_carries_token(mock_credentials) -> None:
    captured: list[dict[str, str]] = []
    pages = iter(
        [
            {
                "items": [_group_item("g1", "TV")],
                "nextPageToken": "tok-2",
            },
            {"items": [_group_item("g2", "Movies")]},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return _next_json_response(pages)

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)
    groups = client.list_groups(account_id="acct")
    assert groups == [CmsGroup("g1", "TV"), CmsGroup("g2", "Movies")]
    assert "pageToken" not in captured[0]
    assert captured[1]["pageToken"] == "tok-2"


def test_list_groups_endless_next_page_token_raises_cap_error(
    mock_credentials, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(groups_module, "_MAX_PAGES", 3)

    call_count = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        payload = {
            "items": [_group_item(f"g{call_count}", "T")],
            "nextPageToken": f"tok-{call_count + 1}",
        }
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)

    with pytest.raises(GoogleApiResponseError, match="3"):
        client.list_groups(account_id="acct")
    assert call_count == 3


def test_list_groups_rejects_item_missing_title(mock_credentials) -> None:
    payload = {"items": [{"id": "g1", "snippet": {}}]}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)

    with pytest.raises(GoogleApiResponseError):
        client.list_groups(account_id="acct")


def test_list_groups_rejects_item_missing_id(mock_credentials) -> None:
    payload = {"items": [{"snippet": {"title": "TV"}}]}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)

    with pytest.raises(GoogleApiResponseError):
        client.list_groups(account_id="acct")


def test_list_groups_rejects_item_missing_content_details(mock_credentials) -> None:
    payload = {"items": [{"id": "g1", "snippet": {"title": "TV"}}]}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)

    with pytest.raises(GoogleApiResponseError):
        client.list_groups(account_id="acct")


def test_list_groups_rejects_item_missing_item_type(mock_credentials) -> None:
    payload = {"items": [{"id": "g1", "snippet": {"title": "TV"}, "contentDetails": {}}]}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)

    with pytest.raises(GoogleApiResponseError):
        client.list_groups(account_id="acct")


def test_list_groups_skips_non_channel_item_types(mock_credentials) -> None:
    """A group's itemType is homogeneous (channel/playlist/video/asset).

    Only channel-type groups map onto channel_groups membership; the others
    would otherwise create a local group with zero channel members.
    """
    payload = {
        "items": [
            _group_item("g1", "TV"),
            _group_item("g2", "Asset Bundle", item_type="youtubePartner#asset"),
            _group_item("g3", "Highlights Playlist", item_type="youtube#playlist"),
            _group_item("g4", "Explainer Videos", item_type="youtube#video"),
        ]
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)
    groups = client.list_groups(account_id="acct")
    assert groups == [CmsGroup("g1", "TV")]


def test_list_groups_sends_mine_and_on_behalf_of_content_owner(mock_credentials) -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return httpx.Response(200, content=b'{"items": []}')

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)
    client.list_groups(account_id="content-owner-42")
    assert captured[0]["mine"] == "true"
    assert captured[0]["onBehalfOfContentOwner"] == "content-owner-42"


def test_list_group_items_splits_channel_and_non_channel_members(mock_credentials) -> None:
    payload = {
        "items": [
            {"resource": {"kind": "youtube#channel", "id": "UCa..."}},
            {"resource": {"kind": "youtube#channel", "id": "UCb..."}},
            {"resource": {"kind": "youtube#video", "id": "vid-1"}},
        ]
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)
    members = client.list_group_items(group_id="g1", account_id="acct")
    assert members == CmsGroupMembers(channel_ids=("UCa...", "UCb..."), non_channel_count=1)


def test_list_group_items_paginates_and_carries_token(mock_credentials) -> None:
    captured: list[dict[str, str]] = []
    pages = iter(
        [
            {
                "items": [{"resource": {"kind": "youtube#channel", "id": "UCa..."}}],
                "nextPageToken": "tok-2",
            },
            {"items": [{"resource": {"kind": "youtube#channel", "id": "UCb..."}}]},
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return _next_json_response(pages)

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)
    members = client.list_group_items(group_id="g1", account_id="acct")
    assert members.channel_ids == ("UCa...", "UCb...")
    assert captured[1]["pageToken"] == "tok-2"


def test_list_group_items_rejects_channel_item_missing_resource_id(mock_credentials) -> None:
    payload = {"items": [{"resource": {"kind": "youtube#channel"}}]}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)

    with pytest.raises(GoogleApiResponseError):
        client.list_group_items(group_id="g1", account_id="acct")


def test_list_group_items_rejects_item_missing_resource(mock_credentials) -> None:
    payload = {"items": [{"id": "not-a-member"}]}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)

    with pytest.raises(GoogleApiResponseError):
        client.list_group_items(group_id="g1", account_id="acct")


def test_list_group_items_rejects_item_missing_resource_kind(mock_credentials) -> None:
    payload = {"items": [{"resource": {"id": "UCa..."}}]}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=json.dumps(payload).encode())

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)

    with pytest.raises(GoogleApiResponseError):
        client.list_group_items(group_id="g1", account_id="acct")


def test_list_group_items_sends_group_id_and_on_behalf_of_content_owner(
    mock_credentials,
) -> None:
    captured: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        return httpx.Response(200, content=b'{"items": []}')

    http = GoogleHttpClient(
        credentials=mock_credentials,
        transport=httpx.MockTransport(handler),
    )
    client = YouTubeGroupsClient(http=http)
    client.list_group_items(group_id="group-7", account_id="content-owner-42")
    assert captured[0]["groupId"] == "group-7"
    assert captured[0]["onBehalfOfContentOwner"] == "content-owner-42"
