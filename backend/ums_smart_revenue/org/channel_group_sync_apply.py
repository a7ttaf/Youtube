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

from dataclasses import dataclass

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.org.channel_group_sync import (
    GroupSyncOutcome,
    GroupSyncPlan,
    GroupSyncPlanEntry,
)
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry, ChannelGroupRegistryStore

# Provenance marker on every audit record this module writes: it is how an
# auditor separates CMS-mirror changes from manual group API edits and from the
# bulk import's own group mutations (AUDIT_SOURCE_BULK_IMPORT).
AUDIT_SOURCE_CMS_SYNC = "cms_group_sync"


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
) -> dict[str, int]:
    """Execute every non-UNCHANGED entry; return actual counts by outcome.

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
    """
    # Counted at the WRITE BOUNDARY, never from plan.counts (the same rule the
    # bulk import's CHANNEL_IMPORTED summary follows): the plan is a snapshot of
    # the fetch, this dict is the record of what the store actually executed.
    executed = {outcome.value: 0 for outcome in GroupSyncOutcome}
    for entry in plan.entries:
        if entry.outcome is GroupSyncOutcome.UNCHANGED:
            executed[entry.outcome.value] += 1
            continue
        applied: _AppliedChange | None
        if entry.outcome is GroupSyncOutcome.CREATE:
            applied = _execute_create(entry, groups=groups, content_owner_id=content_owner_id)
        else:
            applied = _execute_update(entry, groups=groups)
        if applied is None:
            # The plan's change was already true at the write boundary — a
            # concurrent writer got there first. Count it as UNCHANGED and
            # emit no audit event rather than claiming a mutation this
            # request did not make.
            executed[GroupSyncOutcome.UNCHANGED.value] += 1
            continue
        executed[entry.outcome.value] += 1
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
                "outcome": entry.outcome.value,
                "name_change": list(applied.name_change) if applied.name_change else None,
                "active_change": list(applied.active_change) if applied.active_change else None,
                "members_added": len(applied.members_added),
                "members_removed": len(applied.members_removed),
            },
        )
    return executed


@dataclass(frozen=True)
class _AppliedChange:
    """What one entry's store writes ACTUALLY changed, for the audit record."""

    group_id: str
    name_change: tuple[str, str] | None
    active_change: tuple[bool, bool] | None
    members_added: tuple[str, ...]
    members_removed: tuple[str, ...]


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
    active = _still_pending(
        entry.active_change[1] if entry.active_change else None,
        current.active,
    )
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
) -> _AppliedChange | None:
    """Apply a non-CREATE entry, or return None when nothing was left to do.

    Diffs the plan against the group's CURRENT state rather than the pre-apply
    snapshot: a concurrent writer (another sync, or the bulk import) can race
    this group between planning and here, and the store's writes are
    individually idempotent no-ops in that case (``add_members`` skips present
    members, ``remove_member`` deletes zero rows). Reporting only what this
    call actually changed keeps the audit trail and the returned counts honest.
    """
    group_id = _require_local_group_id(entry)
    current = groups.get_group(group_id)
    if current is None:
        raise ValueError(f"sync plan entry's local group vanished before apply: {group_id}")
    pending = _resolve_pending_change(entry, current)
    if pending is None:
        return None
    if pending.name is not None or pending.active is not None:
        groups.update_group(group_id=group_id, name=pending.name, active=pending.active)
    if pending.members_added:
        groups.add_members(group_id=group_id, channel_ids=list(pending.members_added))
    for channel_id in pending.members_removed:
        groups.remove_member(group_id=group_id, channel_id=channel_id)
    return _AppliedChange(
        group_id=group_id,
        name_change=(current.name, pending.name) if pending.name is not None else None,
        active_change=(current.active, pending.active) if pending.active is not None else None,
        members_added=pending.members_added,
        members_removed=pending.members_removed,
    )


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
