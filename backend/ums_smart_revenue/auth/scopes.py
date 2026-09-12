# ============================================================================
# Purpose: Authorization scope vocabulary — ScopeType/AccessScope identify
#   what a role assignment grants, and OrgAccessIndex answers whether one
#   scope contains another through the org hierarchy (or by resolution-aware
#   same-type equality for targeted indexes).
# Database/ORM: None directly — the maps here are populated by loaders in
#   org/access_index.py from org_units + youtube_channels.
# Standards: fail-closed — global grants contain everything, a global target
#   is never contained by a scoped grant, malformed id-less scopes deny, and
#   resolution-aware indexes deny same-type stale scopes on dead org targets.
# Blast Radius: Authorization — every scoped permission check funnels
#   through OrgAccessIndex.contains; a wrong True is a privilege grant.
# Connections:
#   - File: backend/ums_smart_revenue/org/access_index.py -> index loaders.
#   - File: backend/ums_smart_revenue/api/users.py -> mutation authorization.
# ============================================================================
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ScopeType(StrEnum):
    """Authorization scope kinds carried by role assignments and checks."""

    GLOBAL = "global"
    SECTOR = "sector"
    COMPANY = "company"
    CHANNEL = "channel"
    GROUP = "group"
    FINANCE_MONTH = "finance-month"
    EXPORT = "export"
    CONNECTOR = "connector"


@dataclass(frozen=True)
class AccessScope:
    """One typed authorization scope: a ScopeType plus an optional target id."""

    type: ScopeType
    id: str | None = None

    @classmethod
    def global_scope(cls) -> AccessScope:
        """Return the tenant-wide scope that contains every other scope."""
        return cls(ScopeType.GLOBAL)

    @classmethod
    def sector(cls, sector_id: str) -> AccessScope:
        """Return a scope anchored on one sector org unit."""
        return cls(ScopeType.SECTOR, sector_id)

    @classmethod
    def company(cls, company_id: str) -> AccessScope:
        """Return a scope anchored on one company org unit."""
        return cls(ScopeType.COMPANY, company_id)

    @classmethod
    def channel(cls, channel_id: str) -> AccessScope:
        """Return a scope anchored on one YouTube channel."""
        return cls(ScopeType.CHANNEL, channel_id)

    @classmethod
    def group(cls, group_id: str) -> AccessScope:
        """Return a scope anchored on one channel group."""
        return cls(ScopeType.GROUP, group_id)

    @classmethod
    def finance_month(cls, month: str) -> AccessScope:
        """Return a scope anchored on one finance month (YYYY-MM)."""
        return cls(ScopeType.FINANCE_MONTH, month)

    @classmethod
    def export(cls, export_id: str | None = None) -> AccessScope:
        """Return a scope anchored on one export artifact."""
        return cls(ScopeType.EXPORT, export_id)

    @classmethod
    def connector(cls, connector_id: str | None = None) -> AccessScope:
        """Return a scope anchored on one connector credential/account."""
        return cls(ScopeType.CONNECTOR, connector_id)


# Scope types whose targets resolve to a live org object (youtube_channels or
# org_units). Only these are gated by OrgAccessIndex.resolved_targets — GROUP
# and the non-org types have no org-unit resolution to consult.
_ORG_RESOLVABLE_TYPES = frozenset({ScopeType.SECTOR, ScopeType.COMPANY, ScopeType.CHANNEL})


