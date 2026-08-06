# ============================================================================
# Purpose: SQL implementation of ChannelGroupRegistryStore — tenant-scoped
#   channel-group rows, their membership, and the CMS-key lookup the bulk
#   channel import reconciles Group_ID columns against.
# Database/ORM: ChannelGroupORM and ChannelGroupMemberORM (SELECT / INSERT /
#   DELETE), joined to YouTubeChannelORM to resolve external channel ids.
#   Uniqueness comes from unique (tenant_id, cms_group_id) (20260803_0001) and
#   the channel_group_members primary key.
# Standards: THE PARENT GROUP ROW IS THE MEMBERSHIP SERIALIZATION POINT — every
#   membership writer locks it, so a reader that checked membership under the
#   same lock cannot have its skip-decision invalidated mid-request. That lock
#   is FOR NO KEY UPDATE (with_for_update(key_share=True)), never plain FOR
#   UPDATE: plain FOR UPDATE conflicts with the FOR KEY SHARE lock a
#   channel_group_members INSERT takes on its referenced group row, which
#   deadlocks the import against the groups API. Member reads come in two
#   flavours that must not be conflated — active-only for the finance scope
#   selector (it has to advertise what the revenue read path will actually
#   sum) and full for management authorization. Uniqueness races raise the
#   typed ChannelGroupConflictError for a 409, never a bare IntegrityError
#   500; because that IntegrityError has already aborted the PostgreSQL
#   transaction, callers must fail the request rather than retry on the same
#   session. SQLite ignores FOR UPDATE and is single-writer, so the locking is
#   a PostgreSQL-only concern.
# Blast Radius: Channel-group membership and the finance group-scope selection
#   built on it. No revenue math, no allocation, no audit of its own — callers
#   (the import applier, the groups routes) own the audit trail.
# Connections:
#   - File: backend/ums_smart_revenue/org/channel_groups.py -> the Protocol and
#     in-memory parity implementation this satisfies.
#   - File: backend/ums_smart_revenue/org/channel_import_apply.py -> bulk
#     import consumer; holds the write-boundary archived recheck.
#   - File: backend/ums_smart_revenue/api/groups.py -> HTTP routes that map
#     ChannelGroupConflictError to 409.
# ============================================================================
"""SQL-backed tenant-scoped channel group registry and membership writes."""

from uuid import UUID, uuid4

from sqlalchemy import delete, select
from sqlalchemy import or_ as sa_or
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.org_models import (
    ChannelGroupMemberORM,
    ChannelGroupORM,
    YouTubeChannelORM,
)
from ums_smart_revenue.org.channel_groups import (
    ChannelGroupConflictError,
    ChannelGroupEntry,
    require_adoptable_owner,
)
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID
from ums_smart_revenue.tenancy.context import get_current_tenant

_DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)
_SQLITE_DUPLICATE_GROUP_MEMBER_ERROR = (
    "unique constraint failed: channel_group_members.group_id, channel_group_members.channel_id"
)
_SQLITE_DUPLICATE_CMS_GROUP_ERROR = (
    "unique constraint failed: channel_groups.tenant_id, channel_groups.cms_group_id"
)


