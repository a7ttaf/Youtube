# ============================================================================
# Purpose: The channel-group domain contract — the immutable ChannelGroupEntry
#   value, the typed conflict error, the ChannelGroupRegistryStore Protocol
#   every backend must satisfy, and an in-memory registry used as the test
#   double for that Protocol.
# Database/ORM: None. This module is deliberately persistence-free; the SQL
#   implementation (ChannelGroupORM / ChannelGroupMemberORM) lives in
#   sql_channel_groups.py. The in-memory registry is a dict, never a database.
# Standards: The in-memory registry must keep BEHAVIOURAL parity with the SQL
#   store on everything a test could assert — notably the per-tenant unique
#   cms_group_id, which raises ChannelGroupConflictError here exactly as the
#   unique constraint does there, so a duplicate can never pass in tests and
#   fail in production. Where parity is impossible the divergence is documented
#   at the method (for_update is a no-op in memory; every member counts as
#   active). Uniqueness races surface as a typed 409, never a bare
#   IntegrityError 500. Member ordering is insertion order, de-duplicated.
# Blast Radius: Channel-group membership and the finance scope selection built
#   on it. No revenue math, no allocation, no audit of its own (callers audit).
# Connections:
#   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the SQL
#     implementation of this Protocol.
#   - File: backend/ums_smart_revenue/org/channel_import_apply.py -> bulk
#     import consumer (get_group_by_cms_id / create_group / add_members).
#   - File: backend/ums_smart_revenue/api/groups.py -> HTTP routes that
#     translate ChannelGroupConflictError to 409.
# ============================================================================
"""Channel-group domain contract, typed errors, and in-memory registry."""

import threading
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import uuid4


class ChannelGroupOwnerReassignmentError(ValueError):
    """A write crossed a content-owner boundary it does not own.

    ``content_owner_id`` is what scopes CMS group sync: it decides which
    groups a sync may reconcile and which it may deactivate. Filling the
    column on an owner-NULL legacy row is adoption and is allowed; changing a
    row that already names an owner is not, because it would silently move the
    group between content owners and corrupt both sides' subsequent plans.
    Raised by the store so a call-site bug fails loudly instead of writing.

    Also raised by the sync apply layer when the locked re-read shows a group
    that was owner-NULL at plan time now claimed by someone else: the entry's
    scoping premise is falsified, so mirroring it would write another owner's
    group AND misattribute the audit row. The API maps this to a 409, the same
    treatment the import's cross-owner rejection gets.
    """


def require_adoptable_owner(current: str | None, incoming: str, *, group_id: str) -> None:
    """Allow filling an owner-NULL row (or a no-op re-stamp); reject a move."""
    if current is not None and current != incoming:
        raise ChannelGroupOwnerReassignmentError(
            f"channel group {group_id} already belongs to content owner {current!r}; "
            f"refusing to reassign it to {incoming!r}"
        )


class ChannelGroupNotFoundError(LookupError):
    """No channel group with the requested id exists for this tenant.

    The stores signal a missing row with a bare ``KeyError`` and let each
    route translate it, which works only because every caller is an HTTP
    handler. A service function is callable from a job, a script, or another
    service, so it raises this instead: the caller can distinguish "no such
    group" from any other ``KeyError`` bubbling out of store internals it does
    not own. Deliberately NOT a ``KeyError`` subclass — inheriting from it
    would let a bare ``except KeyError`` keep swallowing this silently, which
    is the ambiguity the typed error exists to remove.
    """


class ChannelGroupNoOwnerStampError(ValueError):
    """A clear was requested on a group that has no content-owner stamp.

    ``clear_content_owner`` is the one sanctioned eraser for a wrong stamp
    (admin recovery once the group's owner can no longer be fixed by an
    import or sync, both of which are adopt-only). Raising here instead of
    silently no-opping means a caller cannot mistake "nothing to clear" for
    "cleared" — the API layer maps this to a 409 telling the operator there
    was no stamp on the group they targeted.
    """


class ChannelGroupConflictError(ValueError):
    """A group write lost a uniqueness race (duplicate per-tenant cms_group_id).

    Raised instead of letting the database IntegrityError escape as a 500:
    two concurrent imports (or an import racing a group create) can both see
    a CMS key as missing and try to create it. The API layer maps this to a
    retryable 409.
    """


