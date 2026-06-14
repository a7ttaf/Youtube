"""Authorized rollup-scope option builder for GET /revenue/scopes.

Produces the Command Center scope selector's options from a viewer's granted
VIEW_REVENUE scopes. This is a security boundary: the returned options are the
ONLY org scopes the viewer may roll revenue up to, so the selector can never
offer an out-of-scope sector/company (an org-structure leak) nor a dead option
that 403s on selection. Pure logic — no DB, no FastAPI, no auth side effects.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex, ScopeType


@dataclass(frozen=True)
class RevenueScopeOption:
    """One selectable rollup scope (global / a sector / a company)."""

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
#   rollup options (global / sector / company) they are authorized to aggregate,
#   deduplicated, named, and deterministically ordered.
# Database/ORM: None — operates on an already-built OrgAccessIndex and name maps
#   sourced by the caller from the org-unit reader (PostgreSQL source of truth).
# Standards: Pure function; typed RevenueScopeOption boundary; no logging/auth
#   side effects (the route enforces the fail-closed VIEW_REVENUE gate).
# Blast Radius: Authorization-adjacent — this is the anti-scope-leak surface.
#   A global grant lists the full active universe; a scoped grant lists ONLY the
#   granted scopes (sector expands to its member companies via the reverse
#   company_sector walk that mirrors OrgAccessIndex.contains; a company grant
#   confers ONLY itself, never its sector). Unsupported granted types are
#   dropped so a malformed grant cannot widen the surface. No finance numbers,
#   no audit, no Neo4j, no exports.
# Connections:
#   - File: backend/ums_smart_revenue/auth/scopes.py -> OrgAccessIndex.contains
#       (sector-contains-company semantics this expansion must match).
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


def _build_options(
    has_global: bool,
    sector_ids: set[str],
    company_ids: set[str],
    sector_names: Mapping[str, str],
    company_names: Mapping[str, str],
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
    return options


def build_authorized_revenue_scopes(
    *,
    granted: tuple[AccessScope, ...],
    org_index: OrgAccessIndex,
    sector_names: Mapping[str, str],
    company_names: Mapping[str, str],
) -> list[RevenueScopeOption]:
    """Return the viewer's authorized rollup options, deduped and ordered.

    Args:
        granted: Active VIEW_REVENUE AccessScope grants for the principal.
        org_index: Tenant org-access index (company_sector reverse-walked here).
        sector_names: sector_id -> display name (raw-id fallback when missing).
        company_names: company_id -> display name (raw-id fallback when missing).

    Returns:
        Options ordered global-first, then sectors by name, then companies by
        name. The global option is present ONLY when a global grant exists.
    """
    has_global = any(scope.type == ScopeType.GLOBAL for scope in granted)
    if has_global:
        sector_ids, company_ids = _expand_global_grants(sector_names, company_names, org_index)
    else:
        sector_ids, company_ids = _expand_scoped_grants(
            granted, org_index, sector_names, company_names
        )
    return _build_options(has_global, sector_ids, company_ids, sector_names, company_names)
