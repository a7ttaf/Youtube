"""In-memory store: enumerate CMS-synced groups including inactive ones."""

from ums_smart_revenue.org.channel_groups import ChannelGroupRegistry


def test_lists_only_groups_with_a_cms_key() -> None:
    registry = ChannelGroupRegistry()
    registry.create_group(name="Manual", group_type="CUSTOM_GROUP", channel_ids=[])
    synced = registry.create_group(
        name="TV Sector", group_type="SECTOR", channel_ids=[], cms_group_id="cms-tv"
    )
    assert [group.id for group in registry.list_synced_groups()] == [synced.id]


def test_includes_inactive_synced_groups() -> None:
    registry = ChannelGroupRegistry()
    group = registry.create_group(
        name="News", group_type="SECTOR", channel_ids=[], cms_group_id="cms-news"
    )
    registry.update_group(group_id=group.id, name=None, active=False)
    listed = registry.list_synced_groups()
    assert len(listed) == 1
    assert listed[0].active is False
