# ============================================================================
# Purpose: Erase one channel group's content-owner stamp and audit the
#   erasure — the domain half of the sanctioned recovery path for a group
#   stamped to the WRONG YouTube content owner. Mirrors channel_import_apply /
#   channel_group_sync_apply's split: the route stays thin (permission gate,
#   reason validation, error-to-status mapping, response shaping); the store
#   read, the locked write, and the audit row live here.
# Database/ORM: ChannelGroupORM via ChannelGroupRegistryStore.get_group (the
#   unlocked existence pre-read) and clear_content_owner (the row-locked
#   write), inside the caller's single tenant transaction; audit_logs via the
#   supplied sink. No membership, name, or active-state write.
# Standards: The existence pre-read carries NO active filter — deactivation is
#   exactly how a wrongly-synced group gets parked, so an archived group's
#   stamp must stay clearable. It establishes EXISTENCE ONLY: its
#   content_owner_id is unlocked and a concurrent adopt can invalidate it, so
#   the audited previous owner comes from ClearedContentOwner, which the store
#   reads under its own FOR NO KEY UPDATE lock. Both failure modes stay typed
#   for the caller to map — KeyError for a group that does not exist (or
#   vanished between the pre-read and the locked write),
#   ChannelGroupNoOwnerStampError when there was no stamp to erase. The audit
#   row is written on the sink the CALLER supplies, which is how the route
#   keeps it atomic with the tenant transaction (#169 invariant): a lost
#   commit must not leave a row claiming a clear that never landed.
# Blast Radius: Channel-group ownership and the audit trail only. No
#   membership, no revenue math, no allocation. Both owners' NEXT sync plans
#   change — that is the point.
# Connections:
#   - File: backend/ums_smart_revenue/api/groups.py -> the DELETE
#     /groups/{id}/content-owner route that orchestrates this.
#   - File: backend/ums_smart_revenue/org/sql_channel_groups.py ->
#     clear_content_owner performs the locked write.
#   - File: backend/ums_smart_revenue/api/channels.py -> the CMS group sync
#     that re-adopts the cleared group under the correct owner.
# ============================================================================
"""Clear a channel group's content-owner stamp and audit what was erased."""

from dataclasses import dataclass

from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditRecord, AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.org.channel_groups import (
    ChannelGroupEntry,
    ChannelGroupNotFoundError,
    ChannelGroupRegistryStore,
)

# Provenance marker on the audit row this module writes: it is how an auditor
# separates a deliberate ownership erasure from the CMS mirror's own group
# changes (AUDIT_SOURCE_CMS_SYNC) and the bulk import's (AUDIT_SOURCE_BULK_IMPORT).
AUDIT_ACTION_CONTENT_OWNER_CLEARED = "content_owner_cleared"


@dataclass(frozen=True)
class ClearedGroupOwnerStamp:
    """One completed clear: the resulting group and the audit row it wrote.

    ``group`` comes from the store's post-write return rather than from the
    pre-read, so a caller disclosing ``content_owner_id`` reports what the
    write actually produced instead of the ``None`` it asked for.
    """

    group: ChannelGroupEntry
    audit_record: AuditRecord


# ============================================================================
# Purpose: Erase one group's content-owner stamp and audit the erasure — the
#   service entry point behind DELETE /groups/{id}/content-owner, and the only
#   sanctioned eraser now that every other writer is adopt-only.
# Database/ORM: ChannelGroupORM via ChannelGroupRegistryStore.get_group (the
#   unlocked existence check) and clear_content_owner (the FOR NO KEY UPDATE
#   write), inside the caller's tenant transaction; audit_logs via ``sink``.
#   No membership, name, or active-state write.
# Standards: The existence check carries NO active filter — an archived group's
#   stamp must stay clearable, because deactivation is how a wrongly-synced
#   group gets parked. It establishes EXISTENCE ONLY: its content_owner_id is
#   unlocked and a concurrent adopt can invalidate it, so the audited previous
#   owner comes from ClearedContentOwner, read under the store's own lock.
#   Both failure modes are TYPED for any caller, HTTP or not —
#   ChannelGroupNotFoundError (never a bare KeyError, which would be
#   indistinguishable from a store-internal lookup bug) and
#   ChannelGroupNoOwnerStampError. Neither writes an audit row: a failed clear
#   that still audited would claim an erasure that never happened.
# Blast Radius: Channel-group ownership and the audit trail. No membership, no
#   revenue math, no allocation. Both owners' NEXT sync plans change.
# Connections:
#   - File: backend/ums_smart_revenue/api/groups.py -> the route that maps
#     these typed errors to 404/409.
#   - File: backend/ums_smart_revenue/org/sql_channel_groups.py ->
#     clear_content_owner performs the locked write.
# ============================================================================
def clear_group_owner_stamp(
    *,
    groups: ChannelGroupRegistryStore,
    group_id: str,
    actor: UserPrincipal,
    scope: AccessScope,
    reason: str,
    audit_sink: AuditSink,
) -> ClearedGroupOwnerStamp:
    """Erase ``group_id``'s owner stamp, auditing the owner actually removed.

    Raises ``ChannelGroupNotFoundError`` when no such group exists for this
    tenant (or the row vanished between the existence check and the locked
    write) and ``ChannelGroupNoOwnerStampError`` when the group carries no
    stamp to clear — clearing nothing is a caller bug, not a silent no-op.
    """
    if groups.get_group(group_id) is None:
        raise ChannelGroupNotFoundError(f"channel group not found: {group_id}")
    try:
        cleared = groups.clear_content_owner(group_id=group_id)
    except KeyError as exc:
        # The row existed a statement ago, so this is the vanished-row race,
        # not a caller error. Translate rather than leak the store's untyped
        # signal past the service boundary.
        raise ChannelGroupNotFoundError(f"channel group not found: {group_id}") from exc
    # The group-shaped audit helper in the API layer cannot carry these
    # details: it hard-codes the group_type/channel_ids pair every
    # membership-shaped change reports, and neither field moves here. Record
    # directly so the row states what was actually erased.
    record = record_audit_event(
        sink=audit_sink,
        actor=actor,
        event_type=AuditEventType.GROUP_UPDATED,
        entity_type="channel_group",
        entity_id=cleared.group.id,
        scope=scope,
        reason=reason,
        details={
            "action": AUDIT_ACTION_CONTENT_OWNER_CLEARED,
            "cms_group_id": cleared.group.cms_group_id,
            "previous_content_owner_id": cleared.previous_content_owner_id,
        },
    )
    return ClearedGroupOwnerStamp(group=cleared.group, audit_record=record)
