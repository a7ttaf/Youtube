"""API tests for the CMS group sync route (POST /channels/groups/sync).

The Google fetch is substituted through the route's groups-client factory
dependency and credential resolution is patched at the route module, so no
test in this file touches the network or a real credential row.
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from ums_smart_revenue.api.channels import (
    current_audit_sink,
    current_channel_registry,
    current_groups_client_factory,
)
from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.api.dependencies_finance import current_org_access_index
from ums_smart_revenue.api.registry_dependencies import sql_group_registry_from_session
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import AuditRecord, InMemoryAuditSink
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.connectors.google.errors import (
    CredentialNotFoundError,
    GoogleApiResponseError,
    InactiveCredentialError,
    OAuthRefreshError,
    SecretFetchError,
)
from ums_smart_revenue.connectors.google.youtube_groups_client import (
    CmsGroup,
    CmsGroupMembers,
)
from ums_smart_revenue.org.bootstrap_registry import BOOTSTRAP_ORG_INDEX
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry, ChannelGroupRegistry
from ums_smart_revenue.org.channel_registry import ChannelRegistry, ChannelRegistryEntry

CHANNEL_ONE = "UCB6sc84dcg6VQGB_d89sx2g"
CHANNEL_TWO = "UCq-Fj5jknLsUf-MWSy4_brA"
UNKNOWN_CHANNEL = "UCzzzzzzzzzzzzzzzzzzzzzz"
CONTENT_OWNER = "TestOwnerAAAAAAAAAAAAA"
SYNC_URL = "/channels/groups/sync"

# Sentinel meaning "leave this key out of the JSON body entirely" so a test can
# prove Pydantic rejects a missing required field rather than a null one.
_OMIT = object()


class FakeGroupsClient:
    """Canned stand-in for YouTubeGroupsClient; never performs any I/O."""

    def __init__(
        self,
        snapshot: list[tuple[str, str, tuple[str, ...], int]],
        *,
        groups_error: Exception | None = None,
        items_error_for: str | None = None,
    ) -> None:
        """Hold the canned snapshot plus optional failures to raise."""
        self._snapshot = snapshot
        self._groups_error = groups_error
        self._items_error_for = items_error_for
        self.item_calls: list[str] = []

    def list_groups(self, *, account_id: str) -> list[CmsGroup]:
        """Return the canned group list, or raise the configured failure."""
        if self._groups_error is not None:
            raise self._groups_error
        assert account_id == CONTENT_OWNER
        return [CmsGroup(cms_group_id=key, title=title) for key, title, _ids, _n in self._snapshot]

    def list_group_items(self, *, group_id: str, account_id: str) -> CmsGroupMembers:
        """Return the canned members of one group, or raise for the marked id."""
        assert account_id == CONTENT_OWNER
        self.item_calls.append(group_id)
        if self._items_error_for == group_id:
            raise GoogleApiResponseError(
                url="https://youtubeanalytics.googleapis.com/v2/groupItems",
                reason="leaky-items-reason",
            )
        for key, _title, channel_ids, non_channel in self._snapshot:
            if key == group_id:
                return CmsGroupMembers(channel_ids=channel_ids, non_channel_count=non_channel)
        raise AssertionError(f"unexpected group_id: {group_id}")

    def close(self) -> None:
        """No-op: this double never opens a real HTTP client."""


@pytest.fixture(autouse=True)
def fake_credentials():
    """Patch credential resolution at the route module for every test."""
    with patch(
        "ums_smart_revenue.api.channels.resolve_connector_credentials",
        return_value=MagicMock(name="credentials"),
    ) as resolver:
        yield resolver


def auth_headers(role: str, scope_type: str, scope_id: str | None = None) -> dict[str, str]:
    """Build request headers for a principal with one role assignment."""
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


def known_channel_registry() -> ChannelRegistry:
    """Registry holding the two channels the CMS snapshots reference."""
    return ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=channel_id,
                channel_name=name,
                primary_company_id="company-tv",
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
            )
            for channel_id, name in ((CHANNEL_ONE, "Alpha News"), (CHANNEL_TWO, "Beta Sports"))
        ]
    )


def create_sync_app(
    client_double: FakeGroupsClient,
    *,
    registry: ChannelRegistry | None = None,
    groups: ChannelGroupRegistry | None = None,
) -> tuple[TestClient, ChannelRegistry, ChannelGroupRegistry, InMemoryAuditSink]:
    """Build a sync-ready client with the fetch, stores, and sink substituted."""
    app = create_app()
    registry = registry if registry is not None else known_channel_registry()
    groups = groups if groups is not None else ChannelGroupRegistry()
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_channel_registry] = lambda: registry
    app.dependency_overrides[sql_group_registry_from_session] = lambda: groups
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    app.dependency_overrides[current_org_access_index] = lambda: BOOTSTRAP_ORG_INDEX
    # The route only needs a session object to hand to the (patched) credential
    # resolver, so a sentinel keeps these tests off any database.
    app.dependency_overrides[current_db_session] = lambda: MagicMock(name="session")
    app.dependency_overrides[current_groups_client_factory] = lambda: (
        lambda _credentials: client_double
    )
    return TestClient(app), registry, groups, audit_sink


def post_sync(client: TestClient, *, headers: dict[str, str] | None = None, **overrides):
    """POST a sync request as a global super owner unless overridden."""
    body: dict[str, object] = {
        "content_owner_id": CONTENT_OWNER,
        "dry_run": False,
        "reason": "Mirror CMS grouping",
    }
    body.update(overrides)
    return client.post(
        SYNC_URL,
        headers=headers or auth_headers("super_owner", "global"),
        json={key: value for key, value in body.items() if value is not _OMIT},
    )


def records_of(audit_sink: InMemoryAuditSink, event_type: str) -> list[AuditRecord]:
    """Return every audit record of one event type."""
    return [record for record in audit_sink.records if record.event_type == event_type]


def sole_record(audit_sink: InMemoryAuditSink, event_type: str) -> AuditRecord:
    """Return the exactly-one audit record of this type, failing loudly otherwise."""
    matches = records_of(audit_sink, event_type)
    assert len(matches) == 1, f"expected exactly one {event_type}, got {len(matches)}"
    return matches[0]


def group_by_cms_id(groups: ChannelGroupRegistry, cms_group_id: str) -> ChannelGroupEntry:
    """Return the stored group carrying this CMS key, failing loudly otherwise."""
    match = groups.get_group_by_cms_id(cms_group_id)
    assert match is not None, f"no local group for {cms_group_id}"
    return match


def test_sync_requires_global_manage_groups():
    """A MANAGE_CHANNELS-only principal cannot mirror CMS groups."""
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, groups, audit_sink = create_sync_app(fake)
    channels_only = UserPrincipal(
        user_id="user-1",
        email="user@example.com",
        direct_permissions=(
            PermissionGrant(
                permission=Permission.MANAGE_CHANNELS,
                scope=AccessScope.global_scope(),
            ),
        ),
    )
    client.app.dependency_overrides[current_principal_from_headers] = lambda: channels_only

    response = post_sync(client)

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: registry.manage_groups"
    assert groups.list_synced_groups() == []
    assert audit_sink.records == []
    # Authorization fails closed BEFORE the fetch: no Google call was made.
    assert fake.item_calls == []


def test_blank_reason_is_rejected():
    """A whitespace-only reason is rejected; GROUPS_SYNCED requires a reason."""
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, groups, audit_sink = create_sync_app(fake)

    response = post_sync(client, reason="   ")

    assert response.status_code == 422
    assert response.json()["detail"] == "reason is required"
    assert groups.list_synced_groups() == []
    assert audit_sink.records == []


def test_blank_content_owner_id_is_rejected():
    """A whitespace-only content_owner_id is rejected before any fetch."""
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, _groups, _sink = create_sync_app(fake)

    response = post_sync(client, content_owner_id="  ")

    assert response.status_code == 422
    assert response.json()["detail"] == "content_owner_id is required"
    assert fake.item_calls == []


def test_missing_dry_run_is_rejected():
    """dry_run is mandatory: omitting it is a validation error, not an apply."""
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, groups, audit_sink = create_sync_app(fake)

    response = post_sync(client, dry_run=_OMIT)

    assert response.status_code == 422
    assert groups.list_synced_groups() == []
    assert audit_sink.records == []


def test_dry_run_returns_the_plan_and_writes_nothing():
    """A dry run previews the mirror without writing groups or audit rows."""
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE, UNKNOWN_CHANNEL), 2)])
    client, _registry, groups, audit_sink = create_sync_app(fake)

    response = post_sync(client, dry_run=True)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dry_run"] is True
    assert payload["content_owner_id"] == CONTENT_OWNER
    assert payload["counts"]["CREATE"] == 1
    assert payload["unknown_channel_total"] == 1
    assert payload["non_channel_member_count"] == 2
    entry = payload["groups"][0]
    assert entry["cms_group_id"] == "cms-a"
    assert entry["outcome"] == "CREATE"
    assert entry["title"] == "News"
    assert entry["members_added"] == [CHANNEL_ONE]
    assert entry["unknown_channel_ids"] == [UNKNOWN_CHANNEL]
    assert groups.list_synced_groups() == []
    assert audit_sink.records == []


def test_apply_creates_the_group_with_members_and_audits():
    """Applying a CREATE lands the group, its members, and both audit events."""
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, groups, audit_sink = create_sync_app(fake)

    response = post_sync(client)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dry_run"] is False
    assert payload["counts"]["CREATE"] == 1
    created = group_by_cms_id(groups, "cms-a")
    assert created.name == "News"
    assert created.active is True
    assert created.channel_ids == (CHANNEL_ONE,)
    group_updated = sole_record(audit_sink, "GROUP_UPDATED")
    assert group_updated.entity_id == created.id
    assert group_updated.details["outcome"] == "CREATE"
    summary = sole_record(audit_sink, "GROUPS_SYNCED")
    assert summary.entity_type == "channel_group_sync"
    assert summary.entity_id == CONTENT_OWNER
    assert summary.reason == "Mirror CMS grouping"
    assert summary.details["content_owner_id"] == CONTENT_OWNER
    assert summary.details["counts"]["CREATE"] == 1
    assert summary.details["counts"]["UNCHANGED"] == 0


def test_summary_counts_come_from_the_apply_not_the_plan():
    """The GROUPS_SYNCED summary reports executed counts, never plan counts."""
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, _groups, audit_sink = create_sync_app(fake)
    executed = {"CREATE": 0, "RENAME": 3, "UNCHANGED": 0}

    with patch(
        "ums_smart_revenue.api.channels.apply_group_sync",
        return_value=executed,
    ):
        response = post_sync(client)

    assert response.status_code == 200, response.text
    # The response echoes the PLAN (one CREATE) ...
    assert response.json()["counts"]["CREATE"] == 1
    # ... while the durable summary carries what the write boundary reported.
    assert sole_record(audit_sink, "GROUPS_SYNCED").details["counts"] == executed


def test_full_mirror_round_renames_churns_members_and_deactivates():
    """One sync renames, reconciles membership both ways, and deactivates."""
    seeded = ChannelGroupRegistry(
        [
            ChannelGroupEntry(
                id="grp-a",
                name="Old News",
                group_type="SECTOR",
                active=True,
                channel_ids=(CHANNEL_ONE,),
                cms_group_id="cms-a",
                content_owner_id=CONTENT_OWNER,
            ),
            ChannelGroupEntry(
                id="grp-b",
                name="Retired Bundle",
                group_type="SECTOR",
                active=True,
                channel_ids=(CHANNEL_TWO,),
                cms_group_id="cms-b",
                content_owner_id=CONTENT_OWNER,
            ),
        ]
    )
    fake = FakeGroupsClient([("cms-a", "News HD", (CHANNEL_TWO,), 0)])
    client, _registry, groups, audit_sink = create_sync_app(fake, groups=seeded)

    response = post_sync(client)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["counts"]["RENAME"] == 1
    assert payload["counts"]["DEACTIVATE"] == 1
    renamed = group_by_cms_id(groups, "cms-a")
    assert renamed.name == "News HD"
    assert renamed.channel_ids == (CHANNEL_TWO,)
    deactivated = group_by_cms_id(groups, "cms-b")
    assert deactivated.active is False
    assert deactivated.name == "Retired Bundle"
    assert {record.entity_id for record in records_of(audit_sink, "GROUP_UPDATED")} == {
        "grp-a",
        "grp-b",
    }
    assert sole_record(audit_sink, "GROUPS_SYNCED").details["counts"]["RENAME"] == 1


def test_resyncing_a_mirrored_content_owner_is_all_unchanged():
    """Re-running the same snapshot is a no-op with no further group audit."""
    seeded = ChannelGroupRegistry(
        [
            ChannelGroupEntry(
                id="grp-a",
                name="Old News",
                group_type="SECTOR",
                active=True,
                channel_ids=(CHANNEL_ONE,),
                cms_group_id="cms-a",
                content_owner_id=CONTENT_OWNER,
            ),
            ChannelGroupEntry(
                id="grp-b",
                name="Retired Bundle",
                group_type="SECTOR",
                active=True,
                channel_ids=(CHANNEL_TWO,),
                cms_group_id="cms-b",
                content_owner_id=CONTENT_OWNER,
            ),
        ]
    )
    fake = FakeGroupsClient([("cms-a", "News HD", (CHANNEL_TWO,), 0)])
    client, _registry, _groups, audit_sink = create_sync_app(fake, groups=seeded)

    first = post_sync(client)
    audit_sink.records.clear()
    second = post_sync(client)

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    counts = second.json()["counts"]
    assert counts["UNCHANGED"] == 2
    assert counts["RENAME"] == 0
    assert counts["DEACTIVATE"] == 0
    assert counts["MEMBERS_CHANGED"] == 0
    assert records_of(audit_sink, "GROUP_UPDATED") == []
    assert sole_record(audit_sink, "GROUPS_SYNCED").details["counts"]["UNCHANGED"] == 2


def test_unknown_members_are_surfaced_and_never_created():
    """A CMS member missing from the registry is reported, not invented."""
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE, UNKNOWN_CHANNEL), 0)])
    client, registry, groups, _sink = create_sync_app(fake)

    response = post_sync(client)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["unknown_channel_total"] == 1
    entry = payload["groups"][0]
    assert entry["unknown_channel_ids"] == [UNKNOWN_CHANNEL]
    assert entry["unknown_channel_count"] == 1
    assert registry.get_channel(UNKNOWN_CHANNEL) is None
    assert group_by_cms_id(groups, "cms-a").channel_ids == (CHANNEL_ONE,)


def test_missing_credential_returns_503_with_canned_detail(fake_credentials):
    """A missing credential is a 503 whose detail leaks no lookup internals."""
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, groups, audit_sink = create_sync_app(fake)
    fake_credentials.side_effect = CredentialNotFoundError(
        connector_key="youtube-analytics", account_id="leaky-account-id"
    )

    response = post_sync(client)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "youtube-analytics credential" in detail
    assert "leaky-account-id" not in detail
    assert "no credential for" not in detail
    assert groups.list_synced_groups() == []
    assert audit_sink.records == []
    assert fake.item_calls == []


def test_inactive_credential_returns_503_with_canned_detail(fake_credentials):
    """An inactive credential is a 503 that never echoes the credential id."""
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, groups, _sink = create_sync_app(fake)
    fake_credentials.side_effect = InactiveCredentialError(
        credential_id="leaky-credential-uuid", status="revoked"
    )

    response = post_sync(client)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "leaky-credential-uuid" not in detail
    assert "revoked" not in detail
    assert groups.list_synced_groups() == []


def test_oauth_refresh_failure_returns_503_with_canned_detail(fake_credentials):
    """A token refresh failure is a 503 that never names the inner exception."""

    class LeakyTokenError(Exception):
        """Distinctive inner error whose class name must not reach the client."""

    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, groups, _sink = create_sync_app(fake)
    fake_credentials.side_effect = OAuthRefreshError(inner=LeakyTokenError("token revoked"))

    response = post_sync(client)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert "refresh failed" in detail
    assert "LeakyTokenError" not in detail
    assert "oauth refresh failed" not in detail
    assert groups.list_synced_groups() == []


def test_other_credential_layer_error_returns_503_with_canned_detail(fake_credentials):
    """A GoogleConnectorError subclass outside the three handled ones still fails closed.

    resolve_connector_credentials can raise other GoogleConnectorError subclasses
    (e.g. SecretFetchError from the secret backend); before this fix only
    CredentialNotFoundError/InactiveCredentialError/OAuthRefreshError were caught,
    so this escaped as an unhandled 500.
    """

    class LeakySecretBackendError(Exception):
        """Distinctive inner error whose class name must not reach the client."""

    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, groups, audit_sink = create_sync_app(fake)
    fake_credentials.side_effect = SecretFetchError(
        ref="gcp-secret://projects/leaky-project/secrets/leaky-secret",
        inner=LeakySecretBackendError("boom"),
    )

    response = post_sync(client)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail == (
        "Credential resolution failed; check connector configuration and secret references."
    )
    assert "LeakySecretBackendError" not in detail
    assert "leaky-project" not in detail
    assert "leaky-secret" not in detail
    assert "secret fetch failed" not in detail
    assert groups.list_synced_groups() == []
    assert audit_sink.records == []
    assert fake.item_calls == []


def test_content_owner_id_with_nul_byte_is_rejected_422():
    """A NUL byte in content_owner_id fails 422, matching the import route's contract."""
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, groups, audit_sink = create_sync_app(fake)

    response = post_sync(client, content_owner_id="own\x00er")

    assert response.status_code == 422
    assert response.json()["detail"] == "content_owner_id contains a NUL character"
    assert groups.list_synced_groups() == []
    assert audit_sink.records == []
    assert fake.item_calls == []


