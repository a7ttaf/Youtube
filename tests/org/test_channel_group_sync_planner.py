"""Pure planning for CMS group sync."""

from ums_smart_revenue.org.channel_group_sync import (
    CmsGroupSnapshot,
    GroupSyncOutcome,
    plan_group_sync,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry

CH_A = "UCB6sc84dcg6VQGB_d89sx2g"
CH_B = "UC3Dci3BzZXDo4jw4dU8KqWg"
CH_UNKNOWN = "UCzzzzzzzzzzzzzzzzzzzzzz"
KNOWN = frozenset({CH_A, CH_B})


def _snapshot(**overrides: object) -> CmsGroupSnapshot:
    defaults: dict[str, object] = {
        "cms_group_id": "g1",
        "title": "TV Sector",
        "member_channel_ids": (CH_A,),
        "non_channel_member_count": 0,
    }
    defaults.update(overrides)
    return CmsGroupSnapshot(**defaults)  # type: ignore[arg-type]


def _local(**overrides: object) -> ChannelGroupEntry:
    defaults: dict[str, object] = {
        "id": "local-1",
        "name": "TV Sector",
        "group_type": "SECTOR",
        "active": True,
        "channel_ids": (CH_A,),
        "cms_group_id": "g1",
    }
    defaults.update(overrides)
    return ChannelGroupEntry(**defaults)  # type: ignore[arg-type]


def _plan(snapshot=(), local=(), known=KNOWN):
    return plan_group_sync(
        snapshot=tuple(snapshot), local_groups=tuple(local), known_channel_ids=known
    )


def test_new_cms_group_plans_create() -> None:
    plan = _plan(snapshot=[_snapshot()])
    entry = plan.entries[0]
    assert entry.outcome is GroupSyncOutcome.CREATE
    assert entry.members_added == (CH_A,)
    assert plan.counts["CREATE"] == 1


def test_identical_group_is_unchanged() -> None:
    plan = _plan(snapshot=[_snapshot()], local=[_local()])
    assert plan.entries[0].outcome is GroupSyncOutcome.UNCHANGED


def test_title_difference_plans_rename() -> None:
    plan = _plan(snapshot=[_snapshot(title="TV")], local=[_local()])
    entry = plan.entries[0]
    assert entry.outcome is GroupSyncOutcome.RENAME
    assert entry.name_change == ("TV Sector", "TV")


def test_membership_set_reconciles_adds_and_removals() -> None:
    plan = _plan(
        snapshot=[_snapshot(member_channel_ids=(CH_B,))],
        local=[_local(channel_ids=(CH_A,))],
    )
    entry = plan.entries[0]
    assert entry.outcome is GroupSyncOutcome.MEMBERS_CHANGED
    assert entry.members_added == (CH_B,)
    assert entry.members_removed == (CH_A,)


def test_group_absent_upstream_plans_deactivate() -> None:
    plan = _plan(snapshot=[], local=[_local()])
    entry = plan.entries[0]
    assert entry.outcome is GroupSyncOutcome.DEACTIVATE
    assert entry.active_change == (True, False)


def test_inactive_group_reappearing_plans_reactivate() -> None:
    plan = _plan(snapshot=[_snapshot()], local=[_local(active=False)])
    entry = plan.entries[0]
    assert entry.outcome is GroupSyncOutcome.REACTIVATE
    assert entry.active_change == (False, True)


def test_reactivate_dominates_rename_and_members_but_carries_both() -> None:
    plan = _plan(
        snapshot=[_snapshot(title="TV", member_channel_ids=(CH_B,))],
        local=[_local(active=False, channel_ids=(CH_A,))],
    )
    entry = plan.entries[0]
    assert entry.outcome is GroupSyncOutcome.REACTIVATE
    assert entry.name_change == ("TV Sector", "TV")
    assert entry.members_added == (CH_B,)
    assert entry.members_removed == (CH_A,)


def test_rename_dominates_members_changed() -> None:
    plan = _plan(
        snapshot=[_snapshot(title="TV", member_channel_ids=(CH_A, CH_B))],
        local=[_local()],
    )
    assert plan.entries[0].outcome is GroupSyncOutcome.RENAME
    assert plan.entries[0].members_added == (CH_B,)


def test_unknown_channels_are_skipped_and_counted() -> None:
    plan = _plan(snapshot=[_snapshot(member_channel_ids=(CH_A, CH_UNKNOWN))])
    entry = plan.entries[0]
    assert entry.members_added == (CH_A,)
    assert entry.unknown_channel_ids == (CH_UNKNOWN,)
    assert plan.unknown_channel_total == 1


def test_unknown_channel_never_causes_removal_churn() -> None:
    # Upstream has an unknown member; local group already mirrors the known set.
    plan = _plan(
        snapshot=[_snapshot(member_channel_ids=(CH_A, CH_UNKNOWN))],
        local=[_local(channel_ids=(CH_A,))],
    )
    assert plan.entries[0].outcome is GroupSyncOutcome.UNCHANGED


def test_deactivated_group_absent_upstream_is_unchanged() -> None:
    plan = _plan(snapshot=[], local=[_local(active=False)])
    assert plan.entries[0].outcome is GroupSyncOutcome.UNCHANGED


def test_manual_groups_are_invisible() -> None:
    # plan_group_sync receives synced groups only; guard that a None key raises.
    import pytest

    with pytest.raises(ValueError):
        _plan(snapshot=[], local=[_local(cms_group_id=None)])


def test_non_channel_members_are_totalled() -> None:
    plan = _plan(
        snapshot=[
            _snapshot(non_channel_member_count=2),
            _snapshot(cms_group_id="g2", title="News", non_channel_member_count=1),
        ]
    )
    assert plan.non_channel_member_count == 3


def test_entries_sorted_by_cms_group_id() -> None:
    plan = _plan(snapshot=[_snapshot(cms_group_id="g2", title="B"), _snapshot()])
    assert [entry.cms_group_id for entry in plan.entries] == ["g1", "g2"]


def test_counts_cover_every_outcome_key() -> None:
    plan = _plan(snapshot=[_snapshot()])
    assert set(plan.counts) == {
        "CREATE",
        "RENAME",
        "MEMBERS_CHANGED",
        "DEACTIVATE",
        "REACTIVATE",
        "UNCHANGED",
    }
