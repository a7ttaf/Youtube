"""In-memory registry: inventory field update."""

import pytest

from ums_smart_revenue.org.channel_registry import (
    ChannelRegistry,
    ChannelRegistryEntry,
    ChannelRegistryValidationError,
)

CHANNEL_ID = "UCB6sc84dcg6VQGB_d89sx2g"


def _registry() -> ChannelRegistry:
    return ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Old Name",
                primary_company_id=None,
                cms_status="UNKNOWN",
                revenue_required=False,
                content_owner_id=None,
            )
        ]
    )


def test_update_inventory_replaces_all_four_fields() -> None:
    updated = _registry().update_inventory(
        youtube_channel_id=CHANNEL_ID,
        channel_name="CBC Egypt",
        cms_status="INSIDE_CMS",
        content_owner_id="PlZrS5Fh56RMd9dmSL6XSA",
        revenue_required=True,
    )
    assert updated.channel_name == "CBC Egypt"
    assert updated.cms_status == "INSIDE_CMS"
    assert updated.content_owner_id == "PlZrS5Fh56RMd9dmSL6XSA"
    assert updated.revenue_required is True


def test_update_inventory_keeps_revenue_source_status_consistent() -> None:
    updated = _registry().update_inventory(
        youtube_channel_id=CHANNEL_ID,
        channel_name="CBC Egypt",
        cms_status="INSIDE_CMS",
        content_owner_id="PlZrS5Fh56RMd9dmSL6XSA",
        revenue_required=True,
    )
    assert updated.revenue_source_status == "MISSING_REVENUE_SOURCE"


def test_update_inventory_rejects_unknown_channel() -> None:
    with pytest.raises(ChannelRegistryValidationError):
        _registry().update_inventory(
            youtube_channel_id="UCzzzzzzzzzzzzzzzzzzzzzz",
            channel_name="Nope",
            cms_status="INSIDE_CMS",
            content_owner_id=None,
            revenue_required=True,
        )
