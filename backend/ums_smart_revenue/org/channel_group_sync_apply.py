# ============================================================================
# Purpose: Execute a CMS group-sync plan through the channel-group store and
#   audit every changed group. Mirrors channel_import_apply's split: the route
#   stays thin; writes and per-item audit live here; the route's GROUPS_SYNCED
#   summary uses the ACTUAL counts this module returns, never the plan's
#   (a plan is a snapshot; the write boundary is the record).
# Database/ORM: ChannelGroupORM + ChannelGroupMemberORM via
#   ChannelGroupRegistryStore, inside the caller's single tenant transaction.
# Standards: One GROUP_UPDATED audit per changed group (reason required);
#   UNCHANGED performs no write and no audit; fail on first store error and
#   let the transaction roll everything back.
# Blast Radius: Group naming/membership/active state and audit rows.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> sync route.
#   - File: backend/ums_smart_revenue/org/channel_group_sync.py -> plan types.
# ============================================================================
"""Apply a CMS group-sync plan and audit each changed group."""

from collections.abc import Mapping
from dataclasses import dataclass

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.org.channel_group_sync import (
    CmsGroupSnapshot,
    GroupSyncOutcome,
    GroupSyncPlan,
    GroupSyncPlanEntry,
    plan_group_sync,
)
from ums_smart_revenue.org.channel_groups import (
    ChannelGroupEntry,
    ChannelGroupOwnerReassignmentError,
    ChannelGroupRegistryStore,
)
from ums_smart_revenue.org.channel_registry import ChannelRegistryStore

# Provenance marker on every audit record this module writes: it is how an
# auditor separates CMS-mirror changes from manual group API edits and from the
# bulk import's own group mutations (AUDIT_SOURCE_BULK_IMPORT).
AUDIT_SOURCE_CMS_SYNC = "cms_group_sync"


def _known_member_channel_ids(
    registry: ChannelRegistryStore,
    *,
    member_ids: set[str],
    content_owner_id: str,
) -> frozenset[str]:
    """CMS members this sync may attach: registered, and not another owner's.

    ``include_inactive=True`` because an archived-but-still-existing channel is
    known, not unknown. The groups API's own authorization deliberately counts
    the full member set (active + inactive) when deciding scope access, so a
    sync must not silently strip an inactive channel's membership just because
    an active-only filter made it look absent.

    The owner filter is OR-NULL, mirroring ``list_synced_groups``: a channel
    stamped to a DIFFERENT content owner is excluded, an unstamped legacy row
    is not. Without the exclusion, a channel still registered to owner B that
    appears in owner A's CMS snapshot would be attached to A's synced group,
    bypassing the import's content_owner_id contract and pulling B's channel
    into A's group-scope finance reads. Excluded ids are not dropped silently —
    they fall through to the plan's ``unknown_channel_ids``, the same "surface
    it, never invent it" contract unregistered members already get, and the
    operator repairs the registry with ``PATCH /channels/{id}/content-owner``.

    Keeping NULL attachable matters as much as excluding the mismatch: every
    channel predating the content-owner stamp is NULL, and a strict equality
    filter would make a whole tenant's roster look unknown and strip its
    memberships on the first sync.
    """
    return frozenset(
        entry.youtube_channel_id
        for entry in registry.list_channels_by_ids(member_ids, include_inactive=True)
        if entry.content_owner_id in (None, content_owner_id)
    )