@dataclass(frozen=True)
class ChannelGroupEntry:
    id: str
    name: str
    group_type: str
    active: bool
    channel_ids: tuple[str, ...]
    cms_group_id: str | None = None
    content_owner_id: str | None = None

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "group_type": self.group_type,
            "active": self.active,
            "channel_ids": list(self.channel_ids),
            "cms_group_id": self.cms_group_id,
            "content_owner_id": self.content_owner_id,
        }


@dataclass(frozen=True)
class ClearedContentOwner:
    """The outcome of one ``clear_content_owner`` call, read under its lock.

    ``previous_content_owner_id`` is never ``None``: clearing an owner-NULL
    group raises ``ChannelGroupNoOwnerStampError`` instead of returning. The
    store returns it rather than leaving the caller to pre-read it, because a
    caller's read is NOT serialized against a concurrent adopt — only the
    store's ``for_update`` read is. An unlocked pre-read can observe a NULL
    stamp that an adopt then fills before the clear takes its lock, which
    would make the audit row understate the owner actually erased.
    """

    group: ChannelGroupEntry
    previous_content_owner_id: str


class ChannelGroupRegistryStore(Protocol):
    # The store's transactional backing, REQUIRED by the protocol — see
    # ChannelRegistryStore.sql_unit_of_work for the full contract
    # (PR #196 rounds 6+8, codex).
    sql_unit_of_work: object | None

    def list_groups(self) -> list[ChannelGroupEntry]:
        pass

    def list_groups_full(self) -> list[ChannelGroupEntry]:
        pass

    def list_synced_groups(self, *, content_owner_id: str | None = None) -> list[ChannelGroupEntry]:
        """Return every CMS-keyed group, optionally scoped to one owner.

        ``content_owner_id=None`` (the default) returns every synced group
        tenant-wide. CMS group sync MUST pass its content owner: groups carry
        no owner column of their own beyond content_owner_id stamped at
        create time, so an unscoped call would hand sync planning every OTHER
        owner's groups too, and any group missing from the CURRENT owner's
        upstream snapshot looks "vanished" and gets deactivated.
        """
        pass

    def get_group(self, group_id: str, *, for_update: bool = False) -> ChannelGroupEntry | None:
        """Return the group by id, or None.

        ``for_update`` row-locks the parent group — the membership
        serialization point — so a caller that diffs membership under the lock
        cannot have that diff invalidated by a concurrent add/remove before it
        writes. CMS group sync's apply uses it for exactly that.
        """

    def get_group_by_cms_id(
        self, cms_group_id: str, *, for_update: bool = False
    ) -> ChannelGroupEntry | None:
        """Return the tenant-scoped group carrying this CMS key, or None.

        Archived groups ARE returned so callers (import planning) can fail
        rows targeting them closed. ``for_update`` row-locks the group so a
        write-boundary active-state check cannot race a concurrent archive.
        """

    def list_archived_cms_group_ids(self, cms_group_ids: set[str]) -> set[str]:
        """Return the subset of CMS keys whose existing group is archived.

        One bulk lookup (no per-key round trips) so import planning can vet a
        full roster's group keys without a lookup-per-group query storm.
        Unknown keys are simply absent from the result.
        """

    def list_foreign_owner_cms_group_ids(
        self, cms_group_ids: set[str], *, content_owner_id: str
    ) -> set[str]:
        """Return the subset of CMS keys stamped to a DIFFERENT content owner.

        Owner-NULL and same-owner keys are excluded — both are attachable.
        One bulk lookup, matching ``list_archived_cms_group_ids``, so import
        planning can vet a whole roster's group keys in one query and surface
        cross-owner conflicts in the dry run instead of only at the write
        boundary.
        """

    def list_adoptable_cms_group_ids(self, cms_group_ids: set[str]) -> set[str]:
        """Return the subset of CMS keys whose existing group is owner-NULL.

        The exact complement of ``list_foreign_owner_cms_group_ids`` over the
        keys that resolve to a group. Both sets are refused by import
        planning, for different reasons: a foreign key would inject a channel
        into another owner's mirrored group, while an owner-NULL key would let
        a CSV cell decide which content owner's sync governs that group from
        then on. Unknown keys are absent from the result: a key with no group
        is a CREATE, and a group created by the import is stamped at birth —
        an ownership claim the request already carries.
        """

    # ========================================================================
    # Purpose: Bulk-classify a roster's CMS group keys by "this owner already
    #   holds it", so planning can label each row CREATE or JOIN without a
    #   lookup-per-group query storm.
    # Database/ORM: ChannelGroupORM (read-only; cms_group_id + content_owner_id
    #   under the per-tenant unique key). No membership loading, no writes.
    # Standards: One bounded SELECT for the whole key set, tenant-scoped, and
    #   the only one of the four bulk key lookups that REPORTS rather than
    #   refuses — its three siblings (archived, cross-owner, adoptable) all
    #   exist to fail rows closed. That difference is the point: without a
    #   non-refusing lookup, "exists and is mine" was unrepresentable, and the
    #   preview could not tell CREATE from JOIN (review #184).
    # Blast Radius: The preview's group-effect claim and the write-boundary
    #   recheck built on it. Read-only.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_import.py ->
    #     _planned_group_action consumes this set.
    # ========================================================================
    def list_owned_cms_group_ids(
        self, cms_group_ids: set[str], *, content_owner_id: str
    ) -> set[str]:
        """Return the subset of CMS keys already stamped to THIS content owner.

        The fourth and last of the matched bulk lookups, and the only one that
        is not a refusal: the other three name keys whose rows the planner
        FAILS, while this one names the keys the import will JOIN rather than
        CREATE. Without it the four are not exhaustive — a key absent from all
        of archived/foreign/adoptable is either an existing group of this
        owner's or no group at all, and those are the two outcomes the
        operator most needs told apart before approving. One bulk lookup, no
        membership loading, unknown keys absent — matching its siblings.
        """

    def get_active_member_channels(self, group_id: str) -> tuple[str, ...] | None:
        pass

    def create_group(
        self,
        *,
        name: str,
        group_type: str,
        channel_ids: list[str],
        cms_group_id: str | None = None,
        content_owner_id: str | None = None,
    ) -> ChannelGroupEntry:
        pass

    def update_group(
        self,
        *,
        group_id: str,
        name: str | None,
        active: bool | None,
        content_owner_id: str | None = None,
    ) -> ChannelGroupEntry:
        """Update a group's name, active state, and/or content owner.

        Every field is None-means-unchanged. ``content_owner_id`` exists so CMS
        group sync can ADOPT an owner-NULL legacy group once the upstream key
        proves ownership; it never reassigns a group that already carries an
        owner.
        """

    def clear_content_owner(self, *, group_id: str) -> ClearedContentOwner:
        """Erase a group's owner stamp, returning it to the adoptable pool.

        The adopt-only guard governs SETTING an owner; this is the one
        sanctioned eraser (admin recovery for a wrong stamp). Raises
        ChannelGroupNoOwnerStampError when there is nothing to clear.
        Returns the cleared group AND the owner id observed under the write
        lock, so the caller's audit row names what was actually erased.
        """

    def add_members(self, *, group_id: str, channel_ids: list[str]) -> ChannelGroupEntry:
        pass

    def remove_member(self, *, group_id: str, channel_id: str) -> ChannelGroupEntry:
        pass

    # ========================================================================
    # Purpose: The group half of the import's atomicity boundary — same
    #   contract as ChannelRegistryStore.transaction, whose block carries the
    #   full rationale; the two must not drift, because the bulk import
    #   enters BOTH around its two passes and a group-store boundary that
    #   behaved differently would undo one half of a refused import and keep
    #   the other (review #184, C2).
    # Database/ORM: ChannelGroupORM + membership rows in the SQL adapter's
    #   SAVEPOINT; a dict journal in the in-memory adapter.
    # Standards: SQL maps to a SAVEPOINT on the request session and never
    #   commits; the in-memory adapter journals its own writes per thread and
    #   replays them backwards on raise, so a foreign write interleaved
    #   mid-boundary survives. Exceptions always propagate.
    # Blast Radius: Whether a refused import leaves partial group writes on
    #   adapters without a database underneath. Production SQL end-state
    #   unchanged.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_registry.py -> the
    #     registry protocol method carrying the full contract.
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SAVEPOINT implementation.
    # ========================================================================
    def transaction(self) -> AbstractContextManager[None]:
        """Return a context manager making the wrapped writes all-or-nothing.

        See ``ChannelRegistryStore.transaction`` for the full contract this
        mirrors.

        Raises:
            RuntimeError: on same-store nesting — every implementation
                refuses it (the in-memory adapter per thread, the SQL
                adapter per store instance), exactly as the registry
                protocol documents.
        """
        ...


