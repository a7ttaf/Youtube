from dataclasses import dataclass, replace
from typing import Protocol
from uuid import uuid4


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

    def get_group(self, group_id: str) -> ChannelGroupEntry | None:
        pass

    def get_group_by_cms_id(
        self, cms_group_id: str, *, for_update: bool = False
    ) -> ChannelGroupEntry | None:
        pass

    def list_archived_cms_group_ids(self, cms_group_ids: set[str]) -> set[str]:
        pass

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
