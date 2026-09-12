# ============================================================================
# Purpose: Build OrgAccessIndex — the company->sector and
#   channel->company/sector containment maps every scoped authorization
#   check resolves through. Two loaders: the full tenant index for read-heavy
#   list paths, and a targeted per-target index for the user-management
#   mutation routes.
# Database/ORM: org_units + youtube_channels, read-only, always
#   tenant-scoped and active-only.
# Standards: fail-closed — missing/inactive/orphan edges are omitted, never
#   granted; the targeted loader mirrors build_org_access_index edge rules
#   exactly (a channel->company edge exists only with a live sector parent).
# Blast Radius: Authorization — these maps decide whether scoped callers
#   (sector/company admins) may act on companies, channels, and grants.
# Connections:
#   - File: backend/ums_smart_revenue/auth/scopes.py -> OrgAccessIndex.contains.
#   - File: backend/ums_smart_revenue/api/dependencies_finance.py -> full index.
#   - File: backend/ums_smart_revenue/api/users.py -> targeted loader callers.
# ============================================================================
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Row
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex, ScopeType
from ums_smart_revenue.db.org_models import OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.tenancy.context import require_current_tenant


@dataclass(frozen=True)
class OrgUnitRow:
    """Typed read row for one org_units entry used to build the index."""

    id: str
    parent_id: str | None
    type: str
    name: str
    active: bool


@dataclass(frozen=True)
class ChannelRegistryRow:
    """Typed read row for one youtube_channels entry used to build the index."""

    youtube_channel_id: str
    primary_org_unit_id: str | None
    active: bool


def build_org_access_index(
    *,
    org_units: list[OrgUnitRow],
    channels: list[ChannelRegistryRow],
) -> OrgAccessIndex:
    """Derive company->sector and channel->company/sector containment maps."""
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


# ============================================================================
# Purpose: Single primary-key read of one active org unit's id/parent/type.
# Database/ORM: org_units — one tenant-scoped, active-only SELECT.
# Standards: read-only; returns None for missing/inactive rows.
# Blast Radius: None detected — read helper for the scoped index builder.
# Connections:
#   - File: backend/ums_smart_revenue/org/access_index.py -> loader above.
# ============================================================================
def _active_org_unit_row(
    session: Session, tenant_id: UUID, unit_id: UUID
) -> Row[tuple[UUID, UUID | None, str]] | None:
    """Return (id, parent_id, type) for one active org unit, or None."""
    return session.execute(
        select(OrgUnitORM.id, OrgUnitORM.parent_id, OrgUnitORM.type).where(
            OrgUnitORM.tenant_id == tenant_id,
            OrgUnitORM.id == unit_id,
            OrgUnitORM.active.is_(True),
        )
    ).one_or_none()


# ============================================================================
# Purpose: Resolve a unit's parent id only when the parent is an active SECTOR
#   — mirrors build_org_access_index's company->sector edge rule.
# Database/ORM: org_units — one tenant-scoped, active-only type SELECT.
# Standards: read-only; inactive or non-sector parents yield None.
# Blast Radius: None detected — read helper for the scoped index builder.
# Connections:
#   - File: backend/ums_smart_revenue/org/access_index.py -> loader above.
# ============================================================================
def _parent_sector_id(
    session: Session,
    tenant_id: UUID,
    unit_row: Row[tuple[UUID, UUID | None, str]],
) -> str | None:
    """Return the unit's parent id when it is an active SECTOR, else None."""
    parent_id = unit_row[1]
    if parent_id is None:
        return None
    parent_type = session.execute(
        select(OrgUnitORM.type).where(
            OrgUnitORM.tenant_id == tenant_id,
            OrgUnitORM.id == parent_id,
            OrgUnitORM.active.is_(True),
        )
    ).scalar_one_or_none()
    return str(parent_id) if parent_type == "SECTOR" else None


# ============================================================================
# Purpose: Build the MINIMAL org-access index needed to evaluate
#   OrgAccessIndex.contains for one target scope — the user-management
#   mutations only ever consult the maps by the target's own id, so loading
#   every org unit and channel in the tenant is wasted work.
# Database/ORM: org_units + youtube_channels — at most three primary/indexed
#   lookups (channel -> primary unit -> parent sector) scoped to the request
#   tenant.
# Standards: fail-closed — a target that fails to parse or resolve yields an
#   empty index with resolved_targets EMPTY, so contains() returns False for
#   every scoped caller INCLUDING a same-type stale scope (the id-equality
#   shortcut is gated on resolution); global-scoped authority still passes
#   (its check needs no index).
# Blast Radius: Authorization scope containment for the four user-management
#   mutation routes; containment results are identical to the full index for
#   the queried target.
# Connections:
#   - File: backend/ums_smart_revenue/auth/scopes.py -> contains() lookups.
#   - File: backend/ums_smart_revenue/api/users.py -> mutation routes.
# ============================================================================
def _unresolved_index() -> OrgAccessIndex:
    """Return the fail-closed index for a target that did not resolve.

    ``resolved_targets=frozenset()`` turns ON same-type resolution checks:
    an org target that cannot be resolved is denied to same-type callers
    (contains() still answers True for global-scoped authority, so stale
    assignments stay reachable for cleanup by global admins only).
    """
    return OrgAccessIndex(
        channel_company={},
        channel_sector={},
        company_sector={},
        resolved_targets=frozenset(),
    )


