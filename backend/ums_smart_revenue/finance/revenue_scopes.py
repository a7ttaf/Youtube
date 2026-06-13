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

    sector_ids: set[str] = set()
    company_ids: set[str] = set()

    if has_global:
        # A global grant authorizes the entire active universe. company_names
        # (from the active org-unit reader) is the authoritative active-company
        # set; the name maps carry exactly the tenant's active sectors/companies.
        sector_ids.update(sector_names.keys())
        company_ids.update(company_names.keys())
    else:
        for scope in granted:
            if scope.id is None:
                continue
            if scope.type == ScopeType.SECTOR:
                sector_ids.add(scope.id)
                # A sector grant contains its companies (mirror
                # OrgAccessIndex.contains via the reverse company_sector walk).
                company_ids.update(
                    company_id
                    for company_id, sector_id in org_index.company_sector.items()
                    if sector_id == scope.id
                )
            elif scope.type == ScopeType.COMPANY:
                # A company grant confers ONLY that company, never its sector.
                company_ids.add(scope.id)

    sectors = [
        RevenueScopeOption(
            scope_type="sector",
            scope_id=sector_id,
            label=sector_names.get(sector_id, sector_id),
        )
        for sector_id in sector_ids
    ]
    companies = [
        RevenueScopeOption(
            scope_type="company",
            scope_id=company_id,
            label=company_names.get(company_id, company_id),
        )
        for company_id in company_ids
    ]
    sectors.sort(key=lambda option: (option.label, option.scope_id or ""))
    companies.sort(key=lambda option: (option.label, option.scope_id or ""))

    options: list[RevenueScopeOption] = []
    if has_global:
        options.append(
            RevenueScopeOption(scope_type="global", scope_id=None, label="Global")
        )
    options.extend(sectors)
    options.extend(companies)
    return options
