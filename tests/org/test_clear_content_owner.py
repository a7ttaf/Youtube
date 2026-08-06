# ============================================================================
# Purpose: Pin the in-memory store's `clear_content_owner` — the one
#   sanctioned eraser for a wrong content-owner stamp, returning a group to
#   the adoptable pool so the correct owner's sync can adopt it.
# Database/ORM: None. In-memory store only; the SQL counterpart is pinned in
#   tests/org/test_sql_channel_groups.py.
# Standards: Clearing is NOT routed through require_adoptable_owner — that
#   guard governs SETTING an owner, not erasing one. Unknown group ->
#   KeyError (the store's existing convention); owner-NULL group ->
#   ChannelGroupNoOwnerStampError (there is nothing to clear).
# Blast Radius: Test-only.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_groups.py -> subject.
# ============================================================================
"""In-memory store: erase a channel group's content-owner stamp."""

import pytest

from ums_smart_revenue.org.channel_groups import (
    ChannelGroupNoOwnerStampError,
    ChannelGroupRegistry,
)


def test_clear_content_owner_nulls_the_stamp_and_leaves_other_fields_unchanged() -> None:
    registry = ChannelGroupRegistry()
    group = registry.create_group(
        name="TV Sector",
        group_type="SECTOR",
        channel_ids=["channel-a", "channel-b"],
        cms_group_id="cms-tv",
        content_owner_id="owner-a",
    )

    cleared = registry.clear_content_owner(group_id=group.id)

    assert cleared.content_owner_id is None
    assert cleared.name == "TV Sector"
    assert cleared.cms_group_id == "cms-tv"
    assert cleared.channel_ids == ("channel-a", "channel-b")
    assert cleared.active is True


def test_clear_content_owner_on_owner_null_group_raises_typed_error() -> None:
    registry = ChannelGroupRegistry()
    group = registry.create_group(
        name="Legacy Sector",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-legacy",
    )

    with pytest.raises(ChannelGroupNoOwnerStampError):
        registry.clear_content_owner(group_id=group.id)


def test_clear_content_owner_on_unknown_group_raises_keyerror() -> None:
    registry = ChannelGroupRegistry()

    with pytest.raises(KeyError):
        registry.clear_content_owner(group_id="missing-group")


def test_cleared_group_is_adoptable_again() -> None:
    registry = ChannelGroupRegistry()
    group = registry.create_group(
        name="TV Sector",
        group_type="SECTOR",
        channel_ids=[],
        cms_group_id="cms-tv",
        content_owner_id="owner-a",
    )
    assert registry.list_adoptable_cms_group_ids({"cms-tv"}) == set()

    registry.clear_content_owner(group_id=group.id)

    assert registry.list_adoptable_cms_group_ids({"cms-tv"}) == {"cms-tv"}
