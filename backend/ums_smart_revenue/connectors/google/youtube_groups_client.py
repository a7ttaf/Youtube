# ============================================================================
# Purpose: Read-only client for the YouTube Analytics v2 groups / groupItems
#   endpoints, fetching a content owner's CMS groups and each group's channel
#   membership for CMS group sync.
# Database/ORM: None (read-only API calls; no persistence here).
# Standards: Same GoogleHttpClient.request() idiom and fail-closed shape
#   validation as youtube_reporting_client.py; pagination via nextPageToken
#   with an explicit _MAX_PAGES cap so a misbehaving/looping API response
#   cannot hang the caller forever.
# Blast Radius: None by itself — this module only fetches and normalizes
#   Google's group/member payloads into typed dataclasses. No write, no
#   authorization, no finance calculation.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> future CMS group
#     sync route will call this client to fetch group snapshots.
#   - File: backend/ums_smart_revenue/org/channel_group_sync.py -> future
#     planner will diff the fetched groups/members against local state.
#   - File: backend/ums_smart_revenue/connectors/google/http_client.py ->
#     GoogleHttpClient.request() handles Bearer auth, retry, and JSON decode.
# ============================================================================
"""YouTube Analytics groups / groupItems client (CMS group sync fetch client).

Endpoints used (Bearer auth via GoogleHttpClient):
- GET /v2/groups     -> list a content owner's CMS groups
- GET /v2/groupItems -> list the members of one CMS group

Both endpoints are paginated via nextPageToken; group members are split into
channel ids (kind == "youtube#channel") and a count of everything else.
"""

from __future__ import annotations

from dataclasses import dataclass

from ums_smart_revenue.connectors.google.errors import GoogleApiResponseError
from ums_smart_revenue.connectors.google.http_client import GoogleHttpClient

_BASE = "https://youtubeanalytics.googleapis.com/v2"
_MAX_PAGES = 500


@dataclass(frozen=True)
class CmsGroup:
    """One CMS group as YouTube reports it."""

    cms_group_id: str
    title: str


@dataclass(frozen=True)
class CmsGroupMembers:
    """Channel members of one CMS group, with the non-channel count."""

    channel_ids: tuple[str, ...]
    non_channel_count: int


def _non_empty_str(value: object, *, url: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GoogleApiResponseError(url=url, reason=f"{field!r} must be a non-empty string")
    return value


def _response_object_list(
    body: dict[str, object], field: str, *, url: str
) -> list[dict[str, object]]:
    if field not in body:
        return []
    value = body[field]
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise GoogleApiResponseError(url=url, reason=f"{field!r} must be a list of objects")
    return value


def _next_page_token(body: dict[str, object], *, url: str) -> str | None:
    if "nextPageToken" not in body:
        return None
    value = body["nextPageToken"]
    if value == "":
        return None
    if not isinstance(value, str) or not value.strip():
        raise GoogleApiResponseError(url=url, reason="'nextPageToken' must be a non-empty string")
    return value


def _parse_group(item: dict[str, object], *, url: str) -> CmsGroup:
    group_id = _non_empty_str(item.get("id"), url=url, field="id")
    snippet = item.get("snippet")
    if not isinstance(snippet, dict):
        raise GoogleApiResponseError(url=url, reason="'snippet' must be an object")
    title = _non_empty_str(snippet.get("title"), url=url, field="snippet.title")
    return CmsGroup(cms_group_id=group_id, title=title)


def _parse_group_item(item: dict[str, object], *, url: str, channel_ids: list[str]) -> int:
    """Classify one groupItems entry, appending channel ids in place.

    Returns 1 if the item is a non-channel member, else 0.
    """
    resource = item.get("resource")
    if not isinstance(resource, dict):
        raise GoogleApiResponseError(url=url, reason="'resource' must be an object")
    kind = _non_empty_str(resource.get("kind"), url=url, field="resource.kind")
    if kind != "youtube#channel":
        return 1
    channel_ids.append(_non_empty_str(resource.get("id"), url=url, field="resource.id"))
    return 0


class YouTubeGroupsClient:
    """Thin wrapper over groups.list / groupItems.list."""

    def __init__(self, *, http: GoogleHttpClient) -> None:
        """Bind the shared HTTP client (auth + retry + JSON decode)."""
        self._http = http

    # ========================================================================
    # Purpose: List the CMS groups a content owner has, paginated via
    #   nextPageToken with a hard page cap so an API bug or infinite token
    #   loop cannot hang the caller.
    # Database/ORM: None (read-only API call).
    # Standards: Fail-closed shape validation on every item; typed
    #   GoogleApiResponseError on malformed 'items'/'snippet.title'/pagination
    #   token, or on exceeding _MAX_PAGES.
    # Blast Radius: Fetch-only. No write, no persistence, no authorization.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/google/http_client.py ->
    #     typed retry/error/response-validation pipeline.
    # ========================================================================
    def list_groups(self, *, account_id: str) -> list[CmsGroup]:
        """Fetch every CMS group visible to the given content owner."""
        url = f"{_BASE}/groups"
        token: str | None = None
        out: list[CmsGroup] = []
        for _ in range(_MAX_PAGES):
            params: dict[str, str] = {
                "mine": "true",
                "onBehalfOfContentOwner": account_id,
            }
            if token:
                params["pageToken"] = token
            body = self._http.request(method="GET", url=url, params=params)
            for item in _response_object_list(body, "items", url=url):
                out.append(_parse_group(item, url=url))
            token = _next_page_token(body, url=url)
            if not token:
                return out
        raise GoogleApiResponseError(url=url, reason=f"pagination exceeded {_MAX_PAGES} pages")

    # ========================================================================
    # Purpose: List the channel members of one CMS group, paginated via
    #   nextPageToken with the same hard page cap as list_groups. Non-channel
    #   members (e.g. videos) are counted but not returned by id.
    # Database/ORM: None (read-only API call).
    # Standards: Fail-closed shape validation on every item; typed
    #   GoogleApiResponseError on malformed 'items'/'resource'/pagination
    #   token, or on exceeding _MAX_PAGES.
    # Blast Radius: Fetch-only. No write, no persistence, no authorization.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/google/http_client.py ->
    #     typed retry/error/response-validation pipeline.
    # ========================================================================
    def list_group_items(self, *, group_id: str, account_id: str) -> CmsGroupMembers:
        """Fetch the channel membership (and non-channel count) of one group."""
        url = f"{_BASE}/groupItems"
        token: str | None = None
        channel_ids: list[str] = []
        non_channel_count = 0
        for _ in range(_MAX_PAGES):
            params: dict[str, str] = {
                "groupId": group_id,
                "onBehalfOfContentOwner": account_id,
            }
            if token:
                params["pageToken"] = token
            body = self._http.request(method="GET", url=url, params=params)
            for item in _response_object_list(body, "items", url=url):
                non_channel_count += _parse_group_item(item, url=url, channel_ids=channel_ids)
            token = _next_page_token(body, url=url)
            if not token:
                return CmsGroupMembers(
                    channel_ids=tuple(channel_ids), non_channel_count=non_channel_count
                )
        raise GoogleApiResponseError(url=url, reason=f"pagination exceeded {_MAX_PAGES} pages")
