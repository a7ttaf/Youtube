from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api import revenue as revenue_api
from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.finance_models import (
    DeductionComponentORM,
    FinanceBase,
    MonthlyChannelRevenueFactORM,
    RevenueManualOverrideORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

SECTOR_ID = UUID("00000000-0000-0000-0000-00000000c101")
COMPANY_ID = UUID("00000000-0000-0000-0000-00000000c201")
CHANNEL_A_ROW_ID = UUID("00000000-0000-0000-0000-00000000c301")
CHANNEL_B_ROW_ID = UUID("00000000-0000-0000-0000-00000000c302")
USER_ID = UUID("00000000-0000-0000-0000-00000000c401")
APPROVER_ID = UUID("00000000-0000-0000-0000-00000000c402")


def auth_headers(
    role: str,
    scope_type: str = "global",
    scope_id: str | None = None,
) -> dict[str, str]:
    """Build trusted-gateway auth headers for the given role and scope."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "net-revenue@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    """Return a unique SQLite URL under pytest's temp path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed_database(database_url: str) -> None:
    """Seed org, security, and finance rows for net-revenue test isolation."""
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                OrgUnitORM(
                    id=SECTOR_ID,
                    parent_id=None,
                    type="SECTOR",
                    name="TV",
                    active=True,
                ),
                OrgUnitORM(
                    id=COMPANY_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="TV Company",
                    active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_A_ROW_ID,
                    youtube_channel_id="channel-tv-a",
                    channel_name="TV A",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_B_ROW_ID,
                    youtube_channel_id="channel-tv-b",
                    channel_name="TV B",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    source_kind="YOUTUBE_CMS",
                    source_report_id="cms-report-2026-03",
                    gross_revenue_usd=Decimal("1000.00"),
                    net_revenue_usd=Decimal("880.00"),
                    views=250000,
                    watch_time_minutes=Decimal("7200.50"),
                    confidence_score=Decimal("0.9825"),
                    imported_by=USER_ID,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-b",
                    source_kind="YOUTUBE_CMS",
                    source_report_id="cms-report-2026-03",
                    gross_revenue_usd=Decimal("200.00"),
                    net_revenue_usd=None,
                    views=50000,
                    watch_time_minutes=Decimal("1400.00"),
                    confidence_score=Decimal("0.9500"),
                    imported_by=USER_ID,
                ),
                RevenueManualOverrideORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    adjustment_revenue_usd=Decimal("50.00"),
                    reason="Finance correction",
                    status="APPROVED",
                    created_by=USER_ID,
                    approved_by=APPROVER_ID,
                    approved_at=datetime(2026, 4, 25, tzinfo=UTC),
                    approval_reason="Approved correction",
                ),
                UserORM(
                    id=USER_ID,
                    email="net-revenue@example.com",
                    display_name="Net Revenue User",
                ),
            ]
        )
        session.commit()


