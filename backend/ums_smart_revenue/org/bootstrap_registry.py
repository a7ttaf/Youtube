from ums_smart_revenue.org.access_index import (
    ChannelRegistryRow,
    OrgUnitRow,
    build_org_access_index,
)

BOOTSTRAP_SECTOR_TV_ID = "00000000-0000-0000-0000-000000000101"
BOOTSTRAP_SECTOR_NEWS_ID = "00000000-0000-0000-0000-000000000102"
BOOTSTRAP_COMPANY_TV_ID = "00000000-0000-0000-0000-000000000201"
BOOTSTRAP_COMPANY_NEWS_ID = "00000000-0000-0000-0000-000000000202"

BOOTSTRAP_ORG_UNITS = [
    OrgUnitRow(id=BOOTSTRAP_SECTOR_TV_ID, parent_id=None, type="SECTOR", name="TV", active=True),
    OrgUnitRow(
        id=BOOTSTRAP_SECTOR_NEWS_ID,
        parent_id=None,
        type="SECTOR",
        name="News",
        active=True,
    ),
    OrgUnitRow(
        id=BOOTSTRAP_COMPANY_TV_ID,
        parent_id=BOOTSTRAP_SECTOR_TV_ID,
        type="COMPANY",
        name="TV Company A",
        active=True,
    ),
    OrgUnitRow(
        id=BOOTSTRAP_COMPANY_NEWS_ID,
        parent_id=BOOTSTRAP_SECTOR_NEWS_ID,
        type="COMPANY",
        name="News Company A",
        active=True,
    ),
]

BOOTSTRAP_CHANNELS = [
    ChannelRegistryRow(
        youtube_channel_id="channel-tv-a",
        primary_org_unit_id=BOOTSTRAP_COMPANY_TV_ID,
        active=True,
    ),
    ChannelRegistryRow(
        youtube_channel_id="channel-news-a",
        primary_org_unit_id=BOOTSTRAP_COMPANY_NEWS_ID,
        active=True,
    ),
]

BOOTSTRAP_ORG_INDEX = build_org_access_index(
    org_units=BOOTSTRAP_ORG_UNITS,
    channels=BOOTSTRAP_CHANNELS,
)