def _sector_target_index(
    session: Session, tenant_id: UUID, target_scope: AccessScope
) -> OrgAccessIndex:
    """Build the index for a sector target: resolved iff the unit is a live SECTOR."""
    sector_id_raw = target_scope.id
    if sector_id_raw is None:
        return _unresolved_index()
    try:
        sector_unit_id = UUID(sector_id_raw)
    except (TypeError, ValueError):
        return _unresolved_index()
    unit = _active_org_unit_row(session, tenant_id, sector_unit_id)
    if unit is None or unit[2] != "SECTOR":
        return _unresolved_index()
    return OrgAccessIndex(
        channel_company={},
        channel_sector={},
        company_sector={},
        resolved_targets=frozenset({(target_scope.type, sector_id_raw)}),
    )


def _channel_target_index(
    session: Session, tenant_id: UUID, target_scope: AccessScope
) -> OrgAccessIndex:
    """Build the index for a channel target from its primary-unit ancestry."""
    channel_id = target_scope.id
    if channel_id is None:
        return _unresolved_index()
    resolved = frozenset({(target_scope.type, channel_id)})
    primary_unit_id = session.execute(
        select(YouTubeChannelORM.primary_org_unit_id).where(
            YouTubeChannelORM.tenant_id == tenant_id,
            YouTubeChannelORM.youtube_channel_id == channel_id,
            YouTubeChannelORM.active.is_(True),
            YouTubeChannelORM.primary_org_unit_id.is_not(None),
        )
    ).scalar_one_or_none()
    if primary_unit_id is None:
        return _unresolved_index()
    unit = _active_org_unit_row(session, tenant_id, primary_unit_id)
    if unit is None:
        return _unresolved_index()
    if unit[2] == "SECTOR":
        return OrgAccessIndex(
            channel_company={},
            channel_sector={channel_id: str(unit[0])},
            company_sector={},
            resolved_targets=resolved,
        )
    if unit[2] != "COMPANY":
        return _unresolved_index()
    # Match build_org_access_index: the channel->company edge exists ONLY
    # when the company has an active sector parent. A channel owned by an
    # orphan company gets no company edge, so company-scoped admins cannot
    # grant or revoke against it — sector/global authority still applies.
    # The channel itself is a live anchored target, so it stays resolved
    # (a same-type channel scope may still act on it).
    sector_id = _parent_sector_id(session, tenant_id, unit)
    if sector_id is None:
        return OrgAccessIndex(
            channel_company={},
            channel_sector={},
            company_sector={},
            resolved_targets=resolved,
        )
    return OrgAccessIndex(
        channel_company={channel_id: str(unit[0])},
        channel_sector={channel_id: sector_id},
        company_sector={},
        resolved_targets=resolved,
    )


def _company_target_index(
    session: Session, tenant_id: UUID, target_scope: AccessScope
) -> OrgAccessIndex:
    """Build the index for a company target: resolved iff the unit is a live COMPANY."""
    company_id_raw = target_scope.id
    if company_id_raw is None:
        return _unresolved_index()
    try:
        company_id = UUID(company_id_raw)
    except (TypeError, ValueError):
        return _unresolved_index()
    unit = _active_org_unit_row(session, tenant_id, company_id)
    if unit is None or unit[2] != "COMPANY":
        return _unresolved_index()
    sector_id = _parent_sector_id(session, tenant_id, unit)
    return OrgAccessIndex(
        channel_company={},
        channel_sector={},
        company_sector=(
            {company_id_raw: sector_id} if sector_id is not None else {}
        ),
        resolved_targets=frozenset({(target_scope.type, company_id_raw)}),
    )


def load_org_access_index_for_scope(
    session: Session, target_scope: AccessScope
) -> OrgAccessIndex:
    """Build the minimal index covering only the target scope's ancestry.

    Raises:
        TenantContextMissing: when called without an active request tenant
            context — every lookup below is scoped to ``require_current_tenant``.
    """
    tenant_id = require_current_tenant().id
    if target_scope.id is None:
        return _unresolved_index()
    if target_scope.type == ScopeType.SECTOR:
        return _sector_target_index(session, tenant_id, target_scope)
    if target_scope.type == ScopeType.CHANNEL:
        return _channel_target_index(session, tenant_id, target_scope)
    if target_scope.type == ScopeType.COMPANY:
        return _company_target_index(session, tenant_id, target_scope)
    return _unresolved_index()
