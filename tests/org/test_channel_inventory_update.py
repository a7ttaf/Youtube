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
    _previous, updated = _registry().update_inventory(
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
    _previous, updated = _registry().update_inventory(
        youtube_channel_id=CHANNEL_ID,
        channel_name="CBC Egypt",
        cms_status="INSIDE_CMS",
        content_owner_id="PlZrS5Fh56RMd9dmSL6XSA",
        revenue_required=True,
    )
    assert updated.revenue_source_status == "MISSING_REVENUE_SOURCE"


def _registry_with_official_status(status: str) -> ChannelRegistry:
    return ChannelRegistry(
        [
            ChannelRegistryEntry(
                youtube_channel_id=CHANNEL_ID,
                channel_name="Old Name",
                primary_company_id=None,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                content_owner_id=None,
                revenue_source_status=status,
            )
        ]
    )


def test_update_inventory_preserves_official_status_when_flag_unchanged() -> None:
    """A name/owner refresh must not clobber a proven official revenue source."""
    _previous, updated = _registry_with_official_status("OFFICIAL_CMS_REVENUE").update_inventory(
        youtube_channel_id=CHANNEL_ID,
        channel_name="New Name",
        cms_status="INSIDE_CMS",
        content_owner_id="PlZrS5Fh56RMd9dmSL6XSA",
        revenue_required=True,
    )
    assert updated.revenue_source_status == "OFFICIAL_CMS_REVENUE"


def test_update_inventory_rederives_status_when_flag_flips() -> None:
    """Flipping revenue_required off parks even an official channel."""
    _previous, updated = _registry_with_official_status("OFFICIAL_MANUAL_IMPORT").update_inventory(
        youtube_channel_id=CHANNEL_ID,
        channel_name="Old Name",
        cms_status="INSIDE_CMS",
        content_owner_id=None,
        revenue_required=False,
    )
    assert updated.revenue_source_status == "PERFORMANCE_ONLY"


def test_update_inventory_rejects_unknown_channel() -> None:
    with pytest.raises(ChannelRegistryValidationError):
        _registry().update_inventory(
            youtube_channel_id="UCzzzzzzzzzzzzzzzzzzzzzz",
            channel_name="Nope",
            cms_status="INSIDE_CMS",
            content_owner_id=None,
            revenue_required=True,
        )
