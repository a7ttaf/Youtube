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


def test_list_archived_cms_group_ids_returns_only_archived_keys() -> None:
    """One bulk call classifies a roster's CMS keys by archived state."""
    registry = ChannelGroupRegistry()
    active = registry.create_group(
        name="Active", group_type="SECTOR", channel_ids=[], cms_group_id="cms-active"
    )
    archived = registry.create_group(
        name="Archived", group_type="SECTOR", channel_ids=[], cms_group_id="cms-archived"
    )
    registry.update_group(group_id=archived.id, name=None, active=False)

    result = registry.list_archived_cms_group_ids({"cms-active", "cms-archived", "cms-missing"})

    assert result == {"cms-archived"}
    assert registry.get_group_by_cms_id("cms-active") == active
