# ============================================================================
# Purpose: Domain-side execution of a bulk channel inventory import plan —
#   registry writes, group-membership reconciliation, and the full audit
#   trail (per-channel, per-group-mutation, and the import summary). Keeps
#   the HTTP route a thin orchestration layer.
# Database/ORM: YouTubeChannelORM via ChannelRegistryStore (create_channel /
#   update_inventory), ChannelGroupORM + membership via
#   ChannelGroupRegistryStore, audit rows via the supplied AuditSink.
# Standards: All-or-nothing relies on the caller wiring every store and the
#   sink to ONE transaction (the import route binds the audit sink to the
#   request's tenant session, platform-lane elevated per append). Every write
#   is audited with the permission that authorized it (MANAGE_CHANNELS via
#   permission_override) and with field-level provenance: the plan's diff,
#   the operator's raw view_revenue token, and group/channel identifiers for
#   membership mutations. Registry errors propagate typed for the route to
#   translate (e.g. ChannelRevenueRequirementLockedMonthError -> 409).
# Blast Radius: Channel registry inventory, channel-group membership, audit
#   trail. Connector ingest targeting via cms_status/content_owner_id. No
#   finance totals, no allocation, no month-close writes.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> POST /channels/import
#     route boundary that authorizes, plans, and calls this.
#   - File: backend/ums_smart_revenue/org/channel_import.py -> pure parse/plan
#     core that produces the plan this executes.
# ============================================================================
"""Execute a bulk channel import plan with registry writes and audit trail."""

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry, ChannelGroupRegistryStore
from ums_smart_revenue.org.channel_import import (
    ChannelImportError,
    ChannelImportOutcome,
    ChannelImportPlan,
    ChannelImportPlanEntry,
    ParsedChannelImport,
    plan_channel_import,
)
from ums_smart_revenue.org.channel_registry import ChannelRegistryEntry, ChannelRegistryStore

_INVENTORY_FIELDS = ("channel_name", "cms_status", "content_owner_id", "revenue_required")


class ChannelImportArchivedGroupError(ChannelImportError):
    """A row's CMS group was archived between planning and the apply write.

    Planning fails archived groups closed, but another request can archive the
    group in the plan-to-apply window; the write boundary re-checks (under a
    row lock) and raises this so the whole import rolls back instead of
    silently mutating a retired group. The route maps it to HTTP 409.
    """


# ============================================================================
# Purpose: Domain-side planning entry point for the bulk import — gathers the
#   store state a roster diffs against and delegates to the pure planner,
#   deciding per row whether channel and finance-scope group mutations may
#   proceed at apply time.
# Database/ORM: YouTubeChannelORM via ChannelRegistryStore
#   (list_channels_by_ids, include_inactive=True) and ChannelGroupORM via
#   ChannelGroupRegistryStore (list_archived_cms_group_ids). Read-only — one
#   bulk query per store, never per-row lookups.
# Standards: Layer ownership — data access lives HERE, not in the HTTP route,
#   which keeps only authz/form/upload/rendering. Archived registry rows and
#   archived CMS groups fail their rows closed at planning (per-row ERROR ->
#   422 before any write); the apply re-checks groups under a row lock for
#   the plan-to-apply window.
# Blast Radius: Import planning outcomes (CREATE/UPDATE/UNCHANGED/ERROR) and
#   therefore every apply-time write decision. No writes of its own.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> import_channels calls
#     this and renders the plan.
#   - File: backend/ums_smart_revenue/org/channel_import.py ->
#     plan_channel_import pure diffing core.
# ============================================================================
def plan_channel_import_with_stores(
    parsed: ParsedChannelImport,
    *,
    registry: ChannelRegistryStore,
    groups: ChannelGroupRegistryStore,
    content_owner_id: str,
    cms_status: str,
) -> ChannelImportPlan:
    """Gather store state and build the import plan for a parsed roster.

    Domain-side planning entry point: performs the registry and group-store
    reads (one bulk query each — never per-row lookups) and delegates the
    pure diffing to ``plan_channel_import``, keeping the HTTP route free of
    data access. ``include_inactive`` matters: an archived registry row must
    surface as a per-row planning error, not be mistaken for absent and
    planned as a CREATE that the duplicate guard turns into a conflict
    mid-apply. Archived CMS groups likewise fail their rows closed at
    planning; attaching members to a retired group would audit a change that
    active listings and finance scope selection never surface.
    """
    wanted = {row.youtube_channel_id for row in parsed.rows}
    existing = {
        entry.youtube_channel_id: entry
        for entry in registry.list_channels_by_ids(wanted, include_inactive=True)
    }
    group_ids = {row.group_id for row in parsed.rows if row.group_id}
    return plan_channel_import(
        rows=parsed.rows,
        errors=parsed.errors,
        existing=existing,
        content_owner_id=content_owner_id,
        cms_status=cms_status,
        archived_group_ids=frozenset(groups.list_archived_cms_group_ids(group_ids)),
    )


