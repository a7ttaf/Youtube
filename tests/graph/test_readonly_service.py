import pytest

from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex
from ums_smart_revenue.graph.readonly_service import GraphAccessDenied, Neo4jReadOnlyService


class FakeGraphClient:
    def __init__(self):
        self.write_called = False

    def execute_read(self, query_name, parameters):
        assert query_name == "revenue_flow"
        assert "MERGE" not in query_name
        return [
            {
                "company_id": "company-tv-a",
                "channel_id": "channel-tv-a",
                "month": parameters["month"],
                "gross_revenue_usd": 1000,
            },
            {
                "company_id": "company-news-a",
                "channel_id": "channel-news-a",
                "month": parameters["month"],
                "gross_revenue_usd": 9000,
            },
        ]

    def execute_write(self, query_name, parameters):
        self.write_called = True
        raise AssertionError("dashboard graph service must not write to Neo4j")


@pytest.fixture
def org_index() -> OrgAccessIndex:
    return OrgAccessIndex(
        channel_company={
            "channel-tv-a": "company-tv-a",
            "channel-news-a": "company-news-a",
        },
        channel_sector={
            "channel-tv-a": "tv",
            "channel-news-a": "news",
        },
        company_sector={
            "company-tv-a": "tv",
            "company-news-a": "news",
        },
    )


def principal(role: RoleKey, scope: AccessScope) -> UserPrincipal:
    return UserPrincipal(
        user_id="user-1",
        email="user@example.com",
        role_assignments=[RoleAssignment(role=role, scope=scope)],
    )


def test_revenue_flow_filters_nodes_to_authorized_company(org_index):
    client = FakeGraphClient()
    service = Neo4jReadOnlyService(client=client, org_index=org_index)
    user = principal(RoleKey.FINANCE_VIEWER, AccessScope.company("company-tv-a"))

    result = service.revenue_flow(user, AccessScope.company("company-tv-a"), month="2026-03")

    assert result == [
        {
            "company_id": "company-tv-a",
            "channel_id": "channel-tv-a",
            "month": "2026-03",
            "gross_revenue_usd": 1000,
        }
    ]
    assert not client.write_called


def test_revenue_flow_rejects_assistant_without_finance_visibility(org_index):
    service = Neo4jReadOnlyService(client=FakeGraphClient(), org_index=org_index)
    user = principal(RoleKey.ASSISTANT_ANALYST, AccessScope.company("company-tv-a"))

    with pytest.raises(GraphAccessDenied):
        service.revenue_flow(user, AccessScope.company("company-tv-a"), month="2026-03")