class ChannelGroupRegistry:
    # No SQL backing — see ChannelRegistry.sql_unit_of_work.
    sql_unit_of_work: object | None = None

    def __init__(self, groups: list[ChannelGroupEntry] | None = None):
        self._groups = {group.id: group for group in groups or []}
        # PER-THREAD boundary state, mirroring ChannelRegistry: this store is
        # a long-lived singleton on the no-database tier, and one thread's
        # journal must neither capture nor revert another's writes
        # (PR #196 round 2, codex). `undo` holds the active journal or None.
        self._txn = threading.local()
        # The store-wide lock — see ChannelRegistry.__init__ for the full
        # rationale (PR #196 rounds 3-5): a boundary holds it for its whole
        # duration and every public method, write AND read, takes it
        # (re-entrant), so another thread neither builds on nor OBSERVES an
        # open boundary's uncommitted state. Iterating readers also take
        # list(...) snapshots (round 4) as lock-independent iteration safety.
        self._lock = threading.RLock()

    # ========================================================================
    # Purpose: In-memory implementation of the group store's transaction
    #   boundary — journal this store's own writes on this thread, undo
    #   exactly those on raise. Mirror of ChannelRegistry.transaction, whose
    #   block carries the full journal-vs-snapshot rationale; the two must
    #   not drift.
    # Database/ORM: None (in-memory dict); sql_channel_groups.py maps the
    #   same protocol method to a SAVEPOINT.
    # Standards: Undo journal per thread; a foreign write staged by direct
    #   dict mutation survives, as a committed SQL row survives rollback.
    #   Fails loud on same-thread nesting.
    # Blast Radius: Whether a refused import leaves partial group writes on
    #   direct/test/bootstrap callers.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_registry.py -> the
    #     mirrored implementation and full rationale.
    #   - File: backend/ums_smart_revenue/org/channel_import_apply.py ->
    #     apply_channel_import, the boundary's consumer.
    # ========================================================================
    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Journal this store's own writes on this thread; undo them on raise.

        Compare-and-restore, mirroring ``ChannelRegistry.transaction`` (see
        its docstring for the full semantics): an entry restores its
        pre-image only while the key still holds the exact object this
        boundary wrote, so a foreign writer landing after us keeps its value
        — the SQL row-lock outcome (PR #196 round 2, qodo). The boundary
        holds the store lock, which all reads and writes take, so
        uncommitted boundary state is never observable off-thread.

        Raises:
            RuntimeError: when entered while this THREAD already holds an
                open boundary.
        """
        if getattr(self._txn, "undo", None) is not None:
            raise RuntimeError("ChannelGroupRegistry.transaction does not nest")
        with self._lock:
            undo: list[tuple[str, ChannelGroupEntry | None, ChannelGroupEntry]] = []
            self._txn.undo = undo
            try:
                yield
            except BaseException:
                for key, previous, written in reversed(undo):
                    if self._groups.get(key) is not written:
                        continue
                    if previous is None:
                        self._groups.pop(key, None)
                    else:
                        self._groups[key] = previous
                raise
            finally:
                self._txn.undo = None

    # ========================================================================
    # Purpose: Transaction bookkeeping — record one write's (key, pre-image,
    #   written-entry) triple into this thread's active undo journal, or do
    #   nothing when no boundary is open. Mirror of ChannelRegistry._journal,
    #   whose block carries the full calling contract.
    # Database/ORM: None (per-thread in-memory list).
    # Standards: Called by every write method just before mutating, under
    #   the store lock, with the exact object being installed.
    # Blast Radius: Whether a boundary rollback restores exactly this
    #   store's own writes.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/channel_registry.py -> the
    #     mirrored journal helper and full rationale.
    # ========================================================================
    def _journal(self, group_id: str, written: ChannelGroupEntry) -> None:
        """Record (key, pre-image, written-entry) for the active boundary, if any."""
        undo: list[tuple[str, ChannelGroupEntry | None, ChannelGroupEntry]] | None = getattr(
            self._txn, "undo", None
        )
        if undo is not None:
            undo.append((group_id, self._groups.get(group_id), written))

    # ========================================================================
    # Purpose: In-memory read — every group, ACTIVE OR NOT, sorted by name.
    # Database/ORM: None (dict scan; the SQL adapter issues the SELECT).
    # Standards: Committed read under the store lock — an open boundary's
    #   uncommitted writes are never observed off-thread; the list()
    #   snapshot keeps iteration safe independent of the lock discipline.
    #   DOCUMENTED DIVERGENCE: the SQL twin filters to ACTIVE groups; this
    #   tier keeps no group-activity filter, so a test asserting
    #   active-only listing must run the SQL tier.
    # Blast Radius: Read-only; groups API listing and scope selection.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation (active-only), which this deliberately does
    #     not mirror on group activity.
    # ========================================================================
    def list_groups(self) -> list[ChannelGroupEntry]:
        """Return every group, active or not, sorted by name.

        In-memory registry: every member is treated as active, so the full
        member set equals the active member set and this returns the same
        payload as ``list_groups_full``. Committed reads: takes the store
        lock, so an open boundary's uncommitted writes are never observed
        off-thread (see the lock note in ``__init__``).
        """
        with self._lock:
            return sorted(list(self._groups.values()), key=lambda group: group.name)

    # ========================================================================
    # Purpose: In-memory read — every group with FULL membership; equals
    #   list_groups here because every in-memory member counts as active.
    # Database/ORM: None (dict scan).
    # Standards: Committed read under the store lock — see list_groups,
    #   including its DOCUMENTED DIVERGENCE: inactive groups are returned
    #   here while the SQL twin lists active groups only (their difference
    #   on this tier is nil; on SQL it is full-vs-active MEMBER sets).
    # Blast Radius: Read-only; groups management authorization.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation (active groups, full members).
    # ========================================================================
    def list_groups_full(self) -> list[ChannelGroupEntry]:
        """Return every group with full membership, sorted by name.

        Identical to ``list_groups`` in memory (every member counts as
        active — see its docstring, including the committed-read note).
        """
        with self._lock:
            return sorted(list(self._groups.values()), key=lambda group: group.name)

    # ========================================================================
    # Purpose: In-memory read — every CMS-keyed group for sync planning,
    #   optionally scoped to one owner (owner-NULL rows included, so
    #   unstamped legacy groups stay reconcilable).
    # Database/ORM: None (dict scan).
    # Standards: Committed read under the store lock — see list_groups.
    # Blast Radius: Read-only; CMS group sync's planning input.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation this must answer identically to.
    # ========================================================================
    def list_synced_groups(self, *, content_owner_id: str | None = None) -> list[ChannelGroupEntry]:
        """Return every CMS-keyed group, active or not, for sync planning.

        Parity with the SQL store: a scoped call also returns owner-NULL rows
        so legacy/unstamped groups stay reconcilable instead of colliding on
        the tenant-wide unique cms_group_id.
        """
        with self._lock:
            return [
                group
                for group in list(self._groups.values())
                if group.cms_group_id is not None
                and (
                    content_owner_id is None
                    or group.content_owner_id is None
                    or group.content_owner_id == content_owner_id
                )
            ]

    # ========================================================================
    # Purpose: In-memory read — one group by id.
    # Database/ORM: None (single-key dict get).
    # Standards: Committed read under the store lock. ``for_update`` is a
    #   documented no-op divergence: the store lock already serializes this
    #   read against writers, while the SQL implementation takes the real
    #   FOR NO KEY UPDATE row lock (the membership serialization point).
    # Blast Radius: Read-only; route 404 decisions and membership diffs.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation this must answer identically to.
    # ========================================================================
    def get_group(self, group_id: str, *, for_update: bool = False) -> ChannelGroupEntry | None:
        """Return the group by id, or None.

        ``for_update`` is a no-op in memory: the store lock every method
        takes already serializes this read against writers, matching
        get_group_by_cms_id's documented divergence; the SQL implementation
        takes the real FOR NO KEY UPDATE row lock.
        """
        with self._lock:
            return self._groups.get(group_id)

    # ========================================================================
    # Purpose: In-memory read — the group carrying one CMS key, if any.
    # Database/ORM: None (dict scan under the store lock).
    # Standards: Committed read; ``for_update`` is the same documented
    #   no-op divergence as get_group. The list() snapshot keeps the scan
    #   safe independent of the lock discipline.
    # Blast Radius: Read-only; the import's group-effect checks and
    #   create_group's in-lock duplicate-CMS-key check both ride on it.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation this must answer identically to.
    # ========================================================================
    def get_group_by_cms_id(
        self, cms_group_id: str, *, for_update: bool = False
    ) -> ChannelGroupEntry | None:
        """Return the group carrying this CMS key, or None.

        ``for_update`` is a no-op in memory (the store lock already
        serializes this read against writers); the SQL implementation
        row-locks the group so an archived-state check at the write
        boundary cannot race a concurrent archive.
        """
        with self._lock:
            for group in list(self._groups.values()):
                if group.cms_group_id == cms_group_id:
                    return group
            return None

    # ========================================================================
    # Purpose: In-memory read — which of the requested CMS keys belong to
    #   an ARCHIVED group (import planning fails those rows closed).
    # Database/ORM: None (dict scan under the store lock).
    # Standards: Committed read; list() snapshot — see list_groups.
    # Blast Radius: Read-only; import preview/plan refusal input.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation this must answer identically to.
    # ========================================================================
    def list_archived_cms_group_ids(self, cms_group_ids: set[str]) -> set[str]:
        """Return the subset of CMS keys whose existing group is archived."""
        with self._lock:
            return {
                group.cms_group_id
                for group in list(self._groups.values())
                if group.cms_group_id in cms_group_ids and not group.active
            }

    # ========================================================================
    # Purpose: In-memory read — which of the requested CMS keys are stamped
    #   to a DIFFERENT content owner (the import refuses to touch them).
    # Database/ORM: None (dict scan under the store lock).
    # Standards: Committed read; list() snapshot — see list_groups.
    # Blast Radius: Read-only; cross-owner refusal input for the import.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation this must answer identically to.
    # ========================================================================
    def list_foreign_owner_cms_group_ids(
        self, cms_group_ids: set[str], *, content_owner_id: str
    ) -> set[str]:
        """Return the subset of CMS keys stamped to a different content owner."""
        with self._lock:
            return {
                group.cms_group_id
                for group in list(self._groups.values())
                if group.cms_group_id in cms_group_ids
                and group.content_owner_id is not None
                and group.content_owner_id != content_owner_id
            }

    # ========================================================================
    # Purpose: In-memory read — which of the requested CMS keys belong to
    #   an owner-NULL group (adoptable: the import may stamp, never move).
    # Database/ORM: None (dict scan under the store lock).
    # Standards: Committed read; list() snapshot — see list_groups.
    # Blast Radius: Read-only; the import's JOIN-vs-adopt labeling.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation this must answer identically to.
    # ========================================================================
    def list_adoptable_cms_group_ids(self, cms_group_ids: set[str]) -> set[str]:
        """Return the subset of CMS keys whose existing group is owner-NULL."""
        with self._lock:
            return {
                group.cms_group_id
                for group in list(self._groups.values())
                if group.cms_group_id in cms_group_ids and group.content_owner_id is None
            }

    # ========================================================================
    # Purpose: In-memory implementation of the "this owner already holds it"
    #   lookup — the read that lets the preview label a roster's Group_ID rows
    #   CREATE or JOIN.
    # Database/ORM: None — a scan of the in-memory group map. The SQL adapter
    #   is the one that issues the bounded, tenant-scoped SELECT; this exists
    #   so the planner and its guards can be exercised without a database.
    # Standards: Must answer IDENTICALLY to sql_channel_groups.py, because the
    #   planner's CREATE label is decided by ABSENCE from this set. Two
    #   conditions carry that: membership in the requested key set, and an
    #   EXACT content_owner_id match — an owner-NULL group is deliberately not
    #   "mine", so it stays absent here and is refused by the adoptable lookup
    #   rather than silently planned as a CREATE that then collides on the
    #   per-tenant unique cms_group_id. Read-only, no membership loading, and
    #   unknown keys are simply absent (review #184).
    # Blast Radius: Whether the preview promises "new group" or "adds to
    #   existing", and — because the write boundary re-checks that promise —
    #   whether a diverged effect 409s the whole import. No writes.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the SQL
    #     adapter this must agree with.
    #   - File: backend/ums_smart_revenue/org/channel_import.py ->
    #     _planned_group_action, the sole consumer of the returned set.
    # ========================================================================
    def list_owned_cms_group_ids(
        self, cms_group_ids: set[str], *, content_owner_id: str
    ) -> set[str]:
        """Return the subset of CMS keys already stamped to this content owner."""
        with self._lock:
            return {
                group.cms_group_id
                for group in list(self._groups.values())
                if group.cms_group_id in cms_group_ids
                and group.content_owner_id == content_owner_id
            }

    # ========================================================================
    # Purpose: In-memory read — a group's member channel ids, or None for a
    #   missing group.
    # Database/ORM: None (single-key dict get under the store lock).
    # Standards: Committed read — see list_groups. DOCUMENTED DIVERGENCE:
    #   returns the stored member ids UNFILTERED — this tier has no channel
    #   registry join, so every member counts as active — while the SQL
    #   counterpart filters members by YouTubeChannelORM.active. A test
    #   asserting active-member filtering must run the SQL tier.
    # Blast Radius: Read-only; the revenue scope selector's member source.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation (active members only).
    # ========================================================================
    def get_active_member_channels(self, group_id: str) -> tuple[str, ...] | None:
        """Return active member channel ids for a group, or None if the group is missing.

        In-memory implementation: every member is treated as active. The
        SQL counterpart filters by YouTubeChannelORM.active.
        """
        with self._lock:
            group = self._groups.get(group_id)
            if group is None:
                return None
            return group.channel_ids

    # ========================================================================
    # Purpose: In-memory write — mint one group with de-duplicated,
    #   insertion-ordered members.
    # Database/ORM: None (dict insert; the SQL adapter owns the INSERT and
    #   the per-tenant unique cms_group_id constraint).
    # Standards: A duplicate CMS key raises the same typed conflict the SQL
    #   unique key produces, checked INSIDE the store lock because
    #   check-then-write races too. Journals the write so an open boundary
    #   can undo it.
    # Blast Radius: Group inventory; the import's CREATE group actions.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation this must answer identically to.
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
        with self._lock:
            # Parity with the SQL store's per-tenant unique key: a duplicate
            # CMS key must fail typed here too, not silently create a second
            # group. Checked INSIDE the lock — check-then-write races too.
            if cms_group_id is not None and self.get_group_by_cms_id(cms_group_id) is not None:
                raise ChannelGroupConflictError(
                    f"channel group already exists for cms_group_id: {cms_group_id}"
                )
            group = ChannelGroupEntry(
                id=str(uuid4()),
                name=name,
                group_type=group_type,
                active=True,
                channel_ids=tuple(dict.fromkeys(channel_ids)),
                cms_group_id=cms_group_id,
                content_owner_id=content_owner_id,
            )
            self._journal(group.id, group)
            self._groups[group.id] = group
            return group

    # ========================================================================
    # Purpose: In-memory write — rename, (de)activate, or owner-stamp one
    #   group; None leaves a field untouched.
    # Database/ORM: None (dict replace).
    # Standards: Owner changes are ADOPT-ONLY via require_adoptable_owner —
    #   filling an owner-NULL row is allowed, moving a stamped one raises,
    #   exactly as the SQL store enforces. Store lock + journal — see
    #   create_group.
    # Blast Radius: Group identity/lifecycle and owner scoping for sync.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation this must answer identically to.
    # ========================================================================
    def update_group(
        self,
        *,
        group_id: str,
        name: str | None,
        active: bool | None,
        content_owner_id: str | None = None,
    ) -> ChannelGroupEntry:
        with self._lock:
            group = self._require_group(group_id)
            # Parity with the SQL store: adopt-only, reassignment raises.
            if content_owner_id is not None:
                require_adoptable_owner(
                    group.content_owner_id, content_owner_id, group_id=group_id
                )
            updated = replace(
                group,
                name=name if name is not None else group.name,
                active=active if active is not None else group.active,
                content_owner_id=(
                    content_owner_id if content_owner_id is not None else group.content_owner_id
                ),
            )
            self._journal(group_id, updated)
            self._groups[group_id] = updated
            return updated

    # ========================================================================
    # Purpose: In-memory write — the one sanctioned eraser for a wrong
    #   owner stamp, returning the group to the adoptable pool.
    # Database/ORM: None (dict replace).
    # Standards: Clearing an owner-NULL group raises the typed no-stamp
    #   error (a caller must not mistake "nothing to clear" for "cleared");
    #   the erased owner id is returned from UNDER the lock so audit cannot
    #   understate it. Store lock + journal — see create_group.
    # Blast Radius: Owner scoping for sync; admin recovery path.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation this must answer identically to.
    # ========================================================================
    def clear_content_owner(self, *, group_id: str) -> ClearedContentOwner:
        """Erase a group's owner stamp, returning it to the adoptable pool.

        Does NOT route through require_adoptable_owner: that guard governs
        SETTING an owner, not erasing one. Reports the erased owner id
        alongside the cleared group, matching the SQL store's contract.
        """
        with self._lock:
            group = self._require_group(group_id)
            previous_content_owner_id = group.content_owner_id
            if previous_content_owner_id is None:
                raise ChannelGroupNoOwnerStampError(
                    f"channel group {group_id} has no content-owner stamp to clear"
                )
            updated = replace(group, content_owner_id=None)
            self._journal(group_id, updated)
            self._groups[group_id] = updated
            return ClearedContentOwner(
                group=updated, previous_content_owner_id=previous_content_owner_id
            )

    # ========================================================================
    # Purpose: In-memory write — append members to one group, de-duplicated
    #   against the existing set, insertion order preserved.
    # Database/ORM: None (dict replace; the SQL adapter owns the member
    #   INSERTs and their primary-key de-duplication).
    # Standards: Missing group raises the typed not-found via
    #   _require_group. Store lock + journal — see create_group.
    # Blast Radius: Group membership; the import's JOIN group actions.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation this must answer identically to.
    # ========================================================================
    def add_members(self, *, group_id: str, channel_ids: list[str]) -> ChannelGroupEntry:
        with self._lock:
            group = self._require_group(group_id)
            updated = replace(
                group, channel_ids=tuple(dict.fromkeys([*group.channel_ids, *channel_ids]))
            )
            self._journal(group_id, updated)
            self._groups[group_id] = updated
            return updated

    # ========================================================================
    # Purpose: In-memory write — drop one member from one group; removing
    #   an absent member is a no-op write of the same membership.
    # Database/ORM: None (dict replace; the SQL adapter owns the DELETE).
    # Standards: Missing group raises the typed not-found via
    #   _require_group. Store lock + journal — see create_group.
    # Blast Radius: Group membership; groups management API.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_groups.py -> the
    #     SQL implementation this must answer identically to.
    # ========================================================================
    def remove_member(self, *, group_id: str, channel_id: str) -> ChannelGroupEntry:
        with self._lock:
            group = self._require_group(group_id)
            updated = replace(
                group,
                channel_ids=tuple(
                    channel for channel in group.channel_ids if channel != channel_id
                ),
            )
            self._journal(group_id, updated)
            self._groups[group_id] = updated
            return updated

    def _require_group(self, group_id: str) -> ChannelGroupEntry:
        group = self.get_group(group_id)
        if group is None:
            raise KeyError(f"Group not found: {group_id}")
        return group