# ============================================================================
# Purpose: Execute a bulk channel import plan — per-row registry writes
#   (create_channel / update_inventory), group-membership reconciliation, and
#   the full audit trail (per-channel, per-group-mutation, one summary).
# Database/ORM: YouTubeChannelORM writes via ChannelRegistryStore,
#   ChannelGroupORM + ChannelGroupMemberORM via ChannelGroupRegistryStore,
#   AuditLogORM rows via the supplied AuditSink.
# Standards: All-or-nothing — the caller wires every store and the sink to
#   ONE transaction, so any raised error rolls the whole import back with its
#   audit rows. Write-boundary rechecks over plan trust: audit diffs are
#   rebuilt from what update_inventory actually replaced, and group state is
#   re-read under a row lock. Typed domain errors propagate for the route to
#   translate (locked-month flip, archived group, uniqueness races -> 409).
# Blast Radius: Channel registry inventory, channel-group membership, audit
#   trail, connector ingest targeting via cms_status/content_owner_id. No
#   finance totals, no allocation, no month-close writes.
# Connections:
#   - File: backend/ums_smart_revenue/api/channels.py -> POST /channels/import
#     route boundary that authorizes, plans, and calls this.
#   - File: backend/ums_smart_revenue/org/channel_import.py -> pure plan core
#     whose entries this executes.
# ============================================================================
def apply_channel_import(
    plan: ChannelImportPlan,
    *,
    registry: ChannelRegistryStore,
    groups: ChannelGroupRegistryStore,
    audit_sink: AuditSink,
    actor: UserPrincipal,
    scope: AccessScope,
    content_owner_id: str,
    cms_status: str,
    reason: str,
    filename: str | None,
) -> None:
    """Execute every CREATE/UPDATE row, reconcile groups, and audit everything.

    CREATE rows perform a registry create and an audit event. UPDATE and
    UNCHANGED rows BOTH write through the registry at the write boundary: the
    plan's outcome came from a possibly-stale snapshot, so an UNCHANGED row
    skipped entirely would silently keep a concurrent writer's value instead
    of the authoritative roster's (the file wins). The registry's locked
    re-read returns what was actually replaced; an UNCHANGED row audits a
    CHANNEL_UPDATED event ONLY when that real diff is non-empty, so a truly
    unchanged re-import stays audit-quiet — and so does a planned UPDATE whose
    target values a concurrent writer already committed (the write replaced
    nothing, so recording it would claim a mutation that did not occur). All
    three outcomes still reach
    group-membership reconciliation below — outcomes are computed only from
    inventory fields, so treating UNCHANGED as a no-op would also drop a
    Group_ID column added on re-import. ERROR rows are skipped entirely. One
    CHANNEL_IMPORTED summary event closes the trail.

    Rows are EXECUTED in a deterministic (channel id, group id) order rather
    than CSV order: every row write takes a channel row lock (and every group
    mutation a group row lock) held until commit, so two imports listing the
    same channels in opposite file order would otherwise each hold what the
    other waits for and PostgreSQL would abort one as a deadlock. The response
    payload and per-row numbering still follow the operator's file.
    """
    for entry in _channel_write_order(plan.entries):
        channel_id = entry.youtube_channel_id
        if channel_id is None or entry.channel_name is None:
            continue
        event_type: AuditEventType | None = None
        applied_changes: dict[str, tuple[object, object]] = {}
        if entry.outcome is ChannelImportOutcome.CREATE:
            registry.create_channel(
                youtube_channel_id=channel_id,
                channel_name=entry.channel_name,
                primary_company_id=None,
                cms_status=cms_status,
                revenue_required=bool(entry.revenue_required),
                content_owner_id=content_owner_id,
            )
            event_type = AuditEventType.CHANNEL_CREATED
        elif entry.outcome in (ChannelImportOutcome.UPDATE, ChannelImportOutcome.UNCHANGED):
            # The registry re-reads the row under a lock at the write boundary
            # and returns what it actually replaced; the durable diff below is
            # built from THAT, not the plan's possibly-stale snapshot, so a
            # concurrent committed change cannot be hidden from the trail —
            # and an UNCHANGED classification cannot preserve a concurrent
            # writer's value over the roster's (review #159 r3713841231).
            previous, updated = registry.update_inventory(
                youtube_channel_id=channel_id,
                channel_name=entry.channel_name,
                cms_status=cms_status,
                content_owner_id=content_owner_id,
                revenue_required=bool(entry.revenue_required),
            )
            applied_changes = _entry_changes(previous, updated)
            # The audit rule is the same for planned UPDATE and UNCHANGED:
            # record CHANNEL_UPDATED only when the write-boundary diff is
            # non-empty. A planned UPDATE whose target values a concurrent
            # writer already committed replaces nothing — auditing it would
            # claim a mutation that did not occur (review #159 r3713966806).
            if applied_changes:
                event_type = AuditEventType.CHANNEL_UPDATED
        if event_type is not None:
            record_audit_event(
                sink=audit_sink,
                actor=actor,
                event_type=event_type,
                entity_type="youtube_channel",
                entity_id=channel_id,
                scope=scope,
                reason=reason,
                # The import authorizes on MANAGE_CHANNELS; without the override
                # CHANNEL_UPDATED would be recorded under its definition default
                # (registry.manage_org_mapping) and an auditor filtering by
                # MANAGE_CHANNELS would miss every bulk inventory change.
                # CHANNEL_CREATED's definition already carries MANAGE_CHANNELS,
                # so the override is a no-op there.
                permission_override=Permission.MANAGE_CHANNELS,
                details=_channel_audit_details(
                    entry,
                    content_owner_id=content_owner_id,
                    cms_status=cms_status,
                    applied_changes=applied_changes,
                ),
            )
    # Group membership runs as a SECOND pass, ordered by (group key, channel
    # id): every channel row lock is taken before any group row lock, and both
    # resource classes are visited in a stable order, so overlapping imports
    # can never hold one class while waiting on the other's.
    for entry in _group_write_order(plan.entries):
        channel_id = entry.youtube_channel_id
        if channel_id is None or entry.channel_name is None or not entry.group_id:
            continue
        group_change = _attach_group_membership(
            groups, cms_group_id=entry.group_id, channel_id=channel_id
        )
        # A group creation or membership addition is a finance-scope
        # mutation; without its own GROUP_UPDATED record an inventory-
        # UNCHANGED row's group change would be invisible in the audit
        # trail (the summary event carries only counts).
        if group_change is not None:
            group_action, group = group_change
            record_audit_event(
                sink=audit_sink,
                actor=actor,
                event_type=AuditEventType.GROUP_UPDATED,
                entity_type="channel_group",
                entity_id=group.id,
                scope=scope,
                reason=reason,
                details={
                    "action": group_action,
                    "cms_group_id": entry.group_id,
                    "group_type": group.group_type,
                    "channel_id": channel_id,
                    "source": "bulk_import",
                },
            )
    record_audit_event(
        sink=audit_sink,
        actor=actor,
        event_type=AuditEventType.CHANNEL_IMPORTED,
        entity_type="youtube_channel_import",
        entity_id=content_owner_id,
        scope=scope,
        reason=reason,
        details={
            # The multipart filename is attacker-controlled and lands in
            # audit_logs.details (JSONB); PostgreSQL rejects U+0000 inside a
            # JSON string, so an unsanitized name would roll an otherwise
            # valid import back as an unhandled 500 on this final append.
            "filename": _safe_audit_filename(filename),
            "content_owner_id": content_owner_id,
            "cms_status": cms_status,
            "counts": dict(plan.counts),
        },
    )


