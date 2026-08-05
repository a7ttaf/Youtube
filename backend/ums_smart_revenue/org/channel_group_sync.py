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
    # The CMS key exists locally under a DIFFERENT content owner. The scoped
    # read cannot return that row (it is owner-OR-NULL), so without this the
    # key looks absent and plans as CREATE — and the apply then collides with
    # the tenant-wide unique cms_group_id and 409s on a preview that said the
    # sync was safe. Nothing is executed for a CONFLICT entry.
    CONFLICT = "CONFLICT"
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
    # Whether this key is in the CMS snapshot, which IS the mirrored active
    # state ("YouTube wins": present => active, vanished => inactive). The
    # apply layer takes the target from here rather than from active_change,
    # because active_change is a diff against the PLAN-TIME snapshot and is
    # therefore None for a group that was already active — leaving the write
    # boundary blind to an operator archiving it in the plan-to-apply window
    # (the group API deliberately still permits archiving a synced group).
    # active_change stays the operator-facing "what will visibly flip".
    upstream_present: bool = False
    # True when applying this entry will also backfill content_owner_id on an
    # owner-NULL legacy group. It rides the PLAN so the mandatory dry run
    # previews that write like every other one; the apply re-verifies it under
    # the row lock rather than trusting this snapshot.
    #
    # Divergence from the preview is ONE-DIRECTIONAL: true here can still not
    # adopt (a racer claimed the row first — the apply then fails the sync
    # closed rather than mirroring another owner's group), but false here can
    # never turn into an adoption. content_owner_id is monotonic — update_group
    # treats None as "unchanged", no route clears a stamp, and
    # require_adoptable_owner forbids moving one — so a row that already names
    # an owner at plan time still names that same owner at apply time.
    will_adopt_content_owner: bool = False


@dataclass(frozen=True)
class GroupSyncPlan:
    """Every planned entry plus counts and skipped-member telemetry."""

    entries: tuple[GroupSyncPlanEntry, ...]
    counts: Mapping[str, int]
    unknown_channel_total: int
    non_channel_member_count: int


def _plan_entry_for_upstream_item(
    item: CmsGroupSnapshot,
    *,
    local_by_cms_group_id: Mapping[str, ChannelGroupEntry],
    known_channel_ids: frozenset[str],
    content_owner_id: str | None,
    foreign_owner_group_ids: frozenset[str],
) -> tuple[GroupSyncPlanEntry, int]:
    """Plan one upstream CMS group against its local match, if any.

    Returns the planned entry plus the count of unknown-channel members skipped.
    """
    wanted_known = tuple(
        channel_id for channel_id in item.member_channel_ids if channel_id in known_channel_ids
    )
    unknown = tuple(
        channel_id for channel_id in item.member_channel_ids if channel_id not in known_channel_ids
    )
    if item.cms_group_id in foreign_owner_group_ids:
        # Held by another owner, so it is absent from the local map and would
        # otherwise plan as CREATE. Surfacing it as CONFLICT is what keeps the
        # mandatory preview honest: the apply cannot create this key.
        return (
            GroupSyncPlanEntry(
                cms_group_id=item.cms_group_id,
                outcome=GroupSyncOutcome.CONFLICT,
                title=item.title,
                local_group_id=None,
                name_change=None,
                active_change=None,
                members_added=(),
                members_removed=(),
                unknown_channel_ids=unknown,
                upstream_present=True,
            ),
            len(unknown),
        )
    local = local_by_cms_group_id.get(item.cms_group_id)
    if local is None:
        return (
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
                upstream_present=True,
            ),
            len(unknown),
        )
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
    return (
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
            upstream_present=True,
            # Matching this owner's upstream key is what proves ownership, so
            # an owner-NULL local match will be stamped at apply.
            will_adopt_content_owner=(
                content_owner_id is not None and local.content_owner_id is None
            ),
        ),
        len(unknown),
    )