def test_oversized_content_owner_id_is_rejected_422():
    """The owner id lands in the audit entity B-tree index; bound it like the import route."""
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, groups, audit_sink = create_sync_app(fake)

    response = post_sync(client, content_owner_id="x" * 256)

    assert response.status_code == 422
    assert response.json()["detail"] == "content_owner_id exceeds 255 characters"
    assert groups.list_synced_groups() == []
    assert audit_sink.records == []
    assert fake.item_calls == []


def test_google_fetch_failure_returns_502_with_canned_detail():
    """A Google fetch error is a 502 whose detail carries no API internals."""
    fake = FakeGroupsClient(
        [("cms-a", "News", (CHANNEL_ONE,), 0)],
        groups_error=GoogleApiResponseError(
            url="https://youtubeanalytics.googleapis.com/v2/groups",
            reason="leaky-groups-reason",
        ),
    )
    client, _registry, groups, audit_sink = create_sync_app(fake)

    response = post_sync(client)

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "YouTube groups fetch failed" in detail
    assert "leaky-groups-reason" not in detail
    assert "googleapis.com" not in detail
    assert groups.list_synced_groups() == []
    assert audit_sink.records == []


def test_a_member_fetch_failure_leaves_earlier_groups_unwritten():
    """The whole snapshot is fetched before any write, so a late failure writes nothing."""
    fake = FakeGroupsClient(
        [
            ("cms-a", "News", (CHANNEL_ONE,), 0),
            ("cms-b", "Sports", (CHANNEL_TWO,), 0),
        ],
        items_error_for="cms-b",
    )
    client, _registry, groups, audit_sink = create_sync_app(fake)

    response = post_sync(client)

    assert response.status_code == 502
    assert "leaky-items-reason" not in response.json()["detail"]
    # cms-a was fetched successfully and would have been a CREATE, but the
    # apply never runs, so nothing was written for it either.
    assert fake.item_calls == ["cms-a", "cms-b"]
    assert groups.list_synced_groups() == []
    assert audit_sink.records == []