# ============================================================================
# Purpose: Domain-side planning entry point for CMS group sync — gather the
#   store state one content owner's snapshot diffs against, and delegate the
#   pure diffing to plan_group_sync.
# Database/ORM: ChannelGroupORM via ChannelGroupRegistryStore
#   (list_synced_groups, list_foreign_owner_cms_group_ids) and
#   YouTubeChannelORM via ChannelRegistryStore (list_channels_by_ids).
#   Read-only, one bulk query per store, never per-row lookups.
# Standards: Layer ownership — data access lives HERE, not in the HTTP route,
#   which keeps only authz, validation, and error translation. Mirrors
#   plan_channel_import_with_stores exactly.
# Blast Radius: Sync planning outcomes, and therefore every apply-time write
#   decision. No writes of its own.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> sync route calls this.
#   - File: backend/ums_smart_revenue/org/channel_group_sync.py -> pure core.
# ============================================================================
def plan_group_sync_with_stores(
    snapshot: tuple[CmsGroupSnapshot, ...],
    *,
    registry: ChannelRegistryStore,
    groups: ChannelGroupRegistryStore,
    content_owner_id: str,
) -> GroupSyncPlan:
    """Gather store state and build the sync plan for one CMS snapshot.

    The group read is scoped to THIS content owner: ownership comes from the
    create-time stamp, and without the filter syncing owner A would see every
    OTHER owner's synced groups — none of which can be upstream in A's
    snapshot, so the planner would retire them as vanished. It deliberately
    still returns owner-NULL legacy rows, because (tenant_id, cms_group_id) is
    unique tenant-wide and hiding an existing key would make it plan as CREATE
    and collide; the planner's own gate is what keeps those unowned rows from
    being deactivated.

    That same OR-NULL scoping is why the foreign-owner lookup exists: a key
    another owner holds is INVISIBLE to the scoped read, so it would plan as
    CREATE and then collide at apply, 409ing a sync the mandatory preview had
    called safe. Classified here instead, from stored state.
    """
    local_groups = tuple(groups.list_synced_groups(content_owner_id=content_owner_id))
    member_ids = {cid for item in snapshot for cid in item.member_channel_ids}
    return plan_group_sync(
        snapshot=snapshot,
        local_groups=local_groups,
        known_channel_ids=_known_member_channel_ids(
            registry, member_ids=member_ids, content_owner_id=content_owner_id
        ),
        content_owner_id=content_owner_id,
        foreign_owner_group_ids=frozenset(
            groups.list_foreign_owner_cms_group_ids(
                {item.cms_group_id for item in snapshot},
                content_owner_id=content_owner_id,
            )
        ),
    )


@dataclass(frozen=True)
class GroupSyncAppliedEntry:
    """One group's ACTUAL result at the write boundary.

    The route renders an apply's per-group response from these rather than
    from the plan. Both must say the same thing: a plan-rendered response can
    show RENAME with a full diff for a group whose rename a concurrent writer
    already landed, while the audit trail correctly recorded UNCHANGED and no
    GROUP_UPDATED — the operator would be told this request changed something
    it never touched.
    """

    cms_group_id: str
    outcome: GroupSyncOutcome
    title: str | None
    local_group_id: str | None
    name_change: tuple[str, str] | None
    active_change: tuple[bool, bool] | None
    members_added: tuple[str, ...]
    members_removed: tuple[str, ...]
    unknown_channel_ids: tuple[str, ...]
    adopted_content_owner: bool


@dataclass(frozen=True)
class GroupSyncExecution:
    """The write boundary's record: per-group results plus their tally."""

    counts: Mapping[str, int]
    entries: tuple[GroupSyncAppliedEntry, ...]


