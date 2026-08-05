# ============================================================================
# Purpose: Pure planning for CMS group sync. Diffs a YouTube CMS snapshot
#   against local synced groups into per-group outcomes the apply layer
#   executes. Full mirror, YouTube wins: renames overwrite, membership is
#   set-reconciled with removals, vanished groups deactivate, reappearing
#   keys reactivate their original local group.
# Database/ORM: None. No I/O, no session.
# Standards: Frozen dataclasses; deterministic ordering (cms_group_id);
#   unknown channels are skipped and surfaced, never created here — channel
#   creation belongs to POST /channels/import and its cms_status contract.
# Blast Radius: Channel-group naming/membership/active state only. No finance
#   totals; group-scope rollups change composition only as the CMS does.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_group_sync_apply.py.
#   - File: backend/ums_smart_revenue/connectors/google/youtube_groups_client.py.
# ============================================================================
"""Pure planning for CMS group sync."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from ums_smart_revenue.org.channel_groups import ChannelGroupEntry


class GroupSyncOutcome(StrEnum):
    """Dominant label for what sync will do with one CMS group key."""

    CREATE = "CREATE"
    RENAME = "RENAME"
    MEMBERS_CHANGED = "MEMBERS_CHANGED"
    DEACTIVATE = "DEACTIVATE"
    REACTIVATE = "REACTIVATE"
    UNCHANGED = "UNCHANGED"


@dataclass(frozen=True)
class CmsGroupSnapshot:
    """One CMS group as fetched: identity, title, and channel members."""

    cms_group_id: str
    title: str
    member_channel_ids: tuple[str, ...]
    non_channel_member_count: int


@dataclass(frozen=True)
class GroupSyncPlanEntry:
    """Planned changes for one CMS group key (full diff, dominant outcome)."""

    cms_group_id: str
    outcome: GroupSyncOutcome
    title: str | None
    local_group_id: str | None
    name_change: tuple[str, str] | None
    active_change: tuple[bool, bool] | None
    members_added: tuple[str, ...]
    members_removed: tuple[str, ...]
    unknown_channel_ids: tuple[str, ...]


@dataclass(frozen=True)
class GroupSyncPlan:
    """Every planned entry plus counts and skipped-member telemetry."""

    entries: tuple[GroupSyncPlanEntry, ...]
    counts: Mapping[str, int]
    unknown_channel_total: int
    non_channel_member_count: int


def plan_group_sync(
    *,
    snapshot: tuple[CmsGroupSnapshot, ...],
    local_groups: tuple[ChannelGroupEntry, ...],
    known_channel_ids: frozenset[str],
) -> GroupSyncPlan:
    """Diff the CMS snapshot against local synced groups into a plan."""
    for group in local_groups:
        if group.cms_group_id is None:
            raise ValueError(f"manual group passed to sync planner: {group.id}")
    local_by_key = {group.cms_group_id: group for group in local_groups}
    upstream_keys = {item.cms_group_id for item in snapshot}

    entries: list[GroupSyncPlanEntry] = []
    unknown_total = 0
    non_channel_total = 0

    for item in sorted(snapshot, key=lambda entry: entry.cms_group_id):
        non_channel_total += item.non_channel_member_count
        wanted_known = tuple(
            channel_id for channel_id in item.member_channel_ids if channel_id in known_channel_ids
        )
        unknown = tuple(
            channel_id
            for channel_id in item.member_channel_ids
            if channel_id not in known_channel_ids
        )
        unknown_total += len(unknown)
        local = local_by_key.get(item.cms_group_id)
        if local is None:
            entries.append(
                GroupSyncPlanEntry(
                    cms_group_id=item.cms_group_id,
                    outcome=GroupSyncOutcome.CREATE,
                    title=item.title,
                    local_group_id=None,
                    name_change=None,
                    active_change=None,
                    members_added=wanted_known,
                    members_removed=(),
                    unknown_channel_ids=unknown,
                )
            )
            continue
        name_change = (local.name, item.title) if local.name != item.title else None
        active_change = (False, True) if not local.active else None
        current = set(local.channel_ids)
        wanted = set(wanted_known)
        added = tuple(sorted(wanted - current))
        removed = tuple(sorted(current - wanted))
        if active_change:
            outcome = GroupSyncOutcome.REACTIVATE
        elif name_change:
            outcome = GroupSyncOutcome.RENAME
        elif added or removed:
            outcome = GroupSyncOutcome.MEMBERS_CHANGED
        else:
            outcome = GroupSyncOutcome.UNCHANGED
        entries.append(
            GroupSyncPlanEntry(
                cms_group_id=item.cms_group_id,
                outcome=outcome,
                title=item.title,
                local_group_id=local.id,
                name_change=name_change,
                active_change=active_change,
                members_added=added,
                members_removed=removed,
                unknown_channel_ids=unknown,
            )
        )

    for group in sorted(local_groups, key=lambda entry: str(entry.cms_group_id)):
        if group.cms_group_id in upstream_keys:
            continue
        outcome = GroupSyncOutcome.DEACTIVATE if group.active else GroupSyncOutcome.UNCHANGED
        entries.append(
            GroupSyncPlanEntry(
                cms_group_id=str(group.cms_group_id),
                outcome=outcome,
                title=None,
                local_group_id=group.id,
                name_change=None,
                active_change=(True, False) if group.active else None,
                members_added=(),
                members_removed=(),
                unknown_channel_ids=(),
            )
        )

    entries.sort(key=lambda entry: entry.cms_group_id)
    counts = {outcome.value: 0 for outcome in GroupSyncOutcome}
    for entry in entries:
        counts[entry.outcome.value] += 1
    return GroupSyncPlan(
        entries=tuple(entries),
        counts=MappingProxyType(counts),
        unknown_channel_total=unknown_total,
        non_channel_member_count=non_channel_total,
    )