@dataclass(frozen=True)
class OrgAccessIndex:
    """Org-hierarchy containment edges plus optional target-resolution proof.

    The three maps carry the canonical sector -> company -> channel ancestry
    edges; ``resolved_targets`` (when set) records which org scopes the loader
    proved exist, are active, and belong to the request tenant.
    """

    channel_company: dict[str, str] = field(default_factory=dict)
    channel_sector: dict[str, str] = field(default_factory=dict)
    company_sector: dict[str, str] = field(default_factory=dict)
    # When not None, same-type containment for an org-resolvable target also
    # requires the target in this set — i.e. the loader proved the channel /
    # company / sector exists, is active, and belongs to the request tenant.
    # None = untracked (the canonical full index; equality alone decides).
    resolved_targets: frozenset[tuple[ScopeType, str]] | None = None

    def contains(self, granted_scope: AccessScope, target_scope: AccessScope) -> bool:
        """Return True when the granted scope contains the target scope."""
        if granted_scope.type == ScopeType.GLOBAL:
            return True
        if target_scope.type == ScopeType.GLOBAL:
            return False
        if granted_scope.type == target_scope.type:
            return self._contains_same_type(granted_scope, target_scope)
        return self._contains_cross_type(granted_scope, target_scope)

    # ========================================================================
    # Purpose: Decide same-type containment — id equality, but gated on the
    #   index's resolved_targets when the index tracks resolution.
    # Database/ORM: None — pure in-memory check over the loaded index.
    # Standards: fail-closed — unresolved org targets deny even an exact-id
    #   stale scope; id-less pairs only match when both sides are id-less.
    # Blast Radius: Authorization — a wrong True grants a scoped admin
    #   authority over a dead or malformed target.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/access_index.py -> populates
    #     resolved_targets in the targeted loader.
    # ========================================================================
    # ========================================================================
    # Purpose: Answer whether the index's resolution proof covers the target —
    #   False only when the index tracks resolution AND the target is an
    #   org-resolvable type the loader could not prove live.
    # Database/ORM: None — pure in-memory check over resolved_targets.
    # Standards: fail-open only where tracking is absent (the canonical full
    #   index) or the target type has no org resolution to consult; every
    #   tracked org target must appear in resolved_targets.
    # Blast Radius: Authorization + data integrity — assignment routes use
    #   this to refuse dangling scopes even under global authority.
    # Connections:
    #   - File: backend/ums_smart_revenue/api/users.py -> assignment gate.
    # ========================================================================
    def target_resolved(self, target_scope: AccessScope) -> bool:
        """Return True unless a tracked org target failed to resolve."""
        if self.resolved_targets is None:
            return True
        if target_scope.type not in _ORG_RESOLVABLE_TYPES:
            return True
        return (target_scope.type, target_scope.id) in self.resolved_targets

    def _contains_same_type(
        self, granted_scope: AccessScope, target_scope: AccessScope
    ) -> bool:
        """Decide same-type containment with resolution-aware id equality."""
        if granted_scope.id is None or target_scope.id is None:
            return granted_scope.id is None and target_scope.id is None
        # Fail closed for unresolved org targets: without this gate a
        # deleted/inactive channel or company still authorizes a caller
        # holding that exact stale scope, because id equality alone never
        # consults the maps. Global-scoped authority is unaffected — it
        # already returned True above. Non-org target types are not
        # resolvable here and keep equality semantics.
        if not self.target_resolved(target_scope):
            return False
        return granted_scope.id == target_scope.id

    # ========================================================================
    # Purpose: Decide cross-type containment — sector>company, sector>channel,
    #   company>channel — through the loaded ancestry edge maps.
    # Database/ORM: None — pure in-memory map lookups.
    # Standards: fail-closed — either side missing an id denies (a None id
    #   would compare equal to a missing lookup and falsely authorize), and
    #   any scope pair without a real ancestry edge denies.
    # Blast Radius: Authorization — a wrong True lets a sector/company admin
    #   reach a target outside their subtree.
    # Connections:
    #   - File: backend/ums_smart_revenue/org/access_index.py -> builds the
    #     edge maps this helper reads.
    # ========================================================================
    def _contains_cross_type(
        self, granted_scope: AccessScope, target_scope: AccessScope
    ) -> bool:
        """Decide cross-type containment through the ancestry edge maps."""
        # Cross-type containment requires real ids on both sides; a malformed
        # grant or target with id=None would otherwise compare equal to a
        # missing mapping lookup (also None) and falsely authorize unrelated
        # targets. Fail closed on either side.
        if granted_scope.id is None or target_scope.id is None:
            return False
        if granted_scope.type == ScopeType.SECTOR and target_scope.type == ScopeType.COMPANY:
            return self.company_sector.get(target_scope.id) == granted_scope.id
        if granted_scope.type == ScopeType.SECTOR and target_scope.type == ScopeType.CHANNEL:
            return self.channel_sector.get(target_scope.id) == granted_scope.id
        if granted_scope.type == ScopeType.COMPANY and target_scope.type == ScopeType.CHANNEL:
            return self.channel_company.get(target_scope.id) == granted_scope.id
        return False