# ============================================================================
# Purpose: Execute every non-UNCHANGED entry of a CMS group-sync plan —
#   creations, renames, activation flips, and membership reconciliation — and
#   record one GROUP_UPDATED audit event per changed group.
# Database/ORM: ChannelGroupORM (create_group / update_group) and
#   ChannelGroupMemberORM (add_members / remove_member) via
#   ChannelGroupRegistryStore; AuditLogORM rows via the supplied AuditSink.
# Standards: All-or-nothing — the caller wires the store and the sink to ONE
#   transaction, so the first store error rolls back every group written so far
#   together with its audit rows. UNCHANGED entries perform no write and emit no
#   audit event, but still count: the returned tally is what the route's
#   GROUPS_SYNCED summary persists, and it is accumulated HERE at the write
#   boundary rather than copied from plan.counts, so the summary can never claim
#   a rename no GROUP_UPDATED event backs.
# Blast Radius: Group naming/membership/active state and the audit trail. No
#   channel rows are created (unknown CMS members are surfaced by the planner,
#   never invented here); no revenue totals, no allocation, no month-close.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_group_sync.py -> the planner
#     whose entries this executes.
#   - File: backend/ums_smart_revenue/api/channels.py -> the sync route that
#     authorizes, plans, calls this, and writes the GROUPS_SYNCED summary.
# ============================================================================
def apply_group_sync(
    plan: GroupSyncPlan,
    *,
    groups: ChannelGroupRegistryStore,
    audit_sink: AuditSink,
    actor: UserPrincipal,
    scope: AccessScope,
    content_owner_id: str,
    reason: str,
) -> GroupSyncExecution:
    """Execute every non-UNCHANGED entry; return what actually happened.

    CREATE lands a new SECTOR group carrying the CMS key. Every other changed
    outcome updates the matched local group: name and active state go through a
    single ``update_group`` call, and only when one of them actually changes —
    a MEMBERS_CHANGED entry must not rewrite a name the CMS never touched.
    Membership is then reconciled additions-first, removals per channel, so a
    REACTIVATE carrying a rename and member churn still resolves to ONE audit
    event for the group rather than one per store call.

    UNCHANGED entries return early: no store write and no audit event, because
    an audit row for a group nothing touched would make the trail claim a
    mutation that did not occur. They are still counted — the route's
    GROUPS_SYNCED summary reports the whole mirror, not just its deltas.

    The governing rule is not "UNCHANGED writes nothing" but the stricter and
    more general one it was reaching for: **no executed write means no audit
    row, and every executed write is audited.** Adopting an owner-NULL legacy
    group is a real write even when the mirror itself is already in sync, so it
    happens AND is audited (``adopted_content_owner``) rather than being
    applied silently; the outcome label still reports the mirror change, which
    for an otherwise-in-sync group is UNCHANGED. The planner marks that write
    in advance (``will_adopt_content_owner``) so the mandatory dry run previews
    it like any other, and this layer re-verifies it under the row lock rather
    than trusting the plan.
    """
    # Counted at the WRITE BOUNDARY, never from plan.counts (the same rule the
    # bulk import's CHANNEL_IMPORTED summary follows): the plan is a snapshot of
    # the fetch, this dict is the record of what the store actually executed.
    executed = {outcome.value: 0 for outcome in GroupSyncOutcome}
    results: list[GroupSyncAppliedEntry] = []
    for entry in plan.entries:
        # UNCHANGED is NOT short-circuited here. An owner-NULL legacy group
        # that already mirrors its CMS group plans as UNCHANGED, and that is
        # precisely the case whose owner stamp is still owed — skipping it
        # would leave the row unclaimable forever, so the deactivation gate
        # could never retire it once it vanishes upstream. _execute_update
        # returns None for a genuinely untouched group, which still counts as
        # UNCHANGED with no write and no audit.
        applied: _AppliedChange | None
        if entry.outcome is GroupSyncOutcome.CREATE:
            applied = _execute_create(entry, groups=groups, content_owner_id=content_owner_id)
        else:
            applied = _execute_update(entry, groups=groups, content_owner_id=content_owner_id)
        if applied is None:
            # The plan's change was already true at the write boundary — a
            # concurrent writer got there first. Count it as UNCHANGED and
            # emit no audit event rather than claiming a mutation this
            # request did not make.
            executed[GroupSyncOutcome.UNCHANGED.value] += 1
            results.append(_unchanged_result(entry))
            continue
        # Label from what was WRITTEN, not what was planned. A partial race —
        # another writer already flipped active or the name, leaving only
        # membership for this request — would otherwise have the count and the
        # audit row claim REACTIVATE/RENAME/DEACTIVATE for a request that only
        # moved members.
        outcome = _effective_outcome(entry, applied)
        executed[outcome.value] += 1
        results.append(
            GroupSyncAppliedEntry(
                cms_group_id=entry.cms_group_id,
                outcome=outcome,
                title=entry.title,
                local_group_id=applied.group_id,
                name_change=applied.name_change,
                active_change=applied.active_change,
                members_added=applied.members_added,
                members_removed=applied.members_removed,
                # Fetch telemetry, not a write: still true whatever the store did.
                unknown_channel_ids=entry.unknown_channel_ids,
                adopted_content_owner=applied.adopted_content_owner,
            )
        )
        record_audit_event(
            sink=audit_sink,
            actor=actor,
            event_type=AuditEventType.GROUP_UPDATED,
            entity_type="channel_group",
            entity_id=applied.group_id,
            scope=scope,
            reason=reason,
            details={
                "source": AUDIT_SOURCE_CMS_SYNC,
                "content_owner_id": content_owner_id,
                "cms_group_id": entry.cms_group_id,
                "outcome": outcome.value,
                "name_change": list(applied.name_change) if applied.name_change else None,
                "active_change": list(applied.active_change) if applied.active_change else None,
                "members_added": len(applied.members_added),
                "members_removed": len(applied.members_removed),
                "adopted_content_owner": applied.adopted_content_owner,
            },
        )
    return GroupSyncExecution(counts=executed, entries=tuple(results))


def _unchanged_result(entry: GroupSyncPlanEntry) -> GroupSyncAppliedEntry:
    """Render an entry that wrote nothing: no diffs, whatever the plan said."""
    return GroupSyncAppliedEntry(
        cms_group_id=entry.cms_group_id,
        outcome=GroupSyncOutcome.UNCHANGED,
        title=entry.title,
        local_group_id=entry.local_group_id,
        name_change=None,
        active_change=None,
        members_added=(),
        members_removed=(),
        unknown_channel_ids=entry.unknown_channel_ids,
        adopted_content_owner=False,
    )


