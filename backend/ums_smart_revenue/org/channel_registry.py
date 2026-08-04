# ============================================================================
# Purpose: Channel registry domain contract — the ChannelRegistryEntry value
#   object, the ChannelRegistryStore protocol, typed registry errors, the
#   in-memory reference implementation, and the shared derivation/
#   normalization helpers both implementations use.
# Database/ORM: None here; backend/ums_smart_revenue/org/sql_channel_registry.py
#   is the SQL implementation of the same protocol.
# Standards: Typed domain errors (ChannelRegistryError subclasses); frozen
#   dataclass entries; derivation logic shared via helpers so the in-memory
#   and SQL registries cannot drift.
# Blast Radius: Channel inventory/mapping semantics everywhere the registry
#   protocol is consumed (channel APIs, bulk import, connectors targeting).
# Connections:
#   - File: backend/ums_smart_revenue/org/sql_channel_registry.py -> SQL impl.
#   - File: backend/ums_smart_revenue/api/channels.py -> route consumers.
# ============================================================================
"""Channel registry domain contract, errors, and in-memory implementation."""

from dataclasses import dataclass, replace
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


class ChannelRevenueRequirementLockedMonthError(ChannelRegistryError):
    # ========================================================================
    # Purpose: Signal that flipping a channel's revenue_required flag ON is
    #   blocked because a LOCKED finance month has no revenue fact for it.
    #   Month-close readiness evaluates the CURRENT flag, so the flip would
    #   retroactively make an already-finalized month report a missing
    #   required fact and no longer satisfy the conditions it was locked
    #   under.
    # Database/ORM: None (raised by the SQL registry after a read-only check
    #   on FinanceMonthCloseORM x MonthlyChannelRevenueFactORM).
    # Standards: Typed domain error; the import route maps it to HTTP 409.
    #   Mirrors ChannelMappingLockedMonthError above.
    # Blast Radius: Finance close integrity, audit (a rejected flip must not
    #   be audited). No Neo4j, no exports.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/sql_channel_registry.py ->
    #       raised by SqlAlchemyChannelRegistry.update_inventory.
    #   - File: backend/ums_smart_revenue/finance/month_close_readiness.py ->
    #       the readiness query this guard keeps stable for LOCKED months.
    # ========================================================================
    pass