def test_finance_viewer_reads_month_net_revenue_summary_with_audit(tmp_path):
    """Finance viewer reads the company-scoped monthly net-revenue summary and emits a REVENUE_VIEWED audit event."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}",
        headers=auth_headers("finance_viewer", "company", str(COMPANY_ID)),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_kinds = {row.event_type for row in session.scalars(select(AuditLogORM)).all()}

    assert response.status_code == 200
    assert response.json()["status"] == "PARTIAL"
    assert response.json()["channel_count"] == 2
    assert response.json()["calculated_channel_count"] == 1
    assert response.json()["missing_net_source_count"] == 1
    assert response.json()["total_adjusted_gross_revenue_usd"] == "1250"
    assert response.json()["total_net_revenue_usd"] == "930"
    assert {e["event_type"] for e in response.json()["audit_events"]} == {
        "REVENUE_VIEWED",
        "PAYMENT_VIEWED",
    }
    assert audit_kinds == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}


def test_assistant_cannot_read_month_net_revenue_summary_by_default(tmp_path):
    """assistant_analyst lacks VIEW_REVENUE and is rejected with HTTP 403 on the net-revenue endpoint."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/net-revenue",
        headers=auth_headers("assistant_analyst", "global"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_month_net_revenue_summary_rejects_non_usd_until_fx_support(tmp_path):
    """Requesting a non-USD currency is rejected with HTTP 422 until exchange-rate support is implemented."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/net-revenue?currency=EUR",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "currency must be USD until exchange-rate support is implemented"
    )


def test_net_revenue_endpoint_derives_component_net_for_missing_net_channel(tmp_path):
    """Net-revenue endpoint derives COMPONENT_DERIVED net via youtube_reporting component when source net is absent."""
    # channel-tv-b has 2026-03 fact with net_revenue_usd=None, gross=200.00,
    # source_kind=YOUTUBE_CMS. A youtube_reporting component (maps to YOUTUBE_CMS)
    # of 20.00 must derive net=180.00 and flip the channel to COMPONENT_DERIVED.
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            DeductionComponentORM(
                id=uuid4(),
                month="2026-03",
                component_kind="DEDUCTION",
                scope_kind="CHANNEL",
                scope_id="channel-tv-b",
                amount_usd=Decimal("20.00"),
                amount_native=None,
                currency_code="USD",
                source_system="youtube_reporting",
                source_table="google_revenue_source_rows",
                source_id=None,
                source_key="k-b",
                source_report_id=None,
                raw_payload={"k": "v"},
                component_key="srcrow:youtube_reporting:k-b",
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}",
        headers=auth_headers("finance_viewer", "company", str(COMPANY_ID)),
    )
    assert response.status_code == 200
    body = response.json()
    channels_by_id = {c["youtube_channel_id"]: c for c in body["channels"]}
    assert "channel-tv-b" in channels_by_id
    channel_b = channels_by_id["channel-tv-b"]
    assert channel_b["status"] == "COMPONENT_DERIVED"
    assert channel_b["net_revenue_usd"] == "180"   # 200 - 20, trimmed
    assert channel_b["deduction_amount_usd"] == "20"
    assert body["missing_net_source_count"] == 0   # b is now derived, not missing
    assert {e["event_type"] for e in body["audit_events"]} == {
        "REVENUE_VIEWED",
        "PAYMENT_VIEWED",
    }


def test_net_revenue_endpoint_requests_only_net_applicable_components(tmp_path):
    """Net-revenue route asks the repository for only TAX/DEDUCTION component kinds."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)

    class RecordingDeductionComponentRepository:
        """Capture the component_kinds argument passed by the route."""

        def __init__(self):
            self.component_kinds = None

        def list_month_components(
            self,
            *,
            month,
            youtube_channel_ids=None,
            component_kinds=None,
        ):
            """Record route-supplied component filters and return no deduction rows."""
            self.component_kinds = component_kinds
            return []

        def list_account_components(self, *, month, adsense_account_id=None):
            """No ACCOUNT-grain rows; the allocation orchestrator needs this method."""
            return []

    repository = RecordingDeductionComponentRepository()
    app = create_app(database_url=database_url)
    app.dependency_overrides[
        revenue_api.current_deduction_component_repository
    ] = lambda: repository
    client = TestClient(app)

    response = client.get(
        f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}",
        headers=auth_headers("finance_viewer", "company", str(COMPANY_ID)),
    )

    assert response.status_code == 200
    assert set(repository.component_kinds) == {"TAX", "DEDUCTION"}


def test_net_revenue_forbidden_without_finalized_payment_permission(tmp_path):
    """A principal with VIEW_REVENUE + VIEW_CONFIDENCE but NOT VIEW_FINALIZED_PAYMENTS
    is rejected by the new gate (fail-closed)."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="revenue-no-payments@example.com",
        direct_permissions=(
            PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope()),
            PermissionGrant(Permission.VIEW_CONFIDENCE, AccessScope.global_scope()),
            # deliberately NO VIEW_FINALIZED_PAYMENTS grant
        ),
    )
    client = TestClient(app)
    response = client.get("/revenue/months/2026-03/net-revenue?scope_type=global")
    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_finalized_payments"


def test_net_revenue_scoped_omits_unallocated_surface(tmp_path):
    """A scoped (company) request serializes unallocated-account fields as null."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}",
        headers=auth_headers("finance_viewer", "company", str(COMPANY_ID)),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["unallocated_account_deduction_total_usd"] is None
    assert body["unallocated_account_issues"] is None


def test_net_revenue_global_includes_unallocated_surface(tmp_path):
    """A global request includes the unallocated-account surface (possibly empty)."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/revenue/months/2026-03/net-revenue?scope_type=global",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 200
    body = response.json()
    # present (not None) at global scope — empty when nothing unallocated
    assert body["unallocated_account_deduction_total_usd"] is not None
    assert body["unallocated_account_issues"] is not None


def test_net_revenue_global_visibility_uses_normalized_scope_type(tmp_path):
    """Whitespace-normalized global scope still receives the global-only surface."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/revenue/months/2026-03/net-revenue",
        params={"scope_type": " global "},
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["unallocated_account_deduction_total_usd"] is not None
    assert body["unallocated_account_issues"] is not None

    engine = create_engine(database_url)
    with Session(engine) as session:
        entity_ids = {row.entity_id for row in session.scalars(select(AuditLogORM)).all()}

    assert entity_ids == {"2026-03:global:global"}