def test_manual_group_survives_a_sync_that_deactivates_a_synced_sibling():
    """A group without a CMS key is invisible to the mirror and untouched."""
    seeded = ChannelGroupRegistry(
        [
            ChannelGroupEntry(
                id="grp-manual",
                name="Manual Bundle",
                group_type="SECTOR",
                active=True,
                channel_ids=(CHANNEL_ONE,),
                cms_group_id=None,
            ),
            ChannelGroupEntry(
                id="grp-synced",
                name="Vanished CMS Group",
                group_type="SECTOR",
                active=True,
                channel_ids=(CHANNEL_TWO,),
                cms_group_id="cms-gone",
                content_owner_id=CONTENT_OWNER,
            ),
        ]
    )
    fake = FakeGroupsClient([])
    client, _registry, groups, audit_sink = create_sync_app(fake, groups=seeded)

    response = post_sync(client)

    assert response.status_code == 200, response.text
    assert response.json()["counts"]["DEACTIVATE"] == 1
    manual = groups.get_group("grp-manual")
    assert manual is not None
    assert manual.active is True
    assert manual.name == "Manual Bundle"
    assert manual.channel_ids == (CHANNEL_ONE,)
    assert groups.get_group("grp-synced").active is False
    assert sole_record(audit_sink, "GROUP_UPDATED").entity_id == "grp-synced"


