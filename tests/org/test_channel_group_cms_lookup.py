"""In-memory channel-group store: CMS key round-trip."""

from ums_smart_revenue.org.channel_groups import ChannelGroupRegistry


def test_create_group_records_cms_group_id() -> None:
    registry = ChannelGroupRegistry()
    group = registry.create_group(
        name="TV Sector",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
    )
    assert group.cms_group_id == "cms-tv"


def test_get_group_by_cms_id_finds_the_group() -> None:
    registry = ChannelGroupRegistry()
    created = registry.create_group(
        name="TV Sector",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
    )
    assert registry.get_group_by_cms_id("cms-tv") == created


def test_get_group_by_cms_id_returns_none_when_absent() -> None:
    registry = ChannelGroupRegistry()
    assert registry.get_group_by_cms_id("cms-missing") is None