def _safe_audit_filename(filename: str | None) -> str | None:
    """Strip NULs from an upload filename so it can persist in JSONB details."""
    if filename is None:
        return None
    return filename.replace("\x00", "")


def _channel_write_order(
    entries: tuple[ChannelImportPlanEntry, ...],
) -> list[ChannelImportPlanEntry]:
    """Order inventory writes by channel id, file order within a channel.

    Channel id first gives overlapping imports a consistent lock order (the
    deadlock this closes); ``row_number`` second keeps a repeated channel's
    first copy — the one owning the CREATE/UPDATE decision — ahead of its
    membership-only copies, which the registry would otherwise reject as an
    update to a channel this import has not created yet.
    """
    return sorted(
        entries,
        key=lambda entry: (entry.youtube_channel_id or "", entry.row_number),
    )


def _group_write_order(
    entries: tuple[ChannelImportPlanEntry, ...],
) -> list[ChannelImportPlanEntry]:
    """Order membership writes by (CMS group key, channel id).

    A stable order over the group rows, taken in a pass that runs strictly
    after every channel write, so two imports never hold a lock of one
    resource class while waiting on the other's.
    """
    return sorted(
        entries,
        key=lambda entry: (entry.group_id or "", entry.youtube_channel_id or ""),
    )


def _channel_audit_details(
    entry: ChannelImportPlanEntry,
    *,
    content_owner_id: str,
    cms_status: str,
    applied_changes: dict[str, tuple[object, object]],
) -> dict[str, object]:
    """Build one channel write's audit details with full field-level provenance.

    ``changes`` carries the {field: {from, to}} diff of what the write
    ACTUALLY replaced — computed from the registry's write-boundary re-read,
    not the plan's snapshot (empty for a CREATE, whose values are all new).
    ``view_revenue_raw`` preserves the operator's original CSV token — None
    means the column was absent and the required-by-default rule applied —
    because the derived boolean alone cannot distinguish an explicit
    permission from the default.
    """
    return {
        "channel_name": entry.channel_name,
        "content_owner_id": content_owner_id,
        "cms_status": cms_status,
        "revenue_required": entry.revenue_required,
        "view_revenue_raw": entry.view_revenue_raw,
        "changes": {
            name: {"from": pair[0], "to": pair[1]} for name, pair in applied_changes.items()
        },
        "source": "bulk_import",
    }


