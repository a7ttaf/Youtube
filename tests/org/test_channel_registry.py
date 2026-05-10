import pytest

from ums_smart_revenue.org.channel_registry import (
    ChannelRegistry,
    ChannelRegistryConflictError,
    ChannelRegistryEntry,
    ChannelRegistryValidationError,
)


COMPANY_TV_ID = "00000000-0000-0000-0000-000000000201"
COMPANY_NEWS_ID = "00000000-0000-0000-0000-000000000202"


def test_in_memory_channel_registry_validates_primary_company_id_on_create():
    registry = ChannelRegistry()

    channel = registry.create_channel(
        youtube_channel_id="channel-no-company",
        channel_name="No Company",
        primary_company_id=None,
        cms_status="UNKNOWN",
        revenue_required=True,
    )

    assert channel.primary_company_id is None
    with pytest.raises(ChannelRegistryValidationError, match="primary_company_id must be a valid UUID"):
        registry.create_channel(
            youtube_channel_id="channel-bad-company",
            channel_name="Bad Company",
            primary_company_id="not-a-uuid",
            cms_status="UNKNOWN",
            revenue_required=True,
        )


def test_in_memory_channel_registry_validates_primary_company_id_on_update():
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id="channel-tv-a",
                channel_name="TV A",
                primary_company_id=COMPANY_TV_ID,
                cms_status="UNKNOWN",
                revenue_required=True,
            )
        ]
    )

    updated = registry.update_mapping(
        youtube_channel_id="channel-tv-a",
        primary_company_id=COMPANY_NEWS_ID,
    )

    assert updated.primary_company_id == COMPANY_NEWS_ID
    updated_without_company = registry.update_mapping(
        youtube_channel_id="channel-tv-a",
        primary_company_id=None,
    )

    assert updated_without_company.primary_company_id is None
    with pytest.raises(ChannelRegistryValidationError, match="primary_company_id must be a valid UUID"):
        registry.update_mapping(youtube_channel_id="channel-tv-a", primary_company_id="not-a-uuid")


def test_in_memory_channel_registry_rejects_duplicate_initial_channels():
    with pytest.raises(ChannelRegistryConflictError, match="Duplicate channel id: channel-tv-a"):
        ChannelRegistry(
            [
                ChannelRegistryEntry(
                    youtube_channel_id="channel-tv-a",
                    channel_name="TV A",
                    primary_company_id=COMPANY_TV_ID,
                    cms_status="UNKNOWN",
                    revenue_required=True,
                ),
                ChannelRegistryEntry(
                    youtube_channel_id="channel-tv-a",
                    channel_name="TV A duplicate",
                    primary_company_id=COMPANY_TV_ID,
                    cms_status="UNKNOWN",
                    revenue_required=True,
                ),
            ]
        )


def test_in_memory_channel_registry_list_channels_excludes_inactive_entries():
    registry = ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id="channel-active",
                channel_name="Active",
                primary_company_id=COMPANY_TV_ID,
                cms_status="UNKNOWN",
                revenue_required=True,
                active=True,
            ),
            ChannelRegistryEntry(
                youtube_channel_id="channel-inactive",
                channel_name="Inactive",
                primary_company_id=COMPANY_TV_ID,
                cms_status="UNKNOWN",
                revenue_required=True,
                active=False,
            ),
        ]
    )

    assert [channel.youtube_channel_id for channel in registry.list_channels()] == ["channel-active"]
