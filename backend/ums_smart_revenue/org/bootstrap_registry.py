from ums_smart_revenue.org.access_index import ChannelRegistryRow, OrgUnitRow, build_org_access_index


BOOTSTRAP_ORG_UNITS = [
    OrgUnitRow(id="tv", parent_id=None, type="SECTOR", name="TV", active=True),
    OrgUnitRow(id="news", parent_id=None, type="SECTOR", name="News", active=True),
    OrgUnitRow(id="company-tv-a", parent_id="tv", type="COMPANY", name="TV Company A", active=True),
    OrgUnitRow(id="company-news-a", parent_id="news", type="COMPANY", name="News Company A", active=True),
]

BOOTSTRAP_CHANNELS = [
    ChannelRegistryRow(youtube_channel_id="channel-tv-a", primary_org_unit_id="company-tv-a", active=True),
    ChannelRegistryRow(youtube_channel_id="channel-news-a", primary_org_unit_id="company-news-a", active=True),
]

BOOTSTRAP_ORG_INDEX = build_org_access_index(
    org_units=BOOTSTRAP_ORG_UNITS,
    channels=BOOTSTRAP_CHANNELS,
)

