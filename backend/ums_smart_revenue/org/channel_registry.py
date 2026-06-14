from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ums_smart_revenue.org.bootstrap_registry import BOOTSTRAP_CHANNELS


@dataclass(frozen=True)
class ChannelRegistryEntry:
    youtube_channel_id: str
    channel_name: str
    primary_company_id: str | None
    cms_status: str
    revenue_required: bool
    content_owner_id: str | None = None
    revenue_source_status: str = "MISSING_REVENUE_SOURCE"
    active: bool = True

    def to_api(self) -> dict[str, object]:
        return {
            "youtube_channel_id": self.youtube_channel_id,
            "channel_name": self.channel_name,
            "primary_company_id": self.primary_company_id,
            "cms_status": self.cms_status,
            "content_owner_id": self.content_owner_id,
            "revenue_required": self.revenue_required,
            "revenue_source_status": self.revenue_source_status,
            "active": self.active,
        }


class ChannelRegistryError(ValueError):
    pass


class ChannelRegistryConflictError(ChannelRegistryError):
    pass


class ChannelRegistryValidationError(ChannelRegistryError):
    pass


class ChannelMappingLockedMonthError(ChannelRegistryError):
    # ========================================================================
    # Purpose: Signal that a channel mapping change is blocked because the
    #   channel carries revenue facts in a LOCKED finance month, so re-parenting
    #   it would silently rewrite that closed month's company/sector attribution.
    # Database/ORM: None (raised by the SQL registry after a read-only check on
    #   MonthlyChannelRevenueFactORM x FinanceMonthCloseORM).
    # Standards: Typed domain error; the route boundary maps it to HTTP 409.
    # Blast Radius: Finance attribution integrity, audit (a rejected change must
    #   not be audited). No Neo4j, no exports.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_registry.py ->
    #       raised by SqlAlchemyChannelRegistry.update_mapping.
    #   - File: backend/ums_smart_revenue/api/channels.py -> translated to 409.
    # ========================================================================
    pass


class ChannelRegistryStore(Protocol):
    def list_channels(self) -> list[ChannelRegistryEntry]:
        pass

    def list_channels_by_ids(self, youtube_channel_ids: set[str]) -> list[ChannelRegistryEntry]:
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

    def update_mapping(
        self, *, youtube_channel_id: str, primary_company_id: str | None
    ) -> ChannelRegistryEntry:
        pass


class ChannelRegistry:
    def __init__(self, channels: list[ChannelRegistryEntry] | None = None):
        self._channels: dict[str, ChannelRegistryEntry] = {}
        for channel in channels or []:
            if channel.youtube_channel_id in self._channels:
                raise ChannelRegistryConflictError(
                    f"Duplicate channel id: {channel.youtube_channel_id}"
                )
            self._channels[channel.youtube_channel_id] = channel

    def list_channels(self) -> list[ChannelRegistryEntry]:
        return sorted(
            [channel for channel in self._channels.values() if channel.active],
            key=lambda channel: channel.youtube_channel_id,
        )

    def list_channels_by_ids(self, youtube_channel_ids: set[str]) -> list[ChannelRegistryEntry]:
        return sorted(
            [
                channel
                for channel_id, channel in self._channels.items()
                if channel_id in youtube_channel_ids and channel.active
            ],
            key=lambda channel: channel.youtube_channel_id,
        )

    def get_channel(self, youtube_channel_id: str) -> ChannelRegistryEntry | None:
        return self._channels.get(youtube_channel_id)

    def create_channel(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        primary_company_id: str | None,
        cms_status: str,
        revenue_required: bool,
    ) -> ChannelRegistryEntry:
        normalized_company_id = _parse_optional_uuid(primary_company_id, "primary_company_id")
        if youtube_channel_id in self._channels:
            raise ChannelRegistryConflictError(f"Channel already exists: {youtube_channel_id}")
        initial_revenue_source_status = (
            "MISSING_REVENUE_SOURCE" if revenue_required else "PERFORMANCE_ONLY"
        )
        channel = ChannelRegistryEntry(
            youtube_channel_id=youtube_channel_id,
            channel_name=channel_name,
            primary_company_id=normalized_company_id,
            cms_status=cms_status,
            revenue_required=revenue_required,
            revenue_source_status=initial_revenue_source_status,
        )
        self._channels[youtube_channel_id] = channel
        return channel

    def update_mapping(
        self, *, youtube_channel_id: str, primary_company_id: str | None
    ) -> ChannelRegistryEntry:
        normalized_company_id = _parse_optional_uuid(primary_company_id, "primary_company_id")
        existing = self._channels.get(youtube_channel_id)
        if existing is None:
            raise KeyError(youtube_channel_id)
        updated = ChannelRegistryEntry(
            youtube_channel_id=existing.youtube_channel_id,
            channel_name=existing.channel_name,
            primary_company_id=normalized_company_id,
            cms_status=existing.cms_status,
            revenue_required=existing.revenue_required,
            content_owner_id=existing.content_owner_id,
            revenue_source_status=existing.revenue_source_status,
            active=existing.active,
        )
        self._channels[youtube_channel_id] = updated
        return updated


def bootstrap_channel_registry() -> ChannelRegistry:
    channels = [
        ChannelRegistryEntry(
            youtube_channel_id=channel.youtube_channel_id,
            channel_name=channel.youtube_channel_id,
            primary_company_id=_parse_optional_uuid(
                channel.primary_org_unit_id, "primary_company_id"
            ),
            cms_status="UNKNOWN",
            revenue_required=True,
            content_owner_id=None,
            revenue_source_status="MISSING_REVENUE_SOURCE",
            active=channel.active,
        )
        for channel in BOOTSTRAP_CHANNELS
    ]
    return ChannelRegistry(channels)


def _parse_optional_uuid(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    try:
        return str(UUID(value))
    except ValueError as exc:
        raise ChannelRegistryValidationError(f"{field_name} must be a valid UUID") from exc