class ChannelRegistryStore(Protocol):
    def list_channels(self) -> list[ChannelRegistryEntry]:
        pass

    def list_channels_by_ids(
        self, youtube_channel_ids: set[str], *, include_inactive: bool = False
    ) -> list[ChannelRegistryEntry]:
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
        content_owner_id: str | None = None,
    ) -> ChannelRegistryEntry:
        pass

    def update_mapping(
        self, *, youtube_channel_id: str, primary_company_id: str | None
    ) -> ChannelRegistryEntry:
        pass

    def update_content_owner(
        self, *, youtube_channel_id: str, content_owner_id: str | None
    ) -> ChannelRegistryEntry:
        pass

    def update_inventory(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        cms_status: str,
        content_owner_id: str | None,
        revenue_required: bool,
    ) -> tuple[ChannelRegistryEntry, ChannelRegistryEntry]:
        """Replace a channel's inventory fields from an authoritative import row.

        Returns ``(previous, updated)`` where ``previous`` is the row's state
        observed at the write boundary (re-read under a row lock where the
        backend supports it), NOT the caller's possibly-stale planning
        snapshot — audit trails must record the values actually replaced.
        """


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

    def list_channels_by_ids(
        self, youtube_channel_ids: set[str], *, include_inactive: bool = False
    ) -> list[ChannelRegistryEntry]:
        return sorted(
            [
                channel
                for channel_id, channel in self._channels.items()
                if channel_id in youtube_channel_ids and (channel.active or include_inactive)
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
        content_owner_id: str | None = None,
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
            content_owner_id=normalize_optional_content_owner(content_owner_id),
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

    def update_content_owner(
        self, *, youtube_channel_id: str, content_owner_id: str | None
    ) -> ChannelRegistryEntry:
        existing = self._channels.get(youtube_channel_id)
        if existing is None:
            raise KeyError(youtube_channel_id)
        updated = ChannelRegistryEntry(
            youtube_channel_id=existing.youtube_channel_id,
            channel_name=existing.channel_name,
            primary_company_id=existing.primary_company_id,
            cms_status=existing.cms_status,
            revenue_required=existing.revenue_required,
            content_owner_id=normalize_optional_content_owner(content_owner_id),
            revenue_source_status=existing.revenue_source_status,
            active=existing.active,
        )
        self._channels[youtube_channel_id] = updated
        return updated

    def update_inventory(
        self,
        *,
        youtube_channel_id: str,
        channel_name: str,
        cms_status: str,
        content_owner_id: str | None,
        revenue_required: bool,
    ) -> tuple[ChannelRegistryEntry, ChannelRegistryEntry]:
        """Replace a channel's inventory fields from an authoritative import row.

        Returns ``(previous, updated)`` so audit trails record the values this
        write actually replaced (mirrors the SQL registry's write-boundary
        re-read; in memory the current entry IS the write-boundary state).
        """
        current = self._channels.get(youtube_channel_id)
        if current is None:
            raise ChannelRegistryValidationError(f"Unknown channel: {youtube_channel_id}")
        updated = replace(
            current,
            channel_name=channel_name,
            cms_status=cms_status,
            content_owner_id=content_owner_id,
            revenue_required=revenue_required,
            # Re-derive the source status only when revenue_required actually
            # flips; an unrelated inventory refresh must not clobber a proven
            # OFFICIAL_CMS_REVENUE / OFFICIAL_MANUAL_IMPORT classification back
            # to MISSING_REVENUE_SOURCE.
            revenue_source_status=derive_revenue_source_status(
                current_status=current.revenue_source_status,
                current_revenue_required=current.revenue_required,
                revenue_required=revenue_required,
            ),
        )
        self._channels[youtube_channel_id] = updated
        return current, updated


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


# ============================================================================
# Purpose: Single source of truth for how an inventory update derives the
#   channel's revenue_source_status — the finance-facing classification that
#   marks a channel's revenue evidence as official, missing, or not required.
# Database/ORM: None (pure function); both registry implementations call it.
# Standards: Re-derive ONLY on a revenue_required flip; preserve otherwise, so
#   a roster refresh can never downgrade OFFICIAL_CMS_REVENUE /
#   OFFICIAL_MANUAL_IMPORT back to MISSING_REVENUE_SOURCE (review #159
#   r3706996021).
# Blast Radius: Channel revenue-source classification feeding missing-source
#   monitors and registry issue feeds. No finance totals, no allocation.
# Connections:
#   - File: backend/ums_smart_revenue/org/sql_channel_registry.py ->
#     update_inventory caller.
#   - File: backend/ums_smart_revenue/org/channel_registry.py ->
#     ChannelRegistry.update_inventory caller (same module, above).
# ============================================================================
def derive_revenue_source_status(
    *,
    current_status: str,
    current_revenue_required: bool,
    revenue_required: bool,
) -> str:
    """Return the revenue_source_status an inventory update should persist.

    The status is re-derived ONLY when ``revenue_required`` changes: flipping
    to required starts the channel at MISSING_REVENUE_SOURCE, flipping to
    not-required parks it at PERFORMANCE_ONLY. When the flag is unchanged the
    existing status is preserved, so an import that merely refreshes the name,
    CMS status, or content owner cannot downgrade an established
    OFFICIAL_CMS_REVENUE / OFFICIAL_MANUAL_IMPORT classification.
    """
    if revenue_required == current_revenue_required:
        return current_status
    return "MISSING_REVENUE_SOURCE" if revenue_required else "PERFORMANCE_ONLY"


def normalize_optional_content_owner(value: str | None) -> str | None:
    """Normalize a content owner id, treating blank/None as unset (None).

    The content owner id is a free-form Google CMS string matched against the
    connector account id, not a UUID. The API layer rejects a present-but-blank
    value; this defensive normalization keeps direct registry callers from
    persisting a whitespace-only owner that could never match an account.
    """
    if value is None:
        return None
    return value.strip() or None
