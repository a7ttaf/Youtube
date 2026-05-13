from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class ScopeType(StrEnum):
    GLOBAL = "global"
    SECTOR = "sector"
    COMPANY = "company"
    CHANNEL = "channel"
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
    def finance_month(cls, month: str) -> AccessScope:
        return cls(ScopeType.FINANCE_MONTH, month)

    @classmethod
    def export(cls, export_id: str | None = None) -> AccessScope:
        return cls(ScopeType.EXPORT, export_id)

    @classmethod
    def connector(cls, connector_id: str | None = None) -> AccessScope:
        return cls(ScopeType.CONNECTOR, connector_id)


@dataclass(frozen=True)
class OrgAccessIndex:
    channel_company: dict[str, str] = field(default_factory=dict)
    channel_sector: dict[str, str] = field(default_factory=dict)
    company_sector: dict[str, str] = field(default_factory=dict)

    def contains(self, granted_scope: AccessScope, target_scope: AccessScope) -> bool:
        if granted_scope.type == ScopeType.GLOBAL:
            return True
        if target_scope.type == ScopeType.GLOBAL:
            return False
        if granted_scope.type == target_scope.type:
            if granted_scope.id is None or target_scope.id is None:
                return granted_scope.id is None and target_scope.id is None
            return granted_scope.id == target_scope.id
        if (
            granted_scope.type == ScopeType.SECTOR
            and target_scope.type == ScopeType.COMPANY
        ):
            return self.company_sector.get(target_scope.id or "") == granted_scope.id
        if (
            granted_scope.type == ScopeType.SECTOR
            and target_scope.type == ScopeType.CHANNEL
        ):
            return self.channel_sector.get(target_scope.id or "") == granted_scope.id
        if (
            granted_scope.type == ScopeType.COMPANY
            and target_scope.type == ScopeType.CHANNEL
        ):
            return self.channel_company.get(target_scope.id or "") == granted_scope.id
        return False