@dataclass(frozen=True)
class _AppliedChange:
    """What one entry's store writes ACTUALLY changed, for the audit record."""

    group_id: str
    name_change: tuple[str, str] | None
    active_change: tuple[bool, bool] | None
    members_added: tuple[str, ...]
    members_removed: tuple[str, ...]
    adopted_content_owner: bool = False


def _execute_create(
    entry: GroupSyncPlanEntry,
    *,
    groups: ChannelGroupRegistryStore,
    content_owner_id: str,
) -> _AppliedChange:
    """Create the group carrying this CMS key and report what it wrote."""
    # Falling back to the CMS key keeps the group identifiable when YouTube
    # reports an empty title; the next sync renames it.
    created = groups.create_group(
        name=entry.title or entry.cms_group_id,
        group_type="SECTOR",
        channel_ids=list(entry.members_added),
        cms_group_id=entry.cms_group_id,
        content_owner_id=content_owner_id,
    )
    return _AppliedChange(
        group_id=created.id,
        name_change=entry.name_change,
        active_change=entry.active_change,
        members_added=entry.members_added,
        members_removed=entry.members_removed,
    )


def _effective_outcome(entry: GroupSyncPlanEntry, applied: _AppliedChange) -> GroupSyncOutcome:
    """Return the outcome this apply ACTUALLY produced, not the planned one.

    Mirrors the planner's dominance order (activation > rename > membership)
    but reads it off the executed change, so a plan whose rename or activation
    a concurrent writer already landed is reported as the MEMBERS_CHANGED it
    really was. CREATE is definitionally what it did.
    """
    if entry.outcome is GroupSyncOutcome.CREATE:
        return GroupSyncOutcome.CREATE
    if applied.active_change is not None:
        _, became_active = applied.active_change
        return GroupSyncOutcome.REACTIVATE if became_active else GroupSyncOutcome.DEACTIVATE
    if applied.name_change is not None:
        return GroupSyncOutcome.RENAME
    if applied.members_added or applied.members_removed:
        return GroupSyncOutcome.MEMBERS_CHANGED
    return GroupSyncOutcome.UNCHANGED


@dataclass(frozen=True)
class _PendingChange:
    """The part of a planned entry that is still not true in the store."""

    name: str | None
    active: bool | None
    members_added: tuple[str, ...]
    members_removed: tuple[str, ...]


def _still_pending[T](planned: T | None, current: T) -> T | None:
    """Return the planned value only when it differs from what is stored."""
    if planned is None or planned == current:
        return None
    return planned


def _resolve_pending_change(
    entry: GroupSyncPlanEntry, current: ChannelGroupEntry
) -> _PendingChange | None:
    """Narrow a planned entry to what is STILL undone, or None if all is done."""
    name = _still_pending(
        entry.name_change[1] if entry.name_change else None,
        current.name,
    )
    # Target comes from upstream PRESENCE, not from entry.active_change. The
    # diff is against the plan-time snapshot, so it is None for a group that
    # was already active — which would leave a group archived in the
    # plan-to-apply window inactive despite being present upstream, and the
    # sync would report success on a mirror it did not actually restore.
    active = _still_pending(entry.upstream_present, current.active)
    current_members = set(current.channel_ids)
    members_added = tuple(cid for cid in entry.members_added if cid not in current_members)
    members_removed = tuple(cid for cid in entry.members_removed if cid in current_members)
    if name is None and active is None and not members_added and not members_removed:
        return None
    return _PendingChange(
        name=name,
        active=active,
        members_added=members_added,
        members_removed=members_removed,
    )


