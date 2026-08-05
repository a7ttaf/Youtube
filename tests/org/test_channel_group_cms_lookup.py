"""In-memory channel-group store: CMS key round-trip."""

import pytest

from ums_smart_revenue.org.channel_groups import ChannelGroupConflictError, ChannelGroupRegistry


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


def test_list_foreign_owner_cms_group_ids_excludes_own_and_unclaimed_keys() -> None:
    """Only keys stamped to ANOTHER owner conflict; owner-NULL is adoptable."""
    registry = ChannelGroupRegistry()
    registry.create_group(
        name="Mine",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-mine",
        content_owner_id="owner-a",
    )
    registry.create_group(
        name="Theirs",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-theirs",
        content_owner_id="owner-b",
    )
    registry.create_group(
        name="Legacy", group_type="SECTOR", channel_ids=[], cms_group_id="cms-legacy"
    )

    result = registry.list_foreign_owner_cms_group_ids(
        {"cms-mine", "cms-theirs", "cms-legacy", "cms-missing"},
        content_owner_id="owner-a",
    )

    assert result == {"cms-theirs"}


def test_create_group_duplicate_cms_key_raises_typed_conflict() -> None:
    """Parity with the SQL store's per-tenant unique key on cms_group_id."""
    registry = ChannelGroupRegistry()
    registry.create_group(name="TV", group_type="SECTOR", channel_ids=[], cms_group_id="cms-tv")

    with pytest.raises(ChannelGroupConflictError, match="cms-tv"):
        registry.create_group(
            name="TV Again", group_type="SECTOR", channel_ids=[], cms_group_id="cms-tv"
        )