OTHER_OWNER = "OtherOwnerBBBBBBBBBBBB"


def test_sync_does_not_deactivate_a_different_owners_group():
    """Syncing owner A must not touch a group synced by a different owner B.

    channel_groups carries no owner column of its own beyond the
    content_owner_id stamped at create time; without scoping
    list_synced_groups() to the requesting owner, B's group is simply missing
    from A's upstream snapshot and the planner deactivates it as "vanished".
    """
    seeded = ChannelGroupRegistry(
        [
            ChannelGroupEntry(
                id="grp-other-owner",
                name="Owner B's Sector",
                group_type="SECTOR",
                active=True,
                channel_ids=(CHANNEL_TWO,),
                cms_group_id="cms-owner-b",
                content_owner_id=OTHER_OWNER,
            ),
        ]
    )
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, groups, audit_sink = create_sync_app(fake, groups=seeded)

    response = post_sync(client, content_owner_id=CONTENT_OWNER)

    assert response.status_code == 200, response.text
    assert response.json()["counts"]["DEACTIVATE"] == 0
    other_owner_group = groups.get_group("grp-other-owner")
    assert other_owner_group is not None
    assert other_owner_group.active is True
    assert other_owner_group.channel_ids == (CHANNEL_TWO,)
    assert "grp-other-owner" not in {
        record.entity_id for record in records_of(audit_sink, "GROUP_UPDATED")
    }