def _execute_update(
    entry: GroupSyncPlanEntry,
    *,
    groups: ChannelGroupRegistryStore,
    content_owner_id: str,
) -> _AppliedChange | None:
    """Apply a non-CREATE entry, or return None when nothing was left to do.

    Diffs the plan against the group's CURRENT state rather than the pre-apply
    snapshot: a concurrent writer (another sync, or the bulk import) can race
    this group between planning and here, and the store's writes are
    individually idempotent no-ops in that case (``add_members`` skips present
    members, ``remove_member`` deletes zero rows). Reporting only what this
    call actually changed keeps the audit trail and the returned counts honest.

    The re-read takes ``for_update`` — the parent group row is the membership
    serialization point every membership writer locks. Without it the diff is
    computed against a snapshot a racing writer can still invalidate before
    these writes land, so a loser could perform zero inserts/deletes and STILL
    report members_added/members_removed. Holding the lock across diff-then-
    write makes the reported change the change that actually happened.

    The re-read also re-verifies the entry's SCOPING PREMISE — that the group
    is this owner's or still unclaimed — because the planner matches owner-NULL
    rows on purpose and a rival can claim one before this lock is taken.
    """
    group_id = _require_local_group_id(entry)
    current = groups.get_group(group_id, for_update=True)
    if current is None:
        # Bare ValueError on purpose: this is an invariant breach, not an
        # expected client-visible outcome. There is NO hard-delete path for
        # groups anywhere in the Protocol or the SQL store — they deactivate,
        # and only member rows are ever DELETEd — so a group the planner just
        # listed cannot disappear here except via out-of-band SQL or a store
        # regression. A 500 that rolls the transaction back is the correct
        # response to that. If a hard delete ever ships, this must become a
        # typed error mapped to 409 in the same change.
        raise ValueError(f"sync plan entry's local group vanished before apply: {group_id}")
    # The locked re-read re-verifies the entry's SCOPING PREMISE, not just its
    # mirrored fields. The planner's scoped read deliberately includes
    # owner-NULL rows, so a group that was unclaimed at plan time can be
    # adopted by ANOTHER owner's import before this lock is taken. Declining
    # only the owner stamp is not enough — the rename/membership writes would
    # still land on that owner's group, and the GROUP_UPDATED row would carry
    # THIS owner's content_owner_id on it. Fail the entry closed instead of
    # absorbing it as UNCHANGED: a group whose jurisdiction moved mid-sync is
    # precisely the state a silent no-op would hide from the operator.
    #
    # The message deliberately does NOT say "re-run": retrying cannot clear
    # this. Once the row is stamped to someone else, the scoped read
    # (list_synced_groups is owner-OR-NULL) no longer returns it, so the next
    # plan sees the upstream key with no local match, emits CREATE, and
    # collides on the tenant-unique cms_group_id — a different 409, forever.
    # A wrong stamp has no API remedy today; see the clear-stamp item in
    # Docs/pulls/2026-08-05-pr-tbd-cms-group-sync-report.md.
    if current.content_owner_id not in (None, content_owner_id):
        raise ChannelGroupOwnerReassignmentError(
            f"channel group {group_id} is held by content owner "
            f"{current.content_owner_id!r}; this sync for {content_owner_id!r} cannot "
            "mirror it, and re-running will not release the claim"
        )
    # Matching this owner's upstream key IS the proof of ownership, so an
    # owner-NULL legacy row is adopted here. Without the stamp it stays
    # unclaimable forever: _plan_entries_for_vanished_groups skips owner-NULL
    # rows, so a group that later disappears upstream would never be
    # deactivated and the mirror would silently stop reflecting deletions.
    adopt = current.content_owner_id is None
    pending = _resolve_pending_change(entry, current)
    if pending is None:
        if not adopt:
            return None
        # Nothing to mirror, but the owner stamp is still owed.
        pending = _PendingChange(name=None, active=None, members_added=(), members_removed=())
    _write_pending_change(
        groups,
        group_id=group_id,
        pending=pending,
        adopt_owner=content_owner_id if adopt else None,
    )
    return _AppliedChange(
        group_id=group_id,
        name_change=(current.name, pending.name) if pending.name is not None else None,
        active_change=(current.active, pending.active) if pending.active is not None else None,
        members_added=pending.members_added,
        members_removed=pending.members_removed,
        adopted_content_owner=adopt,
    )


def _write_pending_change(
    groups: ChannelGroupRegistryStore,
    *,
    group_id: str,
    pending: _PendingChange,
    adopt_owner: str | None,
) -> None:
    """Execute one group's pending writes: name/active/owner, then membership."""
    if pending.name is not None or pending.active is not None or adopt_owner is not None:
        groups.update_group(
            group_id=group_id,
            name=pending.name,
            active=pending.active,
            content_owner_id=adopt_owner,
        )
    if pending.members_added:
        groups.add_members(group_id=group_id, channel_ids=list(pending.members_added))
    for channel_id in pending.members_removed:
        groups.remove_member(group_id=group_id, channel_id=channel_id)


def _require_local_group_id(entry: GroupSyncPlanEntry) -> str:
    """Return the entry's local group id, failing closed when it is absent.

    ``plan_group_sync`` only emits a non-CREATE outcome for an entry it matched
    to a local group, so a missing id here means the plan was hand-built or the
    planner regressed. Raising is the fail-closed choice: skipping the entry
    would let the returned tally — which the durable GROUPS_SYNCED summary
    persists — count a rename or deactivation the store never executed.
    """
    if entry.local_group_id is None:
        raise ValueError(
            f"sync plan entry has no local group to apply: {entry.cms_group_id} "
            f"({entry.outcome.value})"
        )
    return entry.local_group_id