def _plan_entries_for_vanished_groups(
    local_groups: tuple[ChannelGroupEntry, ...],
    *,
    upstream_keys: set[str],
    content_owner_id: str | None,
) -> list[GroupSyncPlanEntry]:
    """Plan DEACTIVATE/UNCHANGED entries for local groups no longer upstream.

    Only groups this owner DEFINITIVELY owns are retired. An owner-NULL group
    (created before content_owner_id existed, or by an older import) is still
    passed in for key matching — the tenant-wide unique cms_group_id means
    hiding it would make an existing key plan as CREATE and collide — but it
    must not be deactivated here, because this sync cannot prove it owns it.
    """
    entries: list[GroupSyncPlanEntry] = []
    for group in sorted(local_groups, key=lambda entry: str(entry.cms_group_id)):
        if group.cms_group_id in upstream_keys:
            continue
        if content_owner_id is not None and group.content_owner_id != content_owner_id:
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
                upstream_present=False,
            )
        )
    return entries


# ============================================================================
# Purpose: The CMS group-sync planner — diff one content owner's YouTube CMS
#   snapshot against the local synced groups into a per-group plan the apply
#   layer executes. Full mirror, YouTube wins.
# Database/ORM: None. Pure function, no I/O, no session; the caller supplies
#   both sides and executes the result.
# Standards: Deterministic ordering by cms_group_id; one dominant outcome per
#   group (activation > rename > membership); unknown channel members are
#   surfaced, never invented (channel creation belongs to POST
#   /channels/import). DEACTIVATE is gated on a definitive content_owner_id
#   match, so an owner-NULL legacy row stays matchable — required, because
#   (tenant_id, cms_group_id) is unique tenant-wide and hiding it would make
#   an existing key plan as CREATE — without being retired by an owner that
#   cannot claim it. A manual (cms_group_id=None) group is a caller bug and
#   raises rather than being silently mirrored.
# Blast Radius: Channel-group naming/membership/active state only. No finance
#   totals; group-scope rollups change composition only as the CMS does.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> sync route caller.
#   - File: backend/ums_smart_revenue/org/channel_group_sync_apply.py ->
#     apply_group_sync executes every entry and audits what it wrote.
# ============================================================================
def plan_group_sync(
    *,
    snapshot: tuple[CmsGroupSnapshot, ...],
    local_groups: tuple[ChannelGroupEntry, ...],
    known_channel_ids: frozenset[str],
    content_owner_id: str | None = None,
    foreign_owner_group_ids: frozenset[str] = frozenset(),
) -> GroupSyncPlan:
    """Diff the CMS snapshot against local synced groups into a plan.

    ``content_owner_id`` gates DEACTIVATE only: every supplied group is matched
    against upstream keys, but a group this owner cannot be proven to own (an
    owner-NULL legacy row) is never retired. See
    ``_plan_entries_for_vanished_groups``.

    ``foreign_owner_group_ids`` carries the upstream keys that already exist
    locally under a DIFFERENT owner. ``local_groups`` cannot contain them (the
    caller's read is owner-OR-NULL), so they would otherwise plan as CREATE and
    then collide on the tenant-wide unique key at apply. They plan as CONFLICT
    instead, which is what stops the mandatory dry run from calling an
    unappliable sync safe.
    """
    local_by_cms_group_id: dict[str, ChannelGroupEntry] = {}
    for group in local_groups:
        if group.cms_group_id is None:
            raise ValueError(f"manual group passed to sync planner: {group.id}")
        local_by_cms_group_id[group.cms_group_id] = group
    upstream_keys = {item.cms_group_id for item in snapshot}

    entries: list[GroupSyncPlanEntry] = []
    unknown_total = 0
    non_channel_total = 0

    for item in sorted(snapshot, key=lambda entry: entry.cms_group_id):
        non_channel_total += item.non_channel_member_count
        entry, unknown_count = _plan_entry_for_upstream_item(
            item,
            local_by_cms_group_id=local_by_cms_group_id,
            known_channel_ids=known_channel_ids,
            content_owner_id=content_owner_id,
            foreign_owner_group_ids=foreign_owner_group_ids,
        )
        unknown_total += unknown_count
        entries.append(entry)

    entries.extend(
        _plan_entries_for_vanished_groups(
            local_groups,
            upstream_keys=upstream_keys,
            content_owner_id=content_owner_id,
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
