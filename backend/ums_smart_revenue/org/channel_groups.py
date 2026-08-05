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

from dataclasses import dataclass, replace
from typing import Protocol
from uuid import uuid4


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

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "group_type": self.group_type,
            "active": self.active,
            "channel_ids": list(self.channel_ids),
            "cms_group_id": self.cms_group_id,
        }


class ChannelGroupRegistryStore(Protocol):
    def list_groups(self) -> list[ChannelGroupEntry]:
        pass

    def list_groups_full(self) -> list[ChannelGroupEntry]:
        pass

    def list_synced_groups(self) -> list[ChannelGroupEntry]:
        pass

    def get_group(self, group_id: str) -> ChannelGroupEntry | None:
        pass

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

    def get_active_member_channels(self, group_id: str) -> tuple[str, ...] | None:
        pass

    def create_group(
        self,
        *,
        name: str,
        group_type: str,
        channel_ids: list[str],
        cms_group_id: str | None = None,
    ) -> ChannelGroupEntry:
        pass

    def update_group(
        self, *, group_id: str, name: str | None, active: bool | None
    ) -> ChannelGroupEntry:
        pass

    def add_members(self, *, group_id: str, channel_ids: list[str]) -> ChannelGroupEntry:
        pass

    def remove_member(self, *, group_id: str, channel_id: str) -> ChannelGroupEntry:
        pass


class ChannelGroupRegistry:
    def __init__(self, groups: list[ChannelGroupEntry] | None = None):
        self._groups = {group.id: group for group in groups or []}

    def list_groups(self) -> list[ChannelGroupEntry]:
        # In-memory registry: every member is treated as active. The full
        # member set is the same as the active member set, so list_groups
        # and list_groups_full return the same payload.
        return sorted(self._groups.values(), key=lambda group: group.name)

    def list_groups_full(self) -> list[ChannelGroupEntry]:
        return sorted(self._groups.values(), key=lambda group: group.name)

    def list_synced_groups(self) -> list[ChannelGroupEntry]:
        """Return every CMS-keyed group, active or not, for sync planning."""
        return [group for group in self._groups.values() if group.cms_group_id is not None]

    def get_group(self, group_id: str) -> ChannelGroupEntry | None:
        return self._groups.get(group_id)

    def get_group_by_cms_id(
        self, cms_group_id: str, *, for_update: bool = False
    ) -> ChannelGroupEntry | None:
        """Return the group carrying this CMS key, or None.

        ``for_update`` is a no-op in memory (single-threaded test registry);
        the SQL implementation row-locks the group so an archived-state check
        at the write boundary cannot race a concurrent archive.
        """
        for group in self._groups.values():
            if group.cms_group_id == cms_group_id:
                return group
        return None

    def list_archived_cms_group_ids(self, cms_group_ids: set[str]) -> set[str]:
        """Return the subset of CMS keys whose existing group is archived."""
        return {
            group.cms_group_id
            for group in self._groups.values()
            if group.cms_group_id in cms_group_ids and not group.active
        }

    def get_active_member_channels(self, group_id: str) -> tuple[str, ...] | None:
        """Return active member channel ids for a group, or None if the group is missing.

        In-memory implementation: every member is treated as active. The
        SQL counterpart filters by YouTubeChannelORM.active.
        """
        group = self._groups.get(group_id)
        if group is None:
            return None
        return group.channel_ids

    def create_group(
        self,
        *,
        name: str,
        group_type: str,
        channel_ids: list[str],
        cms_group_id: str | None = None,
    ) -> ChannelGroupEntry:
        # Parity with the SQL store's per-tenant unique key: a duplicate CMS
        # key must fail typed here too, not silently create a second group.
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
        )
        self._groups[group.id] = group
        return group

    def update_group(
        self, *, group_id: str, name: str | None, active: bool | None
    ) -> ChannelGroupEntry:
        group = self._require_group(group_id)
        updated = replace(
            group,
            name=name if name is not None else group.name,
            active=active if active is not None else group.active,
        )
        self._groups[group_id] = updated
        return updated

    def add_members(self, *, group_id: str, channel_ids: list[str]) -> ChannelGroupEntry:
        group = self._require_group(group_id)
        updated = replace(
            group, channel_ids=tuple(dict.fromkeys([*group.channel_ids, *channel_ids]))
        )
        self._groups[group_id] = updated
        return updated

    def remove_member(self, *, group_id: str, channel_id: str) -> ChannelGroupEntry:
        group = self._require_group(group_id)
        updated = replace(
            group,
            channel_ids=tuple(channel for channel in group.channel_ids if channel != channel_id),
        )
        self._groups[group_id] = updated
        return updated

    def _require_group(self, group_id: str) -> ChannelGroupEntry:
        group = self.get_group(group_id)
        if group is None:
            raise KeyError(f"Group not found: {group_id}")
        return group
