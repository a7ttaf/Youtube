from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.scopes import OrgAccessIndex
from ums_smart_revenue.db.org_models import OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.tenancy.context import require_current_tenant


@dataclass(frozen=True)
class OrgUnitRow:
    id: str
    parent_id: str | None
    type: str
    name: str
    active: bool


@dataclass(frozen=True)
class ChannelRegistryRow:
    youtube_channel_id: str
    primary_org_unit_id: str | None
    active: bool


def build_org_access_index(
    *,
    org_units: list[OrgUnitRow],
    channels: list[ChannelRegistryRow],
) -> OrgAccessIndex:
    active_org_units = {unit.id: unit for unit in org_units if unit.active}
    company_sector: dict[str, str] = {}
    channel_company: dict[str, str] = {}
    channel_sector: dict[str, str] = {}

    for unit in active_org_units.values():
        if unit.type == "COMPANY" and unit.parent_id:
            parent = active_org_units.get(unit.parent_id)
            if parent and parent.type == "SECTOR":
                company_sector[unit.id] = parent.id

    for channel in channels:
        if not channel.active or not channel.primary_org_unit_id:
            continue
        primary_unit = active_org_units.get(channel.primary_org_unit_id)
        if not primary_unit:
            continue
        if primary_unit.type == "COMPANY":
            sector_id = company_sector.get(primary_unit.id)
            if sector_id:
                channel_company[channel.youtube_channel_id] = primary_unit.id
                channel_sector[channel.youtube_channel_id] = sector_id
        elif primary_unit.type == "SECTOR":
            channel_sector[channel.youtube_channel_id] = primary_unit.id

    return OrgAccessIndex(
        channel_company=channel_company,
        channel_sector=channel_sector,
        company_sector=company_sector,
    )


def load_org_access_index_from_session(session: Session) -> OrgAccessIndex:
    """Build the org-access index for the current request tenant."""
    tenant_id = require_current_tenant().id
    org_units = [
        OrgUnitRow(
            id=str(row.id),
            parent_id=str(row.parent_id) if row.parent_id is not None else None,
            type=row.type,
            name=row.name,
            active=row.active,
        )
        for row in session.execute(
            select(
                OrgUnitORM.id,
                OrgUnitORM.parent_id,
                OrgUnitORM.type,
                OrgUnitORM.name,
                OrgUnitORM.active,
            ).where(
                OrgUnitORM.tenant_id == tenant_id,
                OrgUnitORM.active.is_(True),
            )
        ).all()
    ]
    channels = [
        ChannelRegistryRow(
            youtube_channel_id=row.youtube_channel_id,
            primary_org_unit_id=str(row.primary_org_unit_id)
            if row.primary_org_unit_id is not None
            else None,
            active=row.active,
        )
        for row in session.execute(
            select(
                YouTubeChannelORM.youtube_channel_id,
                YouTubeChannelORM.primary_org_unit_id,
                YouTubeChannelORM.active,
            ).where(
                YouTubeChannelORM.tenant_id == tenant_id,
                YouTubeChannelORM.active.is_(True),
                YouTubeChannelORM.primary_org_unit_id.is_not(None),
            )
        ).all()
    ]
    return build_org_access_index(org_units=org_units, channels=channels)
