from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ScopeType(StrEnum):
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
    type: ScopeType
    id: str | None = None

    @classmethod
    def global_scope(cls) -> AccessScope:
        return cls(ScopeType.GLOBAL)

    @classmethod
    def sector(cls, sector_id: str) -> AccessScope:
        return cls(ScopeType.SECTOR, sector_id)

    @classmethod
    def company(cls, company_id: str) -> AccessScope:
        return cls(ScopeType.COMPANY, company_id)

    @classmethod
    def channel(cls, channel_id: str) -> AccessScope:
        return cls(ScopeType.CHANNEL, channel_id)

    @classmethod
    def group(cls, group_id: str) -> AccessScope:
        return cls(ScopeType.GROUP, group_id)

    @classmethod
    def finance_month(cls, month: str) -> AccessScope:
        return cls(ScopeType.FINANCE_MONTH, month)

    @classmethod
    def export(cls, export_id: str | None = None) -> AccessScope:
        return cls(ScopeType.EXPORT, export_id)

    @classmethod
    def connector(cls, connector_id: str | None = None) -> AccessScope:
        return cls(ScopeType.CONNECTOR, connector_id)


# Scope types whose targets resolve to a live org object (youtube_channels or
# org_units). Only these are gated by OrgAccessIndex.resolved_targets — GROUP
# and the non-org types have no org-unit resolution to consult.
_ORG_RESOLVABLE_TYPES = frozenset({ScopeType.SECTOR, ScopeType.COMPANY, ScopeType.CHANNEL})


@dataclass(frozen=True)
class OrgAccessIndex:
    channel_company: dict[str, str] = field(default_factory=dict)
    channel_sector: dict[str, str] = field(default_factory=dict)
    company_sector: dict[str, str] = field(default_factory=dict)
    # When not None, same-type containment for an org-resolvable target also
    # requires the target in this set — i.e. the loader proved the channel /
    # company / sector exists, is active, and belongs to the request tenant.
    # None = untracked (the canonical full index; equality alone decides).
    resolved_targets: frozenset[tuple[ScopeType, str]] | None = None

    def contains(self, granted_scope: AccessScope, target_scope: AccessScope) -> bool:
        if granted_scope.type == ScopeType.GLOBAL:
            return True
        if target_scope.type == ScopeType.GLOBAL:
            return False
        if granted_scope.type == target_scope.type:
            if granted_scope.id is None or target_scope.id is None:
                return granted_scope.id is None and target_scope.id is None
            # Fail closed for unresolved org targets: without this gate a
            # deleted/inactive channel or company still authorizes a caller
            # holding that exact stale scope, because id equality alone never
            # consults the maps. Global-scoped authority is unaffected — it
            # already returned True above. Non-org target types are not
            # resolvable here and keep equality semantics.
            if (
                self.resolved_targets is not None
                and target_scope.type in _ORG_RESOLVABLE_TYPES
                and (target_scope.type, target_scope.id) not in self.resolved_targets
            ):
                return False
            return granted_scope.id == target_scope.id
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
