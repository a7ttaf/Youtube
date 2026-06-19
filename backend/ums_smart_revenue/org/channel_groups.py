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

    def to_api(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "group_type": self.group_type,
            "active": self.active,
            "channel_ids": list(self.channel_ids),
        }


class ChannelGroupRegistryStore(Protocol):
    def list_groups(self) -> list[ChannelGroupEntry]:
        pass

    def get_group(self, group_id: str) -> ChannelGroupEntry | None:
        pass

    def get_active_member_channels(self, group_id: str) -> tuple[str, ...] | None:
        pass

    def create_group(
        self, *, name: str, group_type: str, channel_ids: list[str]
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
        return sorted(self._groups.values(), key=lambda group: group.name)

    def get_group(self, group_id: str) -> ChannelGroupEntry | None:
        return self._groups.get(group_id)

    def get_active_member_channels(self, group_id: str) -> tuple[str, ...] | None:
        group = self._groups.get(group_id)
        if group is None:
            return None
        return group.channel_ids

    def create_group(
        self, *, name: str, group_type: str, channel_ids: list[str]
    ) -> ChannelGroupEntry:
        group = ChannelGroupEntry(
            id=str(uuid4()),
            name=name,
            group_type=group_type,
            active=True,
            channel_ids=tuple(dict.fromkeys(channel_ids)),
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
