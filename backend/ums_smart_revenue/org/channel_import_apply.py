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
    ChannelImportOutcome,
    ChannelImportPlan,
    ChannelImportPlanEntry,
)
from ums_smart_revenue.org.channel_registry import ChannelRegistryStore


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

    CREATE and UPDATE rows perform a registry write and an audit event.
    UNCHANGED rows perform neither, but still reach group-membership
    reconciliation below: the plan's outcome is computed only from inventory
    fields (channel_name, cms_status, content_owner_id, revenue_required), so
    treating UNCHANGED as a full no-op would silently drop a Group_ID column
    added on re-import. ERROR rows are skipped entirely. One CHANNEL_IMPORTED
    summary event closes the trail.
    """
    for entry in plan.entries:
        channel_id = entry.youtube_channel_id
        if channel_id is None or entry.channel_name is None:
            continue
        event_type: AuditEventType | None = None
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
        elif entry.outcome is ChannelImportOutcome.UPDATE:
            registry.update_inventory(
                youtube_channel_id=channel_id,
                channel_name=entry.channel_name,
                cms_status=cms_status,
                content_owner_id=content_owner_id,
                revenue_required=bool(entry.revenue_required),
            )
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
                    entry, content_owner_id=content_owner_id, cms_status=cms_status
                ),
            )
        if entry.group_id:
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
            "filename": filename,
            "content_owner_id": content_owner_id,
            "cms_status": cms_status,
            "counts": dict(plan.counts),
        },
    )


def _channel_audit_details(
    entry: ChannelImportPlanEntry, *, content_owner_id: str, cms_status: str
) -> dict[str, object]:
    """Build one channel write's audit details with full field-level provenance.

    ``changes`` carries the plan's {field: {from, to}} diff so the durable
    trail records exactly what was modified and its old/new values (empty for
    a CREATE, whose values are all new). ``view_revenue_raw`` preserves the
    operator's original CSV token — None means the column was absent and the
    required-by-default rule applied — because the derived boolean alone
    cannot distinguish an explicit permission from the default.
    """
    return {
        "channel_name": entry.channel_name,
        "content_owner_id": content_owner_id,
        "cms_status": cms_status,
        "revenue_required": entry.revenue_required,
        "view_revenue_raw": entry.view_revenue_raw,
        "changes": {name: {"from": pair[0], "to": pair[1]} for name, pair in entry.changes.items()},
        "source": "bulk_import",
    }


def _attach_group_membership(
    groups: ChannelGroupRegistryStore, *, cms_group_id: str, channel_id: str
) -> tuple[str, ChannelGroupEntry] | None:
    """Ensure the channel belongs to the group carrying this CMS key.

    Returns the (action, group) pair for the mutation performed — group
    creation or membership addition — so the caller can audit it, or None when
    the membership already existed and nothing changed. Archived groups never
    reach this point: planning fails those rows closed before any write.
    """
    group = groups.get_group_by_cms_id(cms_group_id)
    if group is None:
        created = groups.create_group(
            name=cms_group_id,
            group_type="SECTOR",
            channel_ids=[channel_id],
            cms_group_id=cms_group_id,
        )
        return ("group_created", created)
    if channel_id not in group.channel_ids:
        updated = groups.add_members(group_id=group.id, channel_ids=[channel_id])
        return ("member_added", updated)
    return None
