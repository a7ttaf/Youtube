from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import (
    current_audit_sink,
    current_channel_registry,
    sql_channel_registry_from_session,
)
from ums_smart_revenue.api.dependencies_finance import current_org_access_index
from ums_smart_revenue.api.registry_dependencies import sql_group_registry_from_session
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.db.finance_models import (
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import (
    OrgBase,
    OrgUnitORM,
    YouTubeChannelORM,
)
from ums_smart_revenue.db.security_models import SecurityBase, UserORM
from ums_smart_revenue.org.bootstrap_registry import (
    BOOTSTRAP_COMPANY_NEWS_ID,
    BOOTSTRAP_COMPANY_TV_ID,
    BOOTSTRAP_ORG_INDEX,
    BOOTSTRAP_SECTOR_TV_ID,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry, ChannelGroupRegistry
from ums_smart_revenue.org.channel_registry import (
    ChannelRegistry,
    ChannelRegistryEntry,
    ChannelRegistryValidationError,
    bootstrap_channel_registry,
)

# This module contains tests and utilities for channel management API endpoints.
# It defines stub registries, helper functions for creating test applications and
# authentication headers, and test cases for channel listing behaviors.


class StaleUpdateRegistry:
    """Registry stub that raises when mapping updates race with concurrent changes."""

    @staticmethod
    def list_channels() -> list[ChannelRegistryEntry]:
        """Return an empty list of channel registry entries for stale updates."""
        return []

    @staticmethod
    def get_channel(youtube_channel_id: str) -> ChannelRegistryEntry | None:
        """Return a default channel registry entry for the given YouTube channel ID."""
        return ChannelRegistryEntry(
            youtube_channel_id=youtube_channel_id,
            channel_name="TV A",
            primary_company_id=BOOTSTRAP_COMPANY_TV_ID,
            cms_status="UNKNOWN",
            revenue_required=True,
        )

    @staticmethod
    def create_channel(
        *,
        youtube_channel_id: str,
        channel_name: str,
        primary_company_id: str | None,
        cms_status: str,
        revenue_required: bool,
    ) -> ChannelRegistryEntry:
        """Stub for channel creation; not implemented in stale update registry."""
        raise NotImplementedError

    @staticmethod
    def update_mapping(
        *,
        youtube_channel_id: str,
        primary_company_id: str | None,  # noqa: ARG002
    ) -> ChannelRegistryEntry:
        """Attempt to update the mapping for a channel, raising KeyError for stale entries."""
        raise KeyError(youtube_channel_id)


class ScopedListRegistry:
    """Registry stub that restricts visible channels to an explicit id allowlist."""

    @staticmethod
    def list_channels() -> list[ChannelRegistryEntry]:
        """Disallow unscoped listings for scoped callers by raising an assertion."""
        raise AssertionError("unscoped channel listing should not be used for scoped callers")

    @staticmethod
    def list_channels_by_ids(youtube_channel_ids: set[str]) -> list[ChannelRegistryEntry]:
        """Return channel entries only for the specified set of YouTube channel IDs."""
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
    """Create an application with bootstrap channel registry and
    org access index overrides for testing."""
    app = create_app()
    registry = bootstrap_channel_registry()
    app.dependency_overrides[current_channel_registry] = lambda: registry
    app.dependency_overrides[current_org_access_index] = lambda: BOOTSTRAP_ORG_INDEX
    return app


def auth_headers(role: str, scope_type: str, scope_id: str | None = None) -> dict[str, str]:
    """Generate authentication headers for a test client given
    role, scope type, and optional scope ID."""
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
    """Test that a company manager can only list channels associated with their company."""
    app = create_bootstrap_app()
    client = TestClient(app)

    response = client.get(
        "/channels",
        headers=auth_headers("company_manager", "company", BOOTSTRAP_COMPANY_TV_ID),
    )

    assert response.status_code == 200
    assert [channel["youtube_channel_id"] for channel in response.json()] == ["channel-tv-a"]


def test_company_channel_listing_uses_scoped_registry_query():
    """Test that channel listing uses the scoped registry when overrides are applied."""
    app = create_bootstrap_app()
    app.dependency_overrides[current_channel_registry] = ScopedListRegistry
    client = TestClient(app)

    response = client.get(
        "/channels",
        headers=auth_headers("company_manager", "company", BOOTSTRAP_COMPANY_TV_ID),
    )

    assert response.status_code == 200
    assert [channel["youtube_channel_id"] for channel in response.json()] == ["channel-tv-a"]


def test_company_manager_reads_scoped_outside_cms_monitor():
    """Test that a company manager can read channels with
    OUTSIDE_CMS status using scoped registry."""
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
    """Manual official revenue imports remain visible in global outside-CMS monitoring."""
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
    manual_import_items = [
        item for item in payload["items"] if item["youtube_channel_id"] == "channel-news-a"
    ]
    assert len(manual_import_items) == 1
    manual_import_item = manual_import_items[0]
    assert manual_import_item["missing_official_revenue"] is False
    assert manual_import_item["recommended_action"] == (
        "Keep manual official revenue import current; CMS linking remains recommended."
    )


def test_company_manager_reads_scoped_channel_issues_without_cross_company_leak():
    """Company managers only see channel issues for their own company."""
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
    app.dependency_overrides[sql_group_registry_from_session] = ChannelGroupRegistry
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
    """Global channel issues include registry health summary details."""
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
    app.dependency_overrides[sql_group_registry_from_session] = lambda: ChannelGroupRegistry(
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
    issue_tuples = [(item["youtube_channel_id"], item["issue_type"]) for item in payload["items"]]
    assert ("channel-orphan", "MISSING_COMPANY") in issue_tuples
    assert ("channel-missing-sector", "MISSING_SECTOR") in issue_tuples
    assert all(channel_id != "channel-news-a" for channel_id, _ in issue_tuples)


def test_assistant_cannot_create_channel():
    """Test that an assistant analyst cannot create a channel due to missing permissions."""
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
    """Test that a data steward can create a channel within their assigned company."""
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
    assert response.json()["audit_event"]["event_type"] == "CHANNEL_CREATED"
    assert response.json()["audit_event"]["sensitive"] is True


def test_channel_requests_reject_blank_strings():
    """Test that channel creation and mapping requests reject blank string inputs."""
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
    """Test that a data steward cannot create a channel in a different company."""
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
    """Test that mapping change requests require a reason and proper permissions."""
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
    """Test that authorization is checked before resource existence for mapping changes."""
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
    assert response.json()["detail"] == "Missing permission: registry.manage_org_mapping"


def test_mapping_change_is_audited_for_corporate_admin():
    """Test that mapping changes by corporate admin are audited correctly."""
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
    assert response.json()["audit_event"]["reason"] == "Corporate remap after ownership transfer"


def test_registry_factory_returns_fresh_state_per_app():
    """Test that each app instance gets a fresh channel registry state."""
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


MAPPING_USER_ID = UUID("00000000-0000-0000-0000-0000000c0401")
MAPPING_CHANNEL_ID = "channel-tv-a"


def _sql_mapping_auth_headers() -> dict[str, str]:
    """Return super_owner global headers for the SQL-backed mapping app."""
    return {
        "x-user-id": str(MAPPING_USER_ID),
        "x-user-email": "map-admin@example.com",
        "x-role": "super_owner",
        "x-scope-type": "global",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }


def _seed_sql_mapping_app(tmp_path, *, month: str, month_status: str):
    """Create a SQL-backed app whose channel-tv-a has one fact in ``month``.

    Returns a TestClient wired so PATCH /channels/{id}/mapping hits the SQL
    registry guard (org index pinned to the bootstrap index for the gate).
    """
    database_url = f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            UserORM(
                id=MAPPING_USER_ID,
                email="map-admin@example.com",
                display_name="Map Admin",
            )
        )
        session.add_all(
            [
                OrgUnitORM(
                    id=UUID(BOOTSTRAP_SECTOR_TV_ID),
                    parent_id=None,
                    type="SECTOR",
                    name="TV",
                    active=True,
                ),
                OrgUnitORM(
                    id=UUID(BOOTSTRAP_COMPANY_TV_ID),
                    parent_id=UUID(BOOTSTRAP_SECTOR_TV_ID),
                    type="COMPANY",
                    name="TV Company",
                    active=True,
                ),
                OrgUnitORM(
                    id=UUID(BOOTSTRAP_COMPANY_NEWS_ID),
                    parent_id=UUID(BOOTSTRAP_SECTOR_TV_ID),
                    type="COMPANY",
                    name="News Company",
                    active=True,
                ),
                YouTubeChannelORM(
                    id=uuid4(),
                    youtube_channel_id=MAPPING_CHANNEL_ID,
                    channel_name="TV A",
                    primary_org_unit_id=UUID(BOOTSTRAP_COMPANY_TV_ID),
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
            ]
        )
        session.add(FinanceMonthCloseORM(month=month, status=month_status))
        session.add(
            MonthlyChannelRevenueFactORM(
                id=uuid4(),
                month=month,
                youtube_channel_id=MAPPING_CHANNEL_ID,
                source_kind="YOUTUBE_CMS",
                gross_revenue_usd=100,
            )
        )
        session.commit()
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_channel_registry] = sql_channel_registry_from_session
    app.dependency_overrides[current_org_access_index] = lambda: BOOTSTRAP_ORG_INDEX
    return TestClient(app)


def test_mapping_change_rejected_for_locked_month_fact_without_audit(tmp_path):
    """Re-parenting a channel with a LOCKED-month fact returns 409 and is not audited."""
    client = _seed_sql_mapping_app(tmp_path, month="2026-09", month_status="LOCKED")
    audit_sink = InMemoryAuditSink()
    client.app.dependency_overrides[current_audit_sink] = lambda: audit_sink

    response = client.patch(
        f"/channels/{MAPPING_CHANNEL_ID}/mapping",
        headers=_sql_mapping_auth_headers(),
        json={
            "primary_company_id": BOOTSTRAP_COMPANY_NEWS_ID,
            "reason": "attempt remap on locked month",
        },
    )

    assert response.status_code == 409
    assert "locked" in response.json()["detail"].lower()
    # A rejected mapping change must not be audited as an applied update.
    assert audit_sink.records == []


def test_mapping_change_allowed_for_open_month_fact(tmp_path):
    """Re-parenting a channel whose only fact is in an OPEN month returns 200."""
    client = _seed_sql_mapping_app(tmp_path, month="2026-09", month_status="OPEN")

    response = client.patch(
        f"/channels/{MAPPING_CHANNEL_ID}/mapping",
        headers=_sql_mapping_auth_headers(),
        json={
            "primary_company_id": BOOTSTRAP_COMPANY_NEWS_ID,
            "reason": "remap on open month",
        },
    )

    assert response.status_code == 200
    assert response.json()["primary_company_id"] == BOOTSTRAP_COMPANY_NEWS_ID
    assert response.json()["audit_event"]["event_type"] == "CHANNEL_UPDATED"


def test_mapping_change_preserves_404_if_channel_disappears_before_update():
    """Test that a 404 is preserved if the channel is removed before mapping update."""
    app = create_bootstrap_app()
    app.dependency_overrides[current_channel_registry] = StaleUpdateRegistry
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


def test_no_op_mapping_change_returns_200_without_audit(tmp_path):
    """Idempotent / no-op PATCH (same primary_company_id) returns 200 and is not audited.

    Pairs with test_mapping_change_rejected_for_locked_month_fact_without_audit
    to prove the audit decision lives at the route boundary and that safe
    retries do not produce a misleading CHANNEL_UPDATED audit event.
    """
    client = _seed_sql_mapping_app(tmp_path, month="2026-09", month_status="LOCKED")
    audit_sink = InMemoryAuditSink()
    client.app.dependency_overrides[current_audit_sink] = lambda: audit_sink

    response = client.patch(
        f"/channels/{MAPPING_CHANNEL_ID}/mapping",
        headers=_sql_mapping_auth_headers(),
        json={
            "primary_company_id": BOOTSTRAP_COMPANY_TV_ID,  # matches existing
            "reason": "resubmit current mapping value",
        },
    )

    assert response.status_code == 200
    assert response.json()["primary_company_id"] == BOOTSTRAP_COMPANY_TV_ID
    assert response.json()["audit_event"] is None
    assert audit_sink.records == []


def test_create_channel_persists_content_owner_id():
    """A channel can be created with its CMS content_owner_id set."""
    client = TestClient(create_bootstrap_app())

    response = client.post(
        "/channels",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={
            "youtube_channel_id": "channel-cms-new",
            "channel_name": "CMS New",
            "primary_company_id": BOOTSTRAP_COMPANY_TV_ID,
            "cms_status": "INSIDE_CMS",
            "revenue_required": True,
            "content_owner_id": "owner-cms-1",
        },
    )

    assert response.status_code == 201
    assert response.json()["content_owner_id"] == "owner-cms-1"
    assert response.json()["audit_event"]["event_type"] == "CHANNEL_CREATED"


def test_create_channel_defaults_content_owner_id_to_none():
    """content_owner_id is optional on create; omitting it leaves it unset."""
    client = TestClient(create_bootstrap_app())

    response = client.post(
        "/channels",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={
            "youtube_channel_id": "channel-no-owner",
            "channel_name": "No Owner",
            "primary_company_id": BOOTSTRAP_COMPANY_TV_ID,
            "cms_status": "UNKNOWN",
            "revenue_required": True,
        },
    )

    assert response.status_code == 201
    assert response.json()["content_owner_id"] is None


def test_create_channel_rejects_blank_content_owner_id():
    """A present-but-blank content_owner_id is rejected, not coerced to null."""
    client = TestClient(create_bootstrap_app())

    response = client.post(
        "/channels",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={
            "youtube_channel_id": "channel-blank-owner",
            "channel_name": "Blank Owner",
            "primary_company_id": BOOTSTRAP_COMPANY_TV_ID,
            "cms_status": "INSIDE_CMS",
            "revenue_required": True,
            "content_owner_id": "   ",
        },
    )

    assert response.status_code == 422


def test_set_content_owner_is_audited():
    """PATCH content-owner sets the field and records CHANNEL_UPDATED with old/new."""
    app = create_bootstrap_app()
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    client = TestClient(app)

    response = client.patch(
        "/channels/channel-tv-a/content-owner",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={"content_owner_id": "owner-1", "reason": "Link to CMS account"},
    )

    assert response.status_code == 200
    assert response.json()["content_owner_id"] == "owner-1"
    assert response.json()["audit_event"]["event_type"] == "CHANNEL_UPDATED"
    assert response.json()["audit_event"]["reason"] == "Link to CMS account"
    assert len(audit_sink.records) == 1
    assert audit_sink.records[0].details == {
        "old_content_owner_id": None,
        "new_content_owner_id": "owner-1",
    }


def test_content_owner_audit_tags_manage_channels_permission():
    """The CHANNEL_UPDATED audit for a content-owner change must be tagged with
    registry.manage_channels (the permission that authorized the write), not the
    registry.manage_org_mapping default on the CHANNEL_UPDATED definition —
    otherwise permission-based audit filtering misattributes the write."""
    app = create_bootstrap_app()
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    client = TestClient(app)

    response = client.patch(
        "/channels/channel-tv-a/content-owner",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={"content_owner_id": "owner-perm", "reason": "Tag audit permission"},
    )

    assert response.status_code == 200
    assert len(audit_sink.records) == 1
    # MANAGE_CHANNELS = "registry.manage_channels", NOT "registry.manage_org_mapping".
    assert audit_sink.records[0].permission == "registry.manage_channels"


def test_no_op_content_owner_change_returns_200_without_audit():
    """Re-submitting the current content_owner_id is a no-op and is not audited."""
    app = create_bootstrap_app()
    audit_sink = InMemoryAuditSink()
    app.dependency_overrides[current_audit_sink] = lambda: audit_sink
    client = TestClient(app)

    response = client.patch(
        "/channels/channel-tv-a/content-owner",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={"content_owner_id": None, "reason": "resubmit current value"},
    )

    assert response.status_code == 200
    assert response.json()["content_owner_id"] is None
    assert response.json()["audit_event"] is None
    assert audit_sink.records == []


def test_content_owner_update_requires_manage_channels_permission():
    """Setting content_owner_id requires MANAGE_CHANNELS, not analytics view."""
    client = TestClient(create_bootstrap_app())

    response = client.patch(
        "/channels/channel-tv-a/content-owner",
        headers=auth_headers("assistant_analyst", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={"content_owner_id": "owner-1", "reason": "unauthorized"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: registry.manage_channels"


def test_content_owner_update_authorizes_before_not_found():
    """Authorization is checked before existence: an unauthorized caller gets 403."""
    client = TestClient(create_bootstrap_app())

    response = client.patch(
        "/channels/missing-channel/content-owner",
        headers=auth_headers("assistant_analyst", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={"content_owner_id": "owner-1", "reason": "unauthorized lookup"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: registry.manage_channels"


def test_content_owner_update_404_for_missing_channel_when_authorized():
    """An authorized caller targeting a missing channel gets 404."""
    client = TestClient(create_bootstrap_app())

    response = client.patch(
        "/channels/missing-channel/content-owner",
        headers=auth_headers("super_owner", "global"),
        json={"content_owner_id": "owner-1", "reason": "set owner on missing"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Channel not found"


def test_content_owner_update_requires_reason():
    """A content-owner change must carry a reason (audit provenance)."""
    client = TestClient(create_bootstrap_app())

    response = client.patch(
        "/channels/channel-tv-a/content-owner",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={"content_owner_id": "owner-1"},
    )

    assert response.status_code == 422


def test_content_owner_update_rejects_blank_content_owner_id():
    """A present-but-blank content_owner_id is rejected on update too."""
    client = TestClient(create_bootstrap_app())

    response = client.patch(
        "/channels/channel-tv-a/content-owner",
        headers=auth_headers("data_steward", "company", BOOTSTRAP_COMPANY_TV_ID),
        json={"content_owner_id": "   ", "reason": "blank owner"},
    )

    assert response.status_code == 422


class ValidationFailingContentOwnerRegistry:
    """Registry stub whose update_content_owner raises ChannelRegistryValidationError.

    Mirrors how a flush() IntegrityError is converted inside the SQL registry, so
    the route's 422 translation can be exercised without a live database.
    """

    def __init__(self) -> None:
        self._owner: str | None = None

    @staticmethod
    def list_channels() -> list[ChannelRegistryEntry]:
        return []

    def get_channel(self, youtube_channel_id: str) -> ChannelRegistryEntry | None:
        return ChannelRegistryEntry(
            youtube_channel_id=youtube_channel_id,
            channel_name="TV A",
            primary_company_id=BOOTSTRAP_COMPANY_TV_ID,
            cms_status="UNKNOWN",
            revenue_required=True,
            content_owner_id=self._owner,
        )

    @staticmethod
    def update_content_owner(
        *,
        youtube_channel_id: str,
        content_owner_id: str | None,  # noqa: ARG001
    ) -> ChannelRegistryEntry:
        raise ChannelRegistryValidationError("simulated flush integrity failure")


def test_content_owner_update_translates_registry_validation_error_to_422():
    """A ChannelRegistryValidationError from update_content_owner must surface as
    HTTP 422 (matching create_channel/update_mapping), not an unhandled 500."""
    app = create_bootstrap_app()
    # Pass the class directly (FastAPI instantiates it), matching the file's
    # existing ScopedListRegistry override style and avoiding a needless lambda.
    app.dependency_overrides[current_channel_registry] = ValidationFailingContentOwnerRegistry
    client = TestClient(app)

    response = client.patch(
        "/channels/channel-tv-a/content-owner",
        headers=auth_headers("super_owner", "global"),
        json={"content_owner_id": "owner-1", "reason": "trigger validation error"},
    )

    assert response.status_code == 422
    assert "simulated flush integrity failure" in response.json()["detail"]
