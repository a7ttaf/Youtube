from typing import Protocol

from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.policy import can_view_neo4j_graph
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex


class GraphAccessDeniedError(PermissionError):
    pass


class ReadOnlyGraphClient(Protocol):
    def execute_read(
        self, query_name: str, parameters: dict[str, object]
    ) -> list[dict[str, object]]: ...


class Neo4jReadOnlyService:
    def __init__(self, client: ReadOnlyGraphClient, org_index: OrgAccessIndex):
        self._client = client
        self._org_index = org_index

    def revenue_flow(
        self,
        user: UserPrincipal,
        scope: AccessScope,
        *,
        month: str,
    ) -> list[dict[str, object]]:
        if not can_view_neo4j_graph(
            user, scope, self._org_index, contains_finance=True
        ):
            raise GraphAccessDeniedError(
                "User cannot view finance graph for requested scope"
            )

        rows = self._client.execute_read(
            "revenue_flow",
            {
                "month": month,
                "scope_type": scope.type.value,
                "scope_id": scope.id,
            },
        )
        return [row for row in rows if self._row_in_scope(row, scope)]

    def hierarchy(
        self,
        user: UserPrincipal,
        scope: AccessScope,
    ) -> list[dict[str, object]]:
        if not can_view_neo4j_graph(user, scope, self._org_index):
            raise GraphAccessDeniedError("User cannot view graph for requested scope")

        rows = self._client.execute_read(
            "hierarchy",
            {
                "scope_type": scope.type.value,
                "scope_id": scope.id,
            },
        )
        return [row for row in rows if self._row_in_scope(row, scope)]

    def _row_in_scope(self, row: dict[str, object], scope: AccessScope) -> bool:
        if scope.type.value == "global":
            return True

        channel_id = row.get("channel_id")
        company_id = row.get("company_id")
        sector_id = row.get("sector_id")

        if isinstance(channel_id, str):
            if not self._org_index.contains(scope, AccessScope.channel(channel_id)):
                return False
            expected_company = self._org_index.channel_company.get(channel_id)
            expected_sector = self._org_index.channel_sector.get(channel_id)
            if isinstance(company_id, str) and company_id != expected_company:
                return False
            if isinstance(sector_id, str) and sector_id != expected_sector:
                return False
            return True

        if isinstance(company_id, str):
            if not self._org_index.contains(scope, AccessScope.company(company_id)):
                return False
            expected_sector = self._org_index.company_sector.get(company_id)
            return not isinstance(sector_id, str) or sector_id == expected_sector

        if isinstance(sector_id, str):
            return self._org_index.contains(scope, AccessScope.sector(sector_id))

        return scope.type.value == "global"
