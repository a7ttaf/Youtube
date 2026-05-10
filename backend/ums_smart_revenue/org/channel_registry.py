from dataclasses import dataclass
from typing import Protocol

from ums_smart_revenue.org.bootstrap_registry import BOOTSTRAP_CHANNELS


@dataclass(frozen=True)
class ChannelRegistryEntry:
    youtube_channel_id: str
    channel_name: str
    primary_company_id: str | None
    cms_status: str
    revenue_required: bool
    active: bool = True

    def to_api(self) -> dict[str, object]:
        return {
            "youtube_channel_id": self.youtube_channel_id,
            "channel_name": self.channel_name,
            "primary_company_id": self.primary_company_id,
            "cms_status": self.cms_status,
            "revenue_required": self.revenue_required,
            "active": self.active,
        }


class ChannelRegistryStore(Protocol):
    def list_channels(self) -> list[ChannelRegistryEntry]:
        pass

    def get_channel(self, youtube_channel_id: str) -> ChannelRegistryEntry | None:
        pass

    def create_channel(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        primary_company_id: str | None,
        cms_status: str,
        revenue_required: bool,
    ) -> ChannelRegistryEntry:
        pass

    def update_mapping(self, *, youtube_channel_id: str, primary_company_id: str | None) -> ChannelRegistryEntry:
        pass


class ChannelRegistry:
    def __init__(self, channels: list[ChannelRegistryEntry] | None = None):
        self._channels = {channel.youtube_channel_id: channel for channel in channels or []}

    def list_channels(self) -> list[ChannelRegistryEntry]:
        return sorted(self._channels.values(), key=lambda channel: channel.youtube_channel_id)

    def get_channel(self, youtube_channel_id: str) -> ChannelRegistryEntry | None:
        return self._channels.get(youtube_channel_id)

    def create_channel(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        primary_company_id: str,
        cms_status: str,
        revenue_required: bool,
    ) -> ChannelRegistryEntry:
        if youtube_channel_id in self._channels:
            raise ValueError(f"Channel already exists: {youtube_channel_id}")
        channel = ChannelRegistryEntry(
            youtube_channel_id=youtube_channel_id,
            channel_name=channel_name,
            primary_company_id=primary_company_id,
            cms_status=cms_status,
            revenue_required=revenue_required,
        )
        self._channels[youtube_channel_id] = channel
        return channel

    def update_mapping(self, *, youtube_channel_id: str, primary_company_id: str) -> ChannelRegistryEntry:
        existing = self._channels.get(youtube_channel_id)
        if existing is None:
            raise KeyError(youtube_channel_id)
        updated = ChannelRegistryEntry(
            youtube_channel_id=existing.youtube_channel_id,
            channel_name=existing.channel_name,
            primary_company_id=primary_company_id,
            cms_status=existing.cms_status,
            revenue_required=existing.revenue_required,
            active=existing.active,
        )
        self._channels[youtube_channel_id] = updated
        return updated


def bootstrap_channel_registry() -> ChannelRegistry:
    channels = [
        ChannelRegistryEntry(
            youtube_channel_id=channel.youtube_channel_id,
            channel_name=channel.youtube_channel_id,
            primary_company_id=channel.primary_org_unit_id,
            cms_status="UNKNOWN",
            revenue_required=True,
            active=channel.active,
        )
        for channel in BOOTSTRAP_CHANNELS
    ]
    return ChannelRegistry(channels)