class SqlAlchemyChannelGroupRegistry:
    """SQL-backed channel group registry scoped to a single tenant."""

    def __init__(self, session: Session, *, tenant_id: UUID | str | None = None):
        self._session = session
        self._tenant_id = _resolve_tenant_id(tenant_id)

    def list_groups(self) -> list[ChannelGroupEntry]:
        """Return active channel groups with their member channel ids.

        Member channels are filtered to active rows so the revenue scope
        selector operates on the same set the revenue read path uses
        (via get_active_member_channels). Management callers that need
        the full member set should call list_groups_full instead.
        """
        rows = self._session.scalars(
            select(ChannelGroupORM)
            .where(
                ChannelGroupORM.tenant_id == self._tenant_id,
                ChannelGroupORM.active.is_(True),
            )
            .order_by(ChannelGroupORM.name)
        ).all()
        group_ids = [row.id for row in rows]
        channel_ids_by_group = self._channel_ids_by_group_active(group_ids)
        return [
            self._to_entry(row, channel_ids=channel_ids_by_group.get(row.id, ())) for row in rows
        ]

    def list_groups_full(self) -> list[ChannelGroupEntry]:
        """Return active channel groups with their FULL member channel ids.

        Distinct from list_groups so the groups management API can
        authorize over the complete member set (including channels that
        have since been deactivated and are outside the caller's org
        scope), while the scope selector continues to operate on the
        active-only member set.
        """
        rows = self._session.scalars(
            select(ChannelGroupORM)
            .where(
                ChannelGroupORM.tenant_id == self._tenant_id,
                ChannelGroupORM.active.is_(True),
            )
            .order_by(ChannelGroupORM.name)
        ).all()
        group_ids = [row.id for row in rows]
        channel_ids_by_group = self._channel_ids_by_group(group_ids)
        return [
            self._to_entry(row, channel_ids=channel_ids_by_group.get(row.id, ())) for row in rows
        ]

    # ========================================================================
    # Purpose: Enumerate the CMS-keyed groups a sync must reconcile — every
    #   group carrying a cms_group_id, active or not, optionally narrowed to
    #   one content owner.
    # Database/ORM: ChannelGroupORM (read-only), tenant-scoped, plus
    #   ChannelGroupMemberORM x YouTubeChannelORM for FULL membership (not the
    #   active-only member set the finance scope selector uses).
    # Standards: Owner scoping is OR-NULL by design. (tenant_id, cms_group_id)
    #   is unique tenant-wide, so an owner-NULL row hidden from a scoped read
    #   would be planned as CREATE and collide on that key, making the group
    #   permanently unsyncable. Those rows are therefore returned for matching;
    #   refusing to DEACTIVATE them is the planner's job, not this read's.
    # Blast Radius: CMS group-sync planning only — group naming, membership,
    #   and active state as the mirror resolves them. No finance totals.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/channels.py -> sync route caller.
    #   - File: backend/ums_smart_revenue/org/channel_group_sync.py ->
    #     plan_group_sync consumes the result and owns the deactivation gate.
    # ========================================================================
    def list_synced_groups(self, *, content_owner_id: str | None = None) -> list[ChannelGroupEntry]:
        """Return every CMS-keyed group (active or not) with full membership.

        Sync planning must see deactivated synced groups so a CMS key that
        reappears upstream can REACTIVATE its original local group instead of
        creating a duplicate. ``content_owner_id`` scopes the result to one
        owner's groups (see the Protocol docstring for why CMS group sync must
        always pass it).

        A scoped call ALSO returns owner-NULL rows — groups created before
        ``content_owner_id`` existed, or by an older import that did not stamp
        it. They must stay visible: ``(tenant_id, cms_group_id)`` is unique
        tenant-wide, so hiding them would make the planner emit CREATE for a
        key that already exists and the apply would collide on that key,
        leaving those groups permanently unsyncable. Surfacing them instead
        lets sync reconcile (and thereby adopt) them.
        """
        conditions = [
            ChannelGroupORM.tenant_id == self._tenant_id,
            ChannelGroupORM.cms_group_id.is_not(None),
        ]
        if content_owner_id is not None:
            conditions.append(
                sa_or(
                    ChannelGroupORM.content_owner_id == content_owner_id,
                    ChannelGroupORM.content_owner_id.is_(None),
                )
            )
        rows = self._session.scalars(
            select(ChannelGroupORM).where(*conditions).order_by(ChannelGroupORM.name)
        ).all()
        group_ids = [row.id for row in rows]
        channel_ids_by_group = self._channel_ids_by_group(group_ids)
        return [
            self._to_entry(row, channel_ids=channel_ids_by_group.get(row.id, ())) for row in rows
        ]

    # ========================================================================
    # Purpose: Resolve one group by id, optionally under the membership
    #   serialization lock so a caller can diff membership and write without a
    #   racing writer invalidating the diff in between.
    # Database/ORM: ChannelGroupORM (SELECT, tenant-scoped), plus
    #   ChannelGroupMemberORM x YouTubeChannelORM for the member ids;
    #   with_for_update(key_share=True) when for_update is set.
    # Standards: The lock is FOR NO KEY UPDATE, never plain FOR UPDATE — plain
    #   FOR UPDATE conflicts with the FOR KEY SHARE lock a member INSERT takes
    #   on its referenced group row and deadlocks import against the groups
    #   API. SQLite ignores FOR UPDATE (single-writer), so this is a
    #   PostgreSQL-only concern.
    # Blast Radius: Read-only here; the lock it takes serializes the CALLER's
    #   subsequent membership/name/active writes and their audit rows.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_group_sync_apply.py ->
    #     _execute_update diffs under this lock before writing.
    # ========================================================================
    def get_group(self, group_id: str, *, for_update: bool = False) -> ChannelGroupEntry | None:
        """Return the group by id, or None.

        ``for_update`` takes the parent row's FOR NO KEY UPDATE lock — the
        membership serialization point every membership writer also takes — so
        a caller that diffs membership under it (CMS group sync's apply) cannot
        have that diff invalidated by a concurrent add/remove committing before
        its own write lands.
        """
        row = self._get_group_row(group_id, for_update=for_update)
        if row is None:
            return None
        return self._to_entry(row)

    # ========================================================================
    # Purpose: Resolve the channel group carrying one YouTube CMS group key —
    #   the lookup the bulk channel import reconciles Group_ID columns against
    #   (create the group when absent, attach membership when present).
    # Database/ORM: ChannelGroupORM (read-only), tenant-scoped; uniqueness is
    #   enforced by unique (tenant_id, cms_group_id) from 20260803_0001.
    # Standards: Returns archived groups too (no active filter) so the import
    #   PLANNER can fail rows targeting a retired group closed instead of
    #   mutating it; callers must check .active before writing membership.
    # Blast Radius: Channel-group membership and therefore finance group-scope
    #   selection. No finance totals, no allocation.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     plan_channel_import_with_stores (archived-group planning gate via
    #     list_archived_cms_group_ids) and membership attachment for
    #     non-archived groups (write-boundary for_update recheck here).
    # ========================================================================
    def get_group_by_cms_id(
        self, cms_group_id: str, *, for_update: bool = False
    ) -> ChannelGroupEntry | None:
        """Return the tenant-scoped group carrying this CMS key, or None.

        ``for_update`` row-locks the group so a write-boundary active-state
        check cannot race a concurrent archive (SQLite ignores FOR UPDATE).
        """
        statement = select(ChannelGroupORM).where(
            ChannelGroupORM.tenant_id == self._tenant_id,
            ChannelGroupORM.cms_group_id == cms_group_id,
        )
        if for_update:
            # FOR NO KEY UPDATE: excludes other membership WRITERS (they take
            # the same mode) without conflicting with the FOR KEY SHARE lock a
            # channel_group_members INSERT takes on its referenced group row,
            # which is what keeps import and group-API orderings from
            # deadlocking (review #159 r3714644431).
            statement = statement.with_for_update(key_share=True)
        row = self._session.scalars(statement).one_or_none()
        if row is None:
            return None
        return self._to_entry(row)

    # ========================================================================
    # Purpose: Bulk-classify a roster's CMS group keys by archived state so
    #   import planning can fail rows targeting archived groups closed without
    #   a lookup-per-group query storm.
    # Database/ORM: ChannelGroupORM (read-only; cms_group_id column under the
    #   per-tenant unique key). No membership loading, no writes.
    # Standards: One bounded SELECT for the whole key set; tenant-scoped;
    #   unknown keys are simply absent from the result (planning treats them
    #   as creatable). Read-only -> RLS-safe, no platform lane.
    # Blast Radius: Import planning's archived-group per-row errors, which
    #   gate finance-scope group mutations. No group writes, no audit.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     plan_channel_import_with_stores feeds the result to the planner.
    #   - File: backend/ums_smart_revenue/org/channel_import.py ->
    #     plan_channel_import fails rows whose key lands in this set.
    # ========================================================================
    def list_archived_cms_group_ids(self, cms_group_ids: set[str]) -> set[str]:
        """Return the subset of CMS keys whose existing group is archived.

        One bounded SELECT (no per-key round trips, no membership loading) so
        import planning can vet a full 5000-row roster's group keys without a
        lookup-per-group query storm.
        """
        if not cms_group_ids:
            return set()
        rows = self._session.scalars(
            select(ChannelGroupORM.cms_group_id).where(
                ChannelGroupORM.tenant_id == self._tenant_id,
                ChannelGroupORM.cms_group_id.in_(cms_group_ids),
                ChannelGroupORM.active.is_(False),
            )
        ).all()
        # The IN clause already excludes NULL keys; the comprehension narrows
        # the column's Optional type for the checker without a cast.
        return {key for key in rows if key is not None}

    # ========================================================================
    # Purpose: Bulk-classify a roster's CMS group keys by CROSS-OWNER conflict
    #   so import planning can fail those rows closed, rather than letting the
    #   write boundary's own recheck 409 an import the preview called clean.
    # Database/ORM: ChannelGroupORM (read-only; cms_group_id + content_owner_id
    #   under the per-tenant unique key). No membership loading, no writes.
    # Standards: One bounded SELECT for the whole key set; tenant-scoped;
    #   unknown keys absent. IS NOT NULL is EXPLICIT, not implied by the
    #   inequality — three-valued logic already excludes NULL stamps, but
    #   silently, and adoption depends on those rows staying attachable.
    #   Read-only -> RLS-safe, no platform lane.
    # Blast Radius: Import planning's cross-owner per-row errors and sync's
    #   CONFLICT outcomes. No group writes, no audit.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     plan_channel_import_with_stores feeds the result to the planner.
    #   - File: backend/ums_smart_revenue/org/channel_group_sync_apply.py ->
    #     _foreign_owner_group_ids chunks this for sync planning.
    # ========================================================================
    def list_foreign_owner_cms_group_ids(
        self, cms_group_ids: set[str], *, content_owner_id: str
    ) -> set[str]:
        """Return the subset of CMS keys stamped to a different content owner.

        ``IS NOT NULL`` is explicit rather than implied by the inequality:
        SQL three-valued logic makes ``content_owner_id != :owner`` UNKNOWN
        (not TRUE) for a NULL stamp, so the predicate would already exclude
        owner-NULL rows — but silently, and adoption depends on those rows
        staying attachable. One bounded SELECT, mirroring
        ``list_archived_cms_group_ids``.
        """
        if not cms_group_ids:
            return set()
        rows = self._session.scalars(
            select(ChannelGroupORM.cms_group_id).where(
                ChannelGroupORM.tenant_id == self._tenant_id,
                ChannelGroupORM.cms_group_id.in_(cms_group_ids),
                ChannelGroupORM.content_owner_id.is_not(None),
                ChannelGroupORM.content_owner_id != content_owner_id,
            )
        ).all()
        return {key for key in rows if key is not None}

    # ========================================================================
    # Purpose: Bulk-classify a roster's CMS group keys by ADOPTABILITY — the
    #   owner-NULL rows — so import planning can REFUSE the rows targeting
    #   them: claiming an existing group belongs to the owner's CMS sync, not
    #   to a CSV cell. Third of a matched set with the archived and
    #   cross-owner lookups above; the same pass reads all three.
    # Database/ORM: ChannelGroupORM (read-only; cms_group_id + content_owner_id
    #   under the per-tenant unique key). No membership loading, no writes.
    # Standards: One bounded SELECT for the whole key set; tenant-scoped;
    #   unknown keys absent — a key with no group is a CREATE, and a group the
    #   import creates is stamped at birth rather than adopted. Deliberately
    #   NOT filtered on active: the archived lookup fails those rows closed on
    #   its own, and narrowing here would make two reads of one row disagree.
    #   Read-only -> RLS-safe, no platform lane.
    # Blast Radius: Which import rows plan as ERROR. No group writes, no
    #   audit — and no stamp anywhere downstream: the apply's locked write
    #   boundary refuses these groups rather than claiming them.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     plan_channel_import_with_stores feeds the result to the planner, and
    #     _attach_group_membership re-checks it under the row lock.
    #   - File: backend/ums_smart_revenue/org/channel_import.py ->
    #     plan_channel_import fails rows whose key lands in this set.
    # ========================================================================
    def list_adoptable_cms_group_ids(self, cms_group_ids: set[str]) -> set[str]:
        """Return the subset of CMS keys whose existing group is owner-NULL.

        Deliberately NOT filtered on ``active``: an archived group is already
        failed closed by ``list_archived_cms_group_ids``, and narrowing here
        would make the two sets disagree about the same row for no gain. One
        bounded SELECT, mirroring its two siblings.
        """
        if not cms_group_ids:
            return set()
        rows = self._session.scalars(
            select(ChannelGroupORM.cms_group_id).where(
                ChannelGroupORM.tenant_id == self._tenant_id,
                ChannelGroupORM.cms_group_id.in_(cms_group_ids),
                ChannelGroupORM.content_owner_id.is_(None),
            )
        ).all()
        return {key for key in rows if key is not None}

    def get_active_member_channels(self, group_id: str) -> tuple[str, ...] | None:
        """Return active member channel ids for a group, or None if the group is missing.

        Distinct from get_group (which returns ALL members for management authz)
        so the revenue read path operates on the same set the scope selector
        advertises, while the groups management API can still authorize over
        the full member set (including channels that have since been
        deactivated and are outside the caller's org scope).
        """
        row = self._get_group_row(group_id)
        if row is None:
            return None
        rows = self._session.execute(
            select(YouTubeChannelORM.youtube_channel_id)
            .join(
                ChannelGroupMemberORM,
                (ChannelGroupMemberORM.tenant_id == YouTubeChannelORM.tenant_id)
                & (ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id),
            )
            .where(
                ChannelGroupMemberORM.tenant_id == self._tenant_id,
                ChannelGroupMemberORM.group_id == row.id,
                YouTubeChannelORM.active.is_(True),
            )
            .order_by(YouTubeChannelORM.youtube_channel_id)
        ).all()
        return tuple(row.youtube_channel_id for row in rows)

    # ========================================================================
    # Purpose: Create a channel group row and its initial membership — the
    #   path the bulk import takes when a roster's Group_ID has no group yet.
    # Database/ORM: ChannelGroupORM INSERT then ChannelGroupMemberORM
    #   DELETE-then-INSERT for the member set, with YouTubeChannelORM resolving
    #   external ids to row ids (an unknown id raises KeyError before any
    #   write). The per-tenant unique (tenant_id, cms_group_id) key is the
    #   authoritative duplicate guard.
    # Standards: A CMS-key race is translated to the typed
    #   ChannelGroupConflictError (route: 409, retryable), never allowed to
    #   escape as an IntegrityError 500. That IntegrityError has ALREADY
    #   aborted the PostgreSQL transaction, so callers must fail the request —
    #   catch-and-retry on the same session cannot work, and both the import
    #   and groups routes are written that way. Non-duplicate integrity errors
    #   re-raise untouched rather than being misreported as a conflict.
    # Blast Radius: Channel-group membership and the finance group-scope
    #   selection built on it. No revenue math, no allocation; the caller
    #   audits the creation (the import emits GROUP_CREATED).
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     _attach_group_membership creates the group when the CMS key is new.
    #   - File: backend/ums_smart_revenue/api/groups.py -> group create route.
    # ========================================================================
    def create_group(
        self,
        *,
        name: str,
        group_type: str,
        channel_ids: list[str],
        cms_group_id: str | None = None,
        content_owner_id: str | None = None,
    ) -> ChannelGroupEntry:
        """Create a group row plus membership, raising typed on a CMS-key race.

        On ``ChannelGroupConflictError`` the underlying IntegrityError has
        already aborted the Postgres transaction: the session is unusable
        until rolled back, so callers must fail the request (as the import
        and groups routes do), never catch-and-retry on the same session.
        """
        channel_rows = self._channel_rows_by_external_ids(channel_ids)
        row = ChannelGroupORM(
            id=uuid4(),
            tenant_id=self._tenant_id,
            name=name,
            group_type=group_type,
            active=True,
            cms_group_id=cms_group_id,
            content_owner_id=content_owner_id,
        )
        self._session.add(row)
        try:
            self._session.flush()
        except IntegrityError as exc:
            # Two concurrent imports can both see a CMS key as missing and race
            # this INSERT; the per-tenant unique key makes the loser fail here.
            # Translate to the typed conflict (route: 409, retryable) instead
            # of letting the IntegrityError escape as a 500.
            if not _is_duplicate_cms_group_integrity_error(exc):
                raise
            raise ChannelGroupConflictError(
                f"channel group already exists for cms_group_id: {cms_group_id}"
            ) from exc
        self._replace_member_rows(row.id, channel_rows)
        self._session.flush()
        return self._to_entry(row)

    def update_group(
        self,
        *,
        group_id: str,
        name: str | None,
        active: bool | None,
        content_owner_id: str | None = None,
    ) -> ChannelGroupEntry:
        """Update name / active / content owner; None means leave unchanged.

        ``content_owner_id`` is ADOPT-ONLY and enforced here, not just by the
        caller: it may fill an owner-NULL row, and re-passing the owner a row
        already has is a no-op, but reassigning an owned group to a different
        owner raises. Ownership is what scopes CMS group sync, so a call-site
        bug that silently moved a group between content owners would corrupt
        every later sync's plan on both sides.
        """
        row = self._require_group_row(group_id)
        if name is not None:
            row.name = name
        if active is not None:
            row.active = active
        if content_owner_id is not None:
            require_adoptable_owner(row.content_owner_id, content_owner_id, group_id=group_id)
            row.content_owner_id = content_owner_id
        self._session.flush()
        return self._to_entry(row)

    # ========================================================================
    # Purpose: Attach channels to an existing group idempotently — the bulk
    #   import's membership path for a Group_ID whose group already exists.
    # Database/ORM: ChannelGroupORM row-locked FOR NO KEY UPDATE, then
    #   ChannelGroupMemberORM SELECT (existing members) + INSERT (the pending
    #   ones) inside a SAVEPOINT; YouTubeChannelORM resolves external ids.
    # Standards: Lock the parent group row FIRST — it is the membership
    #   serialization point, so a reader that checked membership under the same
    #   lock cannot have its skip-decision invalidated by a concurrent
    #   add/remove. FOR NO KEY UPDATE specifically, because plain FOR UPDATE
    #   conflicts with the FOR KEY SHARE lock the member INSERT itself takes on
    #   this row. Idempotent by construction: already-present members are
    #   filtered out, and a duplicate-key race is absorbed by rolling back the
    #   savepoint (NOT the request), re-reading membership, and inserting only
    #   what is still missing — so a lost race yields the same end state rather
    #   than a 500. Non-duplicate integrity errors re-raise untouched.
    # Blast Radius: Channel-group membership and the finance group-scope
    #   selection built on it. No revenue math; the caller audits the change.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     _attach_group_membership calls this and audits GROUP_MEMBER_ADDED.
    #   - File: backend/ums_smart_revenue/api/groups.py -> add-members route.
    # ========================================================================
    def add_members(self, *, group_id: str, channel_ids: list[str]) -> ChannelGroupEntry:
        # The parent group row is the membership serialization point: every
        # membership writer locks it, so a reader that checked membership
        # under the same lock (the import's get_group_by_cms_id
        # for_update=True) cannot have its skip-decision invalidated by a
        # concurrent add/remove committing mid-request (review #159
        # r3713841264). SQLite ignores FOR UPDATE (single-writer anyway).
        row = self._require_group_row(group_id, for_update=True)
        channel_rows = self._channel_rows_by_external_ids(channel_ids)
        existing_ids = set(
            self._session.scalars(
                select(ChannelGroupMemberORM.channel_id).where(
                    ChannelGroupMemberORM.tenant_id == self._tenant_id,
                    ChannelGroupMemberORM.group_id == row.id,
                )
            ).all()
        )
        pending_channel_ids = [
            channel.id for channel in channel_rows if channel.id not in existing_ids
        ]
        if not pending_channel_ids:
            return self._to_entry(row)

        nested = self._session.begin_nested()
        try:
            for channel_id in pending_channel_ids:
                self._session.add(
                    ChannelGroupMemberORM(
                        tenant_id=self._tenant_id,
                        group_id=row.id,
                        channel_id=channel_id,
                    )
                )
            self._session.flush()
        except IntegrityError as exc:
            nested.rollback()
            if not _is_duplicate_group_member_integrity_error(exc):
                raise
            row = self._require_group_row(group_id)
            existing_ids = set(
                self._session.scalars(
                    select(ChannelGroupMemberORM.channel_id).where(
                        ChannelGroupMemberORM.tenant_id == self._tenant_id,
                        ChannelGroupMemberORM.group_id == row.id,
                    )
                ).all()
            )
            for channel_id in pending_channel_ids:
                if channel_id not in existing_ids:
                    self._session.add(
                        ChannelGroupMemberORM(
                            tenant_id=self._tenant_id,
                            group_id=row.id,
                            channel_id=channel_id,
                        )
                    )
            self._session.flush()
        else:
            nested.commit()
        return self._to_entry(row)

    # ========================================================================
    # Purpose: Detach one channel from a group — the inverse of add_members.
    # Database/ORM: ChannelGroupORM row-locked FOR NO KEY UPDATE, then a
    #   tenant-scoped ChannelGroupMemberORM DELETE; YouTubeChannelORM resolves
    #   the external channel id (unknown id raises KeyError before the DELETE).
    # Standards: Takes the SAME parent-row lock as add_members, in the same
    #   FOR NO KEY UPDATE mode. Without it a concurrent import could read the
    #   channel as a member under ITS lock, skip the add as redundant, and then
    #   let this DELETE commit — a successful import whose requested membership
    #   is absent afterwards. The DELETE is idempotent (zero rows when the
    #   channel is not a member) so a repeated removal is not an error.
    # Blast Radius: Channel-group membership and the finance group-scope
    #   selection built on it. No revenue math; the caller audits the change.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/groups.py -> remove-member route.
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py ->
    #     add_members, whose lock ordering this must mirror exactly.
    # ========================================================================
    def remove_member(self, *, group_id: str, channel_id: str) -> ChannelGroupEntry:
        # Same membership serialization point as add_members: without the
        # parent-row lock a concurrent import could read the channel as a
        # member (under ITS lock), skip the add, and then let this delete
        # commit — a successful import whose requested membership is absent.
        row = self._require_group_row(group_id, for_update=True)
        channel = self._channel_rows_by_external_ids([channel_id])[0]
        self._session.execute(
            delete(ChannelGroupMemberORM).where(
                ChannelGroupMemberORM.tenant_id == self._tenant_id,
                ChannelGroupMemberORM.group_id == row.id,
                ChannelGroupMemberORM.channel_id == channel.id,
            )
        )
        self._session.flush()
        return self._to_entry(row)

    def _replace_member_rows(self, group_id: UUID, channel_rows: list[YouTubeChannelORM]) -> None:
        self._session.execute(
            delete(ChannelGroupMemberORM).where(
                ChannelGroupMemberORM.tenant_id == self._tenant_id,
                ChannelGroupMemberORM.group_id == group_id,
            )
        )
        for channel in channel_rows:
            self._session.add(
                ChannelGroupMemberORM(
                    tenant_id=self._tenant_id,
                    group_id=group_id,
                    channel_id=channel.id,
                )
            )

    def _get_group_row(self, group_id: str, *, for_update: bool = False) -> ChannelGroupORM | None:
        try:
            group_uuid = UUID(group_id)
        except (TypeError, ValueError) as _:
            return None
        statement = select(ChannelGroupORM).where(
            ChannelGroupORM.tenant_id == self._tenant_id,
            ChannelGroupORM.id == group_uuid,
        )
        if for_update:
            # FOR NO KEY UPDATE: excludes other membership WRITERS (they take
            # the same mode) without conflicting with the FOR KEY SHARE lock a
            # channel_group_members INSERT takes on its referenced group row,
            # which is what keeps import and group-API orderings from
            # deadlocking (review #159 r3714644431).
            statement = statement.with_for_update(key_share=True)
        return self._session.scalars(statement).one_or_none()

    def _require_group_row(self, group_id: str, *, for_update: bool = False) -> ChannelGroupORM:
        row = self._get_group_row(group_id, for_update=for_update)
        if row is None:
            raise KeyError(f"Group not found: {group_id}")
        return row

    def _channel_rows_by_external_ids(self, channel_ids: list[str]) -> list[YouTubeChannelORM]:
        unique_channel_ids = list(dict.fromkeys(channel_ids))
        if not unique_channel_ids:
            return []
        rows = self._session.scalars(
            select(YouTubeChannelORM).where(
                YouTubeChannelORM.tenant_id == self._tenant_id,
                YouTubeChannelORM.youtube_channel_id.in_(unique_channel_ids),
            )
        ).all()
        rows_by_external_id = {row.youtube_channel_id: row for row in rows}
        missing = [
            channel_id for channel_id in unique_channel_ids if channel_id not in rows_by_external_id
        ]
        if missing:
            raise KeyError(f"Channel not found: {missing[0]}")
        return [rows_by_external_id[channel_id] for channel_id in unique_channel_ids]

    def _channel_ids_by_group(self, group_ids: list[UUID]) -> dict[UUID, tuple[str, ...]]:
        if not group_ids:
            return {}
        rows = self._session.execute(
            select(ChannelGroupMemberORM.group_id, YouTubeChannelORM.youtube_channel_id)
            .join(
                YouTubeChannelORM,
                (ChannelGroupMemberORM.tenant_id == YouTubeChannelORM.tenant_id)
                & (ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id),
            )
            .where(
                ChannelGroupMemberORM.tenant_id == self._tenant_id,
                ChannelGroupMemberORM.group_id.in_(group_ids),
            )
            .order_by(ChannelGroupMemberORM.group_id, YouTubeChannelORM.youtube_channel_id)
        ).all()
        channel_ids_by_group: dict[UUID, list[str]] = {}
        for group_id, youtube_channel_id in rows:
            channel_ids_by_group.setdefault(group_id, []).append(youtube_channel_id)
        return {
            group_id: tuple(channel_ids) for group_id, channel_ids in channel_ids_by_group.items()
        }

    def _channel_ids_by_group_active(self, group_ids: list[UUID]) -> dict[UUID, tuple[str, ...]]:
        """Same shape as _channel_ids_by_group but excludes inactive channels.

        Used by list_groups (the scope selector) so the selector advertises
        the same member set the revenue read path will filter to. The
        full-member set is still available via _channel_ids_by_group for
        management authz (list_groups_full).
        """
        if not group_ids:
            return {}
        rows = self._session.execute(
            select(ChannelGroupMemberORM.group_id, YouTubeChannelORM.youtube_channel_id)
            .join(
                YouTubeChannelORM,
                (ChannelGroupMemberORM.tenant_id == YouTubeChannelORM.tenant_id)
                & (ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id),
            )
            .where(
                ChannelGroupMemberORM.tenant_id == self._tenant_id,
                ChannelGroupMemberORM.group_id.in_(group_ids),
                YouTubeChannelORM.active.is_(True),
            )
            .order_by(ChannelGroupMemberORM.group_id, YouTubeChannelORM.youtube_channel_id)
        ).all()
        channel_ids_by_group: dict[UUID, list[str]] = {}
        for group_id, youtube_channel_id in rows:
            channel_ids_by_group.setdefault(group_id, []).append(youtube_channel_id)
        return {
            group_id: tuple(channel_ids) for group_id, channel_ids in channel_ids_by_group.items()
        }

    def _to_entry(
        self, row: ChannelGroupORM, *, channel_ids: tuple[str, ...] | None = None
    ) -> ChannelGroupEntry:
        resolved_channel_ids = (
            channel_ids
            if channel_ids is not None
            else tuple(
                self._session.scalars(
                    select(YouTubeChannelORM.youtube_channel_id)
                    .join(
                        ChannelGroupMemberORM,
                        (ChannelGroupMemberORM.tenant_id == YouTubeChannelORM.tenant_id)
                        & (ChannelGroupMemberORM.channel_id == YouTubeChannelORM.id),
                    )
                    .where(
                        ChannelGroupMemberORM.tenant_id == self._tenant_id,
                        ChannelGroupMemberORM.group_id == row.id,
                    )
                    .order_by(YouTubeChannelORM.youtube_channel_id)
                ).all()
            )
        )
        return ChannelGroupEntry(
            id=str(row.id),
            name=row.name,
            group_type=row.group_type,
            active=row.active,
            channel_ids=resolved_channel_ids,
            cms_group_id=row.cms_group_id,
            content_owner_id=row.content_owner_id,
        )


