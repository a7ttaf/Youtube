from ums_smart_revenue.org.access_index import (
    ChannelRegistryRow,
    OrgUnitRow,
    build_org_access_index,
)


def test_build_org_access_index_maps_channels_to_companies_and_sectors():
    index = build_org_access_index(
        org_units=[
            OrgUnitRow(
                id="sector-tv", parent_id=None, type="SECTOR", name="TV", active=True
            ),
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
    index = build_org_access_index(
        org_units=[
            OrgUnitRow(
                id="sector-tv", parent_id=None, type="SECTOR", name="TV", active=True
            ),
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
