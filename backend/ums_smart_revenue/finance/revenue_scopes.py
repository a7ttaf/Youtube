"""Authorized rollup-scope option builder for GET /revenue/scopes.

Produces the Command Center scope selector's options from a viewer's granted
VIEW_REVENUE scopes. This is a security boundary: the returned options are the
ONLY org scopes the viewer may roll revenue up to, so the selector can never
offer an out-of-scope sector/company/group (an org-structure leak) nor a dead
option that 403s on selection. Pure logic — no DB, no FastAPI, no auth side
effects.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex, ScopeType
from ums_smart_revenue.org.channel_groups import ChannelGroupEntry, ChannelGroupRegistryStore


@dataclass(frozen=True)
class RevenueScopeOption:
    """One selectable rollup scope (global / a sector / a company / a group)."""

    scope_type: str
    scope_id: str | None
    label: str

    def to_api(self) -> dict[str, object]:
        """Serialize to the fixed GET /revenue/scopes item shape."""
        return {
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "label": self.label,
        }


# ============================================================================
# Purpose: Expand a viewer's granted VIEW_REVENUE scopes into the exact set of
#   rollup options (global / sector / company / channel group) they are
#   authorized to aggregate, deduplicated, named, and deterministically ordered.
# Database/ORM: None — operates on an already-built OrgAccessIndex and name maps
#   sourced by the caller from the org-unit reader, plus group entries sourced
#   by the caller from the PostgreSQL-backed channel-group registry.
# Standards: Pure function; typed RevenueScopeOption boundary; no logging/auth
#   side effects (the route enforces the fail-closed VIEW_REVENUE gate).
# Blast Radius: Authorization-adjacent — this is the anti-scope-leak surface.
#   A global grant lists the full active universe; a scoped grant lists ONLY the
#   granted scopes (sector expands to its member companies via the reverse
#   company_sector walk that mirrors OrgAccessIndex.contains; a company grant
#   confers ONLY itself, never its sector). A group option appears only when
#   every member channel is covered by the viewer's granted scopes; unsupported
#   granted types are dropped so a malformed grant cannot widen the surface. No
#   finance numbers, no audit, no Neo4j, no exports.
# Connections:
#   - File: backend/ums_smart_revenue/auth/scopes.py -> OrgAccessIndex.contains
#       (sector-contains-company semantics this expansion must match).
#   - File: backend/ums_smart_revenue/org/channel_groups.py -> ChannelGroupEntry
#       (group member channel IDs used for complete-membership authorization).
#   - File: backend/ums_smart_revenue/api/revenue.py -> GET /revenue/scopes
#       caller (supplies granted scopes + org_index + name maps).
# ============================================================================


def _expand_global_grants(
    sector_names: Mapping[str, str],
    company_names: Mapping[str, str],
    org_index: OrgAccessIndex,
) -> tuple[set[str], set[str]]:
    """A global grant authorizes the entire active universe."""
    sector_ids = set(sector_names.keys())
    company_ids = {cid for cid in company_names if cid in org_index.company_sector}
    return sector_ids, company_ids


def _collect_sector_scope(
    scope_id: str,
    org_index: OrgAccessIndex,
) -> tuple[set[str], set[str]]:
    """Expand one sector grant into its sector id + member company ids."""
    company_ids = {cid for cid, sid in org_index.company_sector.items() if sid == scope_id}
    return {scope_id}, company_ids


def _expand_scoped_grants(
    granted: tuple[AccessScope, ...],
    org_index: OrgAccessIndex,
    sector_names: Mapping[str, str],
    company_names: Mapping[str, str],
) -> tuple[set[str], set[str]]:
    """Expand non-global grants into sector/company id sets."""
    sector_ids: set[str] = set()
    company_ids: set[str] = set()
    for scope in granted:
        if scope.id is None:
            continue
        # FIX (review #102): skip stale grants for deactivated sectors/companies
        # so the selector never offers a dead option that 403s on read.
        if scope.type == ScopeType.SECTOR:
            if scope.id not in sector_names:
                continue
            sector_ids.add(scope.id)
            company_ids.update(
                company_id
                for company_id, sector_id in org_index.company_sector.items()
                if sector_id == scope.id
            )
        elif scope.type == ScopeType.COMPANY:
            if scope.id not in company_names:
                continue
            company_ids.add(scope.id)
    return sector_ids, company_ids


def _scope_sort_key(option: RevenueScopeOption) -> tuple[str, str]:
    return (option.label, option.scope_id or "")


# ============================================================================
# Purpose: Decide whether a channel group may be offered as a revenue rollup
#   option by requiring every member channel to be covered by the viewer's
#   existing org grants.
# Database/ORM: None; ChannelGroupEntry rows are supplied by the caller from
#   the PostgreSQL-backed registry.
# Standards: Pure authorization-adjacent helper; no grant persistence, no
#   logging, no side effects, and fail-closed for inactive or empty groups.
# Blast Radius: Revenue selector authorization surface; no finance totals,
#   audit writes, exports, or database mutations.
# Connections:
#   - File: backend/ums_smart_revenue/auth/scopes.py -> OrgAccessIndex.contains
#       supplies global/sector/company/channel containment semantics.
#   - File: backend/ums_smart_revenue/org/channel_groups.py -> ChannelGroupEntry
#       provides active flag, label, and member channel IDs.
# ============================================================================
def _grant_contains_channel(
    scope: AccessScope,
    org_index: OrgAccessIndex,
    channel_id: str,
) -> bool:
    """Return whether a granted scope covers one channel."""
    return org_index.contains(scope, AccessScope.channel(channel_id))


def _covered_channel_ids(
    granted: tuple[AccessScope, ...],
    org_index: OrgAccessIndex,
) -> set[str] | None:
    """Return the set of channels covered by the granted scopes, or None for global.

    None signals that the caller has a global grant and therefore every channel
    is covered; callers must treat None as a superset of any concrete channel set.
    Computing this once lets the group authorization checks run as a single set
    containment test per group instead of nested per-channel/per-grant scans.
    """
    covered: set[str] = set()
    has_global = False
    for scope in granted:
        if scope.type == ScopeType.GLOBAL:
            has_global = True
            continue
        if scope.type == ScopeType.CHANNEL and scope.id is not None:
            covered.add(scope.id)
        elif scope.type == ScopeType.COMPANY and scope.id is not None:
            for channel_id, company_id in org_index.channel_company.items():
                if company_id == scope.id:
                    covered.add(channel_id)
        elif scope.type == ScopeType.SECTOR and scope.id is not None:
            for channel_id, sector_id in org_index.channel_sector.items():
                if sector_id == scope.id:
                    covered.add(channel_id)
    if has_global:
        return None
    return covered


def _build_group_options(
    *,
    groups: tuple[ChannelGroupEntry, ...],
    granted: tuple[AccessScope, ...],
    org_index: OrgAccessIndex,
) -> list[RevenueScopeOption]:
    """Assemble authorized group scope options from complete member coverage."""
    covered = _covered_channel_ids(granted, org_index)
    options: list[RevenueScopeOption] = []
    for group in groups:
        if not group.active or not group.channel_ids:
            continue
        if covered is not None and not set(group.channel_ids).issubset(covered):
            continue
        options.append(RevenueScopeOption(scope_type="group", scope_id=group.id, label=group.name))
    options.sort(key=_scope_sort_key)
    return options


def _build_options(
    has_global: bool,
    sector_ids: set[str],
    company_ids: set[str],
    sector_names: Mapping[str, str],
    company_names: Mapping[str, str],
    group_options: list[RevenueScopeOption],
) -> list[RevenueScopeOption]:
    """Assemble the ordered, labeled option list from the resolved id sets."""
    sectors = [
        RevenueScopeOption(
            scope_type="sector",
            scope_id=sid,
            label=sector_names.get(sid, sid),
        )
        for sid in sector_ids
    ]
    companies = [
        RevenueScopeOption(
            scope_type="company",
            scope_id=cid,
            label=company_names.get(cid, cid),
        )
        for cid in company_ids
    ]
    sectors.sort(key=_scope_sort_key)
    companies.sort(key=_scope_sort_key)
    options: list[RevenueScopeOption] = []
    if has_global:
        options.append(RevenueScopeOption(scope_type="global", scope_id=None, label="Global"))
    options.extend(sectors)
    options.extend(companies)
    options.extend(group_options)
    return options


def build_authorized_revenue_scopes(
    *,
    granted: tuple[AccessScope, ...],
    org_index: OrgAccessIndex,
    sector_names: Mapping[str, str],
    company_names: Mapping[str, str],
    groups: tuple[ChannelGroupEntry, ...] = (),
) -> list[RevenueScopeOption]:
    """Return the viewer's authorized rollup options, deduped and ordered.

    Args:
        granted: Active VIEW_REVENUE AccessScope grants for the principal.
        org_index: Tenant org-access index (company_sector reverse-walked here).
        sector_names: sector_id -> display name (raw-id fallback when missing).
        company_names: company_id -> display name (raw-id fallback when missing).
        groups: active channel-group entries to include when every member
            channel is covered by the viewer's grants.

    Returns:
        Options ordered global-first, then sectors by name, companies by name,
        and groups by name. The global option is present ONLY when a global
        grant exists.
    """
    has_global = any(scope.type == ScopeType.GLOBAL for scope in granted)
    if has_global:
        sector_ids, company_ids = _expand_global_grants(sector_names, company_names, org_index)
    else:
        sector_ids, company_ids = _expand_scoped_grants(
            granted, org_index, sector_names, company_names
        )
    group_options = _build_group_options(groups=groups, granted=granted, org_index=org_index)
    return _build_options(
        has_global,
        sector_ids,
        company_ids,
        sector_names,
        company_names,
        group_options,
    )


# ============================================================================
# Purpose: Resolve a revenue-read scope string into an AccessScope and an
#   optional channel-id filter, raising HTTPException-shaped errors that the
#   route translates 1:1. The group branch delegates to the registry so the
#   route never touches ChannelGroupRegistryStore directly.
# Database/ORM: ChannelGroupRegistryStore is the only data-access dependency
#   (supplied by the caller); org hierarchy containment comes from the
#   caller-built OrgAccessIndex. No writes.
# Standards: Pure service boundary — no FastAPI imports, no logging side
#   effects, no widened grant storage. Error categories are deliberately
#   limited to "unknown scope_type" (422) and the per-route gate set
#   (permission-shaped errors raised by the route after this returns).
# Blast Radius: Finance read scope resolution and downstream channel filter
#   for net revenue, rankings, and recalculation preview reads.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py ->
#       _revenue_read_scope_to_channel_ids is now a thin shim that calls this
#       helper and translates the service's typed errors.
#   - File: backend/ums_smart_revenue/org/channel_groups.py -> ChannelGroupEntry
#       supplies the active flag, label, and member channel IDs.
# ============================================================================
class RevenueReadScopeResolutionError(ValueError):
    """Base error for revenue-read scope resolution failures."""


class RevenueReadScopeRequestShapeError(RevenueReadScopeResolutionError):
    """The request shape is invalid (missing/extra scope_id, unknown type).

    Translated to HTTP 422 by the route shim so the caller can distinguish
    malformed requests from unauthorized reads.
    """


class UnknownRevenueScopeTypeError(RevenueReadScopeRequestShapeError):
    """The requested scope_type is not one of the supported revenue scopes."""

    def __init__(self, scope_type: str) -> None:
        super().__init__(f"Unknown revenue scope_type: {scope_type}")
        self.scope_type = scope_type


def resolve_revenue_read_scope(
    *,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
) -> tuple[AccessScope, set[str] | None]:
    """Translate a revenue read scope into an AccessScope and channel filter.

    Raises RevenueReadScopeRequestShapeError for malformed requests (missing
    or extra scope_id) and UnknownRevenueScopeTypeError for unsupported
    scope types. Invalid group lookups (missing, inactive, or empty)
    raise RevenueReadScopeResolutionError, which the caller normalizes
    to HTTP 403 so unauthorized probes and unauthorized reads return
    the same response.
    """
    normalized_scope_type = scope_type.strip()
    normalized_scope_id = scope_id.strip() if isinstance(scope_id, str) else scope_id
    if normalized_scope_type == "global":
        if normalized_scope_id:
            raise RevenueReadScopeRequestShapeError(
                "scope_id must be omitted for global revenue reads"
            )
        return AccessScope.global_scope(), None
    if not normalized_scope_id:
        raise RevenueReadScopeRequestShapeError(
            f"scope_id is required for revenue scope_type: {normalized_scope_type}"
        )
    if normalized_scope_type == "sector":
        return (
            AccessScope.sector(normalized_scope_id),
            {
                channel_id
                for channel_id, sector_id in org_index.channel_sector.items()
                if sector_id == normalized_scope_id
            },
        )
    if normalized_scope_type == "company":
        return (
            AccessScope.company(normalized_scope_id),
            {
                channel_id
                for channel_id, company_id in org_index.channel_company.items()
                if company_id == normalized_scope_id
            },
        )
    if normalized_scope_type == "channel":
        return AccessScope.channel(normalized_scope_id), {normalized_scope_id}
    if normalized_scope_type == "group":
        # FIX (Gitar + chatgpt-codex-connector reviews #122): Use the
        # active-only member set for revenue reads while get_group still
        # returns the full member list for management authz. The full
        # group is fetched first so a missing or inactive group yields
        # the same 403 as a group whose active member set is empty.
        group = group_registry.get_group(normalized_scope_id)
        if group is None or not group.active:
            raise RevenueReadScopeResolutionError(
                "group revenue reads require an active, non-empty group"
            )
        active_channel_ids = group_registry.get_active_member_channels(normalized_scope_id) or ()
        if not active_channel_ids:
            raise RevenueReadScopeResolutionError(
                "group revenue reads require an active, non-empty group"
            )
        return AccessScope.group(normalized_scope_id), set(active_channel_ids)
    raise UnknownRevenueScopeTypeError(normalized_scope_type)