def _entry_changes(
    previous: ChannelRegistryEntry, updated: ChannelRegistryEntry
) -> dict[str, tuple[object, object]]:
    """Diff the four inventory fields between two persisted registry entries."""
    return {
        field: (getattr(previous, field), getattr(updated, field))
        for field in _INVENTORY_FIELDS
        if getattr(previous, field) != getattr(updated, field)
    }


# ============================================================================
# Purpose: Reconcile one import row's CMS group membership — resolve the group
#   by its CMS key, create it when absent, and attach the channel — returning
#   the mutation performed so the caller can audit it.
# Database/ORM: ChannelGroupORM (row-locked read via get_group_by_cms_id
#   for_update=True; INSERT via create_group) and ChannelGroupMemberORM
#   (INSERT via add_members). No channel or finance writes.
# Standards: Write-boundary recheck over plan trust — the locked read
#   re-examines `active` and raises ChannelImportArchivedGroupError (route:
#   409) when a group was archived in the plan-to-apply window, so the race
#   fails the whole transaction closed rather than mutating a retired group.
#   The parent group row is the membership serialization point every writer
#   shares (FOR NO KEY UPDATE, compatible with membership FK key-share locks).
#   Returns None when membership already existed so a no-op is never audited.
# Blast Radius: Channel-group membership and therefore finance group-scope
#   selection and rollups; the GROUP_UPDATED audit trail. No revenue totals,
#   no allocation, no month-close.
# Connections:
#   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the locked
#     lookup, typed uniqueness conflict, and membership writers.
#   - File: backend/ums_smart_revenue/api/channels.py -> maps the archived and
#     conflict errors to 409.
# ============================================================================
def _attach_group_membership(
    groups: ChannelGroupRegistryStore, *, cms_group_id: str, channel_id: str
) -> tuple[str, ChannelGroupEntry] | None:
    """Ensure the channel belongs to the group carrying this CMS key.

    Returns the (action, group) pair for the mutation performed — group
    creation or membership addition — so the caller can audit it, or None when
    the membership already existed and nothing changed. Planning fails rows
    targeting archived groups closed, and the write boundary RE-CHECKS under a
    row lock: a group archived in the plan-to-apply window raises
    ChannelImportArchivedGroupError so the race fails the transaction closed
    instead of silently mutating a retired group.
    """
    group = groups.get_group_by_cms_id(cms_group_id, for_update=True)
    if group is None:
        created = groups.create_group(
            name=cms_group_id,
            group_type="SECTOR",
            channel_ids=[channel_id],
            cms_group_id=cms_group_id,
        )
        return ("group_created", created)
    if not group.active:
        raise ChannelImportArchivedGroupError(
            f"channel group was archived during the import: {cms_group_id}; "
            "reactivate it (or remove the Group_ID) and retry"
        )
    if channel_id not in group.channel_ids:
        updated = groups.add_members(group_id=group.id, channel_ids=[channel_id])
        return ("member_added", updated)
    return None