def _resolve_tenant_id(tenant_id: UUID | str | None) -> UUID:
    if tenant_id is not None:
        return _parse_tenant_uuid(tenant_id)
    current_tenant = get_current_tenant()
    if current_tenant is not None:
        return current_tenant.id
    return _DEFAULT_TENANT_UUID


def _parse_tenant_uuid(tenant_id: UUID | str) -> UUID:
    if isinstance(tenant_id, UUID):
        return tenant_id
    try:
        return UUID(tenant_id.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError("tenant_id must be a valid UUID") from exc


def _is_duplicate_group_member_integrity_error(exc: IntegrityError) -> bool:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint_name = str(getattr(diag, "constraint_name", "") or "").lower()
    error_text = f"{exc.orig!s} {exc!s}".lower()
    return (
        "channel_group_members_pkey" in constraint_name
        or _SQLITE_DUPLICATE_GROUP_MEMBER_ERROR in error_text
    )


def _is_duplicate_cms_group_integrity_error(exc: IntegrityError) -> bool:
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    constraint_name = str(getattr(diag, "constraint_name", "") or "").lower()
    error_text = f"{exc.orig!s} {exc!s}".lower()
    return (
        "uq_channel_groups_tenant_id_cms_group_id" in constraint_name
        or "uq_channel_groups_tenant_id_cms_group_id" in error_text
        or _SQLITE_DUPLICATE_CMS_GROUP_ERROR in error_text
    )
