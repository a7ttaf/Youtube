from datetime import UTC, datetime
# ============================================================================
# Purpose: Unit coverage for org-access index builders — the pure
#   build_org_access_index edge derivation plus the session-backed
#   load_org_access_index_for_scope targeted loader, including the
#   orphan-company authorization denial.
# Database/ORM: org_units + youtube_channels seeded on an in-memory SQLite
#   schema for the scoped-loader tests.
# Standards: seeds minimal active rows under the default tenant; asserts the
#   targeted loader's edges equal the canonical builder's, including the
#   no-sector-parent case.
# Blast Radius: Test-only — guards the authorization containment maps.
# Connections:
#   - File: backend/ums_smart_revenue/org/access_index.py -> loader under test.
#   - File: backend/ums_smart_revenue/auth/scopes.py -> contains() contract.
# ============================================================================
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.org.access_index import (
    ChannelRegistryRow,
    OrgUnitRow,
    build_org_access_index,
    load_org_access_index_for_scope,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import TENANT_CTX
from ums_smart_revenue.tenancy.models import Tenant, TenantStatus

TENANT = UUID(UMS_TENANT_ID)
_NOW = datetime(2026, 5, 17, 20, 0, tzinfo=UTC)


def test_build_org_access_index_maps_channels_to_companies_and_sectors():
    """Verify the builder derives company and sector edges for channels."""
    index = build_org_access_index(
        org_units=[
            OrgUnitRow(id="sector-tv", parent_id=None, type="SECTOR", name="TV", active=True),
            OrgUnitRow(
                id="company-tv-a",
                parent_id="sector-tv",
                type="COMPANY",
                name="TV A",
                active=True,
            ),
            OrgUnitRow(
                id="sector-news",
                parent_id=None,
                type="SECTOR",
                name="News",
                active=True,
            ),
            OrgUnitRow(
                id="company-news-a",
                parent_id="sector-news",
                type="COMPANY",
                name="News A",
                active=True,
            ),
        ],
        channels=[
            ChannelRegistryRow(
                youtube_channel_id="channel-tv-a",
                primary_org_unit_id="company-tv-a",
                active=True,
            ),
            ChannelRegistryRow(
                youtube_channel_id="channel-news-a",
                primary_org_unit_id="company-news-a",
                active=True,
            ),
        ],
    )

    assert index.channel_company == {
        "channel-tv-a": "company-tv-a",
        "channel-news-a": "company-news-a",
    }
    assert index.channel_sector == {
        "channel-tv-a": "sector-tv",
        "channel-news-a": "sector-news",
    }
    assert index.company_sector == {
        "company-tv-a": "sector-tv",
        "company-news-a": "sector-news",
    }


def test_build_org_access_index_ignores_inactive_and_unmapped_channels():
    """Verify inactive/unmapped channels are excluded from the index maps."""
    index = build_org_access_index(
        org_units=[
            OrgUnitRow(id="sector-tv", parent_id=None, type="SECTOR", name="TV", active=True),
            OrgUnitRow(
                id="company-tv-a",
                parent_id="sector-tv",
                type="COMPANY",
                name="TV A",
                active=True,
            ),
            OrgUnitRow(
                id="company-inactive",
                parent_id="sector-tv",
                type="COMPANY",
                name="Old TV",
                active=False,
            ),
        ],
        channels=[
            ChannelRegistryRow(
                youtube_channel_id="channel-tv-a",
                primary_org_unit_id="company-tv-a",
                active=True,
            ),
            ChannelRegistryRow(
                youtube_channel_id="channel-inactive",
                primary_org_unit_id="company-tv-a",
                active=False,
            ),
            ChannelRegistryRow(
                youtube_channel_id="channel-unmapped",
                primary_org_unit_id=None,
                active=True,
            ),
            ChannelRegistryRow(
                youtube_channel_id="channel-inactive-company",
                primary_org_unit_id="company-inactive",
                active=True,
            ),
        ],
    )

    assert index.channel_company == {"channel-tv-a": "company-tv-a"}
    assert index.channel_sector == {"channel-tv-a": "sector-tv"}


def _tenant() -> Tenant:
    """Build the ambient tenant object TENANT_CTX expects."""
    return Tenant(
        id=TENANT,
        slug="ums",
        display_name="UMS Tenant",
        primary_currency="USD",
        status=TenantStatus.ACTIVE,
        onboarding_at=_NOW,
        created_at=_NOW,
        updated_at=_NOW,
    )


# ============================================================================
# Purpose: Create an isolated org schema on SQLite and seed active org_units
#   + youtube_channels rows for the scoped-index tests; returns the live
#   session plus the label->UUID map callers use for scope assertions.
# Database/ORM: creates OrgBase tables; inserts org_units and
#   youtube_channels under the default tenant in one commit.
# Standards: test fixture — explicit ids, active rows only, caller owns the
#   session lifecycle.
# Blast Radius: Test-only.
# Connections:
#   - File: tests/auth/test_access_index_builder.py -> scoped-loader tests.
# ============================================================================
def _seed_org_db(
    *,
    units: list[tuple[str, str | None, str]],
    channels: list[tuple[str, str | None]],
) -> tuple[Session, dict[str, UUID]]:
    """Create an isolated org schema and seed units + channels.

    ``units`` rows are (id, parent_id, type); ``channels`` rows are
    (youtube_channel_id, primary_org_unit_id). All rows are seeded under the
    default tenant and active.
    """
    engine = create_engine("sqlite+pysqlite:///:memory:")
    OrgBase.metadata.create_all(engine)
    session = Session(engine)
    id_map: dict[str, UUID] = {}
    for unit_id, parent_id, unit_type in units:
        uid = uuid4()
        id_map[unit_id] = uid
        session.add(
            OrgUnitORM(
                id=uid,
                tenant_id=TENANT,
                parent_id=id_map.get(parent_id) if parent_id else None,
                type=unit_type,
                name=unit_id,
                active=True,
            )
        )
    for channel_id, primary_unit_id in channels:
        session.add(
            YouTubeChannelORM(
                id=uuid4(),
                tenant_id=TENANT,
                youtube_channel_id=channel_id,
                channel_name=channel_id,
                primary_org_unit_id=(
                    id_map[primary_unit_id] if primary_unit_id else None
                ),
                active=True,
            )
        )
    session.commit()
    return session, id_map


def test_scoped_index_matches_full_index_for_company_and_channel() -> None:
    """Targeted loader must produce the canonical edges for a normal tree."""
    session, id_map = _seed_org_db(
        units=[
            ("sector-tv", None, "SECTOR"),
            ("company-tv-a", "sector-tv", "COMPANY"),
        ],
        channels=[("channel-tv-a", "company-tv-a")],
    )
    token = TENANT_CTX.set(_tenant())
    try:
        scoped_channel = load_org_access_index_for_scope(
            session, AccessScope.channel("channel-tv-a")
        )
        scoped_company = load_org_access_index_for_scope(
            session, AccessScope.company(str(id_map["company-tv-a"]))
        )
    finally:
        TENANT_CTX.reset(token)

    company_id = str(id_map["company-tv-a"])
    sector_id = str(id_map["sector-tv"])
    assert scoped_channel.channel_company == {"channel-tv-a": company_id}
    assert scoped_channel.channel_sector == {"channel-tv-a": sector_id}
    assert scoped_company.company_sector == {company_id: sector_id}


def test_scoped_index_omits_company_edge_for_orphan_company() -> None:
    """A channel under a sector-less company gets NO channel->company edge.

    Regression guard: build_org_access_index only emits channel_company when
    the company resolves to an active sector; the scoped loader must mirror
    that rule or company-scoped admins gain authority over orphan channels.
    """
    session, _id_map = _seed_org_db(
        units=[("company-orphan", None, "COMPANY")],
        channels=[("channel-orphan", "company-orphan")],
    )
    token = TENANT_CTX.set(_tenant())
    try:
        scoped = load_org_access_index_for_scope(
            session, AccessScope.channel("channel-orphan")
        )
    finally:
        TENANT_CTX.reset(token)

    assert scoped.channel_company == {}
    assert scoped.channel_sector == {}
    assert scoped.company_sector == {}