def test_create_conflict_on_concurrent_cms_key_returns_409():
    """A tenant-unique cms_group_id race on CREATE is a 409, never a 500.

    The planner only sees THIS owner's local groups (list_synced_groups is
    owner-scoped), so a key already claimed under a different owner still
    looks like CREATE to the plan; the store's per-tenant unique key is the
    authoritative guard, and the route must translate its typed conflict to a
    retryable 409 rather than letting it escape as a server error.
    """
    seeded = ChannelGroupRegistry(
        [
            ChannelGroupEntry(
                id="grp-raced",
                name="Raced In First",
                group_type="SECTOR",
                active=True,
                channel_ids=(CHANNEL_TWO,),
                cms_group_id="cms-a",
                content_owner_id=OTHER_OWNER,
            ),
        ]
    )
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, groups, audit_sink = create_sync_app(fake, groups=seeded)

    response = post_sync(client, content_owner_id=CONTENT_OWNER)

    assert response.status_code == 409, response.text
    assert "cms-a" in response.json()["detail"]
    # No partial write: the raced-in group is untouched, no audit emitted.
    assert groups.get_group("grp-raced").content_owner_id == OTHER_OWNER
    assert audit_sink.records == []


def test_reason_with_nul_byte_is_rejected():
    """A NUL byte in reason is a 422, not an unhandled encoding failure later.

    audit_logs.reason is a PostgreSQL text column; a NUL byte reaching the
    apply/audit boundary would fail with an unhandled encoding error instead
    of the promised validation contract.
    """
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, groups, audit_sink = create_sync_app(fake)

    response = post_sync(client, reason="valid until \x00 here")

    assert response.status_code == 422
    assert response.json()["detail"] == "reason contains a NUL character"
    assert groups.list_synced_groups() == []
    assert audit_sink.records == []


def test_inactive_channel_still_counts_as_known():
    """An archived-but-existing channel must not be treated as unknown.

    known_channel_ids feeds membership reconciliation: an active-only lookup
    would make an inactive channel look "unknown" and plan its removal from
    group membership, even though the groups API itself authorizes over the
    full (active + inactive) member set.
    """
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ONE,
                channel_name="Alpha News",
                primary_company_id="company-tv",
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=CONTENT_OWNER,
                active=False,
            ),
        ]
    )
    fake = FakeGroupsClient([("cms-a", "News", (CHANNEL_ONE,), 0)])
    client, _registry, _groups, _audit_sink = create_sync_app(fake, registry=registry)

    response = post_sync(client, dry_run=True)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["unknown_channel_total"] == 0
    entry = payload["groups"][0]
    assert entry["members_added"] == [CHANNEL_ONE]
    assert entry["unknown_channel_ids"] == []
