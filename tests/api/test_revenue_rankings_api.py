from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import current_principal_from_headers
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.models import PermissionGrant, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.finance_models import (
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
    RevenueManualOverrideORM,
)
from ums_smart_revenue.db.org_models import (
    ChannelGroupMemberORM,
    ChannelGroupORM,
    OrgBase,
    OrgUnitORM,
    YouTubeChannelORM,
)
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.db.tenant_models import TenantBase, TenantORM
from ums_smart_revenue.finance.channel_account_links import (
    SqlAlchemyChannelAccountLinkRepository,
)
from ums_smart_revenue.finance.committed_allocation import (
    SqlAlchemyCommittedAllocationRepository,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.revenue_facts import SqlAlchemyRevenueFactRepository
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

SECTOR_ID = UUID("00000000-0000-0000-0000-00000000d101")
COMPANY_ID = UUID("00000000-0000-0000-0000-00000000d201")
COMPANY_2_ID = UUID("00000000-0000-0000-0000-00000000d202")
CHANNEL_A_ROW_ID = UUID("00000000-0000-0000-0000-00000000d301")
CHANNEL_B_ROW_ID = UUID("00000000-0000-0000-0000-00000000d302")
CHANNEL_C_ROW_ID = UUID("00000000-0000-0000-0000-00000000d303")
CHANNEL_D_ROW_ID = UUID("00000000-0000-0000-0000-00000000d304")
GROUP_ID = UUID("00000000-0000-0000-0000-00000000d501")
USER_ID = UUID("00000000-0000-0000-0000-00000000d401")
APPROVER_ID = UUID("00000000-0000-0000-0000-00000000d402")


def auth_headers(
    role: str,
    scope_type: str = "global",
    scope_id: str | None = None,
) -> dict[str, str]:
    """Build trusted-gateway auth headers for the given role and scope."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "rankings@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def _company_finance_principal() -> UserPrincipal:
    """Return a principal authorized for company-scoped rankings reads."""
    return UserPrincipal(
        user_id=str(USER_ID),
        email="rankings@example.com",
        direct_permissions=(
            PermissionGrant(Permission.VIEW_REVENUE, AccessScope.company(str(COMPANY_ID))),
            PermissionGrant(Permission.VIEW_CONFIDENCE, AccessScope.company(str(COMPANY_ID))),
            PermissionGrant(
                Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month("2026-03")
            ),
        ),
    )


def _channel_a_finance_principal() -> UserPrincipal:
    """Return a principal authorized for channel-tv-a rankings reads."""
    return UserPrincipal(
        user_id=str(USER_ID),
        email="rankings@example.com",
        direct_permissions=(
            PermissionGrant(Permission.VIEW_REVENUE, AccessScope.channel("channel-tv-a")),
            PermissionGrant(Permission.VIEW_CONFIDENCE, AccessScope.channel("channel-tv-a")),
            PermissionGrant(
                Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month("2026-03")
            ),
        ),
    )


def build_database_url(tmp_path) -> str:
    """Return a unique SQLite URL under pytest's temp path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed_database(database_url: str) -> None:
    """Seed org, security, and finance rows for rankings test isolation."""
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
                    email="rankings@example.com",
                    display_name="Rankings User",
                ),
            ]
        )
        session.commit()


def _seed_group(database_url: str, *, channel_row_ids: tuple[UUID, ...]) -> None:
    """Seed one active channel group for group-scoped rankings."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            ChannelGroupORM(
                id=GROUP_ID,
                name="TV A Group",
                group_type="CUSTOM_GROUP",
                active=True,
            )
        )
        session.add_all(
            [
                ChannelGroupMemberORM(group_id=GROUP_ID, channel_id=channel_row_id)
                for channel_row_id in channel_row_ids
            ]
        )
        session.commit()


def test_finance_viewer_reads_month_rankings_with_audit(tmp_path):
    """A company-scoped finance viewer reads ranked channels/companies + audit."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _company_finance_principal
    client = TestClient(app)

    response = client.get(
        f"/revenue/months/2026-03/rankings?scope_type=company&scope_id={COMPANY_ID}",
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_kinds = {row.event_type for row in session.scalars(select(AuditLogORM)).all()}

    assert response.status_code == 200
    body = response.json()
    assert body["month"] == "2026-03"
    assert body["metric"] == "gross"
    channel_ids = [e["entity_id"] for e in body["channels"]]
    assert channel_ids == ["channel-tv-a", "channel-tv-b"]
    assert body["channels"][0]["rank"] == 1
    assert body["channels"][0]["gross_revenue_usd"] == "1050"
    assert body["channels"][1]["gross_revenue_usd"] == "200"
    company = body["companies"][0]
    assert company["entity_id"] == str(COMPANY_ID)
    assert company["entity_name"] == "TV Company"
    assert company["gross_revenue_usd"] == "1250"
    assert {e["event_type"] for e in body["audit_events"]} == {
        "REVENUE_VIEWED",
        "PAYMENT_VIEWED",
    }
    assert audit_kinds == {"REVENUE_VIEWED", "PAYMENT_VIEWED"}


def test_group_scope_reads_only_member_channel_rankings_with_audit(tmp_path):
    """A group-scoped rankings read is filtered to group members and audited."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_group(database_url, channel_row_ids=(CHANNEL_A_ROW_ID,))
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _channel_a_finance_principal
    client = TestClient(app)

    response = client.get(
        f"/revenue/months/2026-03/rankings?scope_type=group&scope_id={GROUP_ID}",
    )

    assert response.status_code == 200
    body = response.json()
    assert [channel["entity_id"] for channel in body["channels"]] == ["channel-tv-a"]
    assert body["channels"][0]["gross_revenue_usd"] == "1050"
    assert [company["entity_id"] for company in body["companies"]] == [str(COMPANY_ID)]
    assert {event["event_type"] for event in body["audit_events"]} == {
        "REVENUE_VIEWED",
        "PAYMENT_VIEWED",
    }

    engine = create_engine(database_url)
    with Session(engine) as session:
        revenue_event = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == "REVENUE_VIEWED")
        ).one()
    assert revenue_event.scope_type == "group"
    assert revenue_event.scope_id == str(GROUP_ID)


def test_assistant_cannot_read_month_rankings_by_default(tmp_path):
    """assistant_analyst lacks VIEW_REVENUE and is rejected with HTTP 403."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/rankings",
        headers=auth_headers("assistant_analyst", "global"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_rankings_metric_net_orders_by_net(tmp_path):
    """metric=net ranks channels by net revenue, None-net channels sinking last."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _company_finance_principal
    client = TestClient(app)

    response = client.get(
        f"/revenue/months/2026-03/rankings?scope_type=company&scope_id={COMPANY_ID}&metric=net",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["metric"] == "net"
    # channel-tv-a has net (930), channel-tv-b has None net -> a first, b last.
    assert [e["entity_id"] for e in body["channels"]] == [
        "channel-tv-a",
        "channel-tv-b",
    ]
    assert body["channels"][1]["net_revenue_usd"] is None


def test_rankings_rejects_unknown_metric(tmp_path):
    """An unsupported metric is rejected with HTTP 422."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/rankings?metric=profit",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 422


def test_rankings_rejects_bad_month(tmp_path):
    """A malformed month is rejected with HTTP 422."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-13/rankings",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 422


def test_rankings_rejects_out_of_range_limit(tmp_path):
    """A limit above the cap is rejected with HTTP 422."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/rankings?limit=101",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 422


def _seed_out_of_scope_channel(database_url: str) -> None:
    """Add a second company + channel outside COMPANY_ID with its own revenue."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all(
            [
                OrgUnitORM(
                    id=COMPANY_2_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="Other Company",
                    active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_C_ROW_ID,
                    youtube_channel_id="channel-tv-c",
                    channel_name="TV C",
                    primary_org_unit_id=COMPANY_2_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-c",
                    source_kind="YOUTUBE_CMS",
                    source_report_id="cms-report-2026-03",
                    gross_revenue_usd=Decimal("9999.00"),
                    net_revenue_usd=Decimal("9000.00"),
                    views=80000,
                    watch_time_minutes=Decimal("2000.00"),
                    confidence_score=Decimal("0.9600"),
                    imported_by=USER_ID,
                ),
            ]
        )
        session.commit()


def test_rankings_scope_isolation_excludes_other_company(tmp_path):
    """A COMPANY_ID-scoped read must not rank a different company's channel."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_out_of_scope_channel(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _company_finance_principal
    client = TestClient(app)

    response = client.get(
        f"/revenue/months/2026-03/rankings?scope_type=company&scope_id={COMPANY_ID}",
    )

    assert response.status_code == 200
    body = response.json()
    channel_ids = {e["entity_id"] for e in body["channels"]}
    assert channel_ids == {"channel-tv-a", "channel-tv-b"}
    assert "channel-tv-c" not in channel_ids
    company_ids = {e["entity_id"] for e in body["companies"]}
    assert str(COMPANY_2_ID) not in company_ids


def test_rankings_zero_channels_returns_empty_not_403(tmp_path):
    """A scoped read with no in-scope channels returns empty rankings, not 403."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    empty_company = "00000000-0000-0000-0000-0000000000ff"
    app.dependency_overrides[current_principal_from_headers] = lambda: UserPrincipal(
        user_id=str(USER_ID),
        email="rankings@example.com",
        direct_permissions=(
            PermissionGrant(Permission.VIEW_REVENUE, AccessScope.company(empty_company)),
            PermissionGrant(Permission.VIEW_CONFIDENCE, AccessScope.company(empty_company)),
            PermissionGrant(
                Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month("2026-03")
            ),
        ),
    )
    client = TestClient(app)

    response = client.get(
        f"/revenue/months/2026-03/rankings?scope_type=company&scope_id={empty_company}",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["channels"] == []
    assert body["companies"] == []
    assert body["sectors"] == []


def _lock_month(database_url: str, month: str) -> None:
    """Mark a finance month LOCKED so readers prefer the committed snapshot."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        row = session.scalars(
            select(FinanceMonthCloseORM).where(
                FinanceMonthCloseORM.tenant_id == UUID(UMS_TENANT_ID),
                FinanceMonthCloseORM.month == month,
            )
        ).one_or_none()
        if row is None:
            session.add(
                FinanceMonthCloseORM(
                    tenant_id=UUID(UMS_TENANT_ID),
                    month=month,
                    status="LOCKED",
                    allocation_rule_payload={},
                )
            )
        else:
            row.status = "LOCKED"
        session.commit()


def _seed_in_scope_account_allocation(database_url: str) -> None:
    """Map an ACCOUNT deduction to a new in-scope, missing-net ADSENSE channel."""
    engine = create_engine(database_url)
    TenantBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                TenantORM(
                    id=UUID(UMS_TENANT_ID),
                    slug="ums",
                    display_name="UMS",
                    primary_currency="USD",
                    status="ACTIVE",
                ),
                YouTubeChannelORM(
                    id=CHANNEL_D_ROW_ID,
                    youtube_channel_id="channel-tv-d",
                    channel_name="TV D",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-d",
                    source_kind="ADSENSE",
                    source_report_id="adsense-report-2026-03",
                    gross_revenue_usd=Decimal("500.00"),
                    net_revenue_usd=None,
                    views=80000,
                    watch_time_minutes=Decimal("2000.00"),
                    confidence_score=Decimal("0.9600"),
                    imported_by=USER_ID,
                ),
                AdsenseContentOwnerLinkORM(
                    id=uuid4(),
                    adsense_account_id="pub-7",
                    content_owner_id="owner-7",
                    verification_status="VERIFIED",
                    provenance_kind="OPERATOR_ASSERTED",
                    provenance_payload={},
                    effective_month_start="2026-01",
                ),
                ContentOwnerChannelLinkORM(
                    id=uuid4(),
                    content_owner_id="owner-7",
                    youtube_channel_id="channel-tv-d",
                    provenance_kind="SOURCE_ROW",
                    active=True,
                    effective_month_start="2026-01",
                ),
                DeductionComponentORM(
                    id=uuid4(),
                    month="2026-03",
                    component_kind="DEDUCTION",
                    scope_kind="ACCOUNT",
                    scope_id="pub-7",
                    amount_usd=Decimal("70.00"),
                    amount_native=None,
                    currency_code="USD",
                    source_system="adsense_management",
                    source_table="google_revenue_source_rows",
                    source_id=None,
                    source_key="k-7",
                    source_report_id=None,
                    raw_payload={"k": "v"},
                    component_key="srcrow:adsense_management:k-7",
                ),
            ]
        )
        session.commit()


def test_rankings_locked_month_serves_committed_snapshot(tmp_path):
    """A LOCKED month ranks the committed-snapshot deduction, not live compute."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_in_scope_account_allocation(database_url)

    engine = create_engine(database_url)
    with Session(engine) as session:
        committed = SqlAlchemyCommittedAllocationRepository(session)
        committed.commit_allocation(
            month="2026-03",
            allocation_method="gross_revenue_proportional",
            idempotency_key="k1",
            request_fingerprint="fp1",
            reason="close",
            committed_by=UMS_TENANT_ID,
            deduction_repository=SqlAlchemyDeductionComponentRepository(session),
            revenue_repository=SqlAlchemyRevenueFactRepository(session),
            link_repository=SqlAlchemyChannelAccountLinkRepository(session),
        )
        session.commit()

    # Mutate the source DEDUCTION so live compute would differ from the snapshot.
    with Session(engine) as session:
        row = session.scalars(
            select(DeductionComponentORM).where(
                DeductionComponentORM.component_key == "srcrow:adsense_management:k-7"
            )
        ).one()
        row.amount_usd = Decimal("999.00")
        session.commit()

    _lock_month(database_url, "2026-03")

    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _company_finance_principal
    client = TestClient(app)

    response = client.get(
        f"/revenue/months/2026-03/rankings"
        f"?scope_type=company&scope_id={COMPANY_ID}&metric=deduction",
    )

    assert response.status_code == 200
    body = response.json()
    channels_by_id = {e["entity_id"]: e for e in body["channels"]}
    # Snapshot froze pub-7's 70.00 allocation for channel-tv-d; live=999.00.
    assert channels_by_id["channel-tv-d"]["deduction_amount_usd"] == "70"
    # The route surfaces the committed-snapshot provenance so the FE badge can
    # distinguish a frozen LOCKED snapshot from a live_fallback.
    assert body["allocation_source"] == "committed_snapshot"
    assert body["committed_run"] is not None


def test_rankings_open_month_reports_live_allocation_source(tmp_path):
    """An OPEN month (no close row) reads live_compute, never committed_snapshot."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _company_finance_principal
    client = TestClient(app)

    response = client.get(
        f"/revenue/months/2026-03/rankings?scope_type=company&scope_id={COMPANY_ID}",
    )

    assert response.status_code == 200
    body = response.json()
    assert body["allocation_source"] in {"live_compute", "live_fallback"}
    assert body["committed_run"] is None


def test_rankings_allocation_source_matches_net_revenue(tmp_path):
    """Rankings allocation_source agrees with net-revenue for the same scope."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _company_finance_principal
    client = TestClient(app)

    rankings = client.get(
        f"/revenue/months/2026-03/rankings?scope_type=company&scope_id={COMPANY_ID}",
    )
    net_revenue = client.get(
        f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}",
    )

    assert rankings.status_code == 200
    assert net_revenue.status_code == 200
    assert rankings.json()["allocation_source"] == net_revenue.json()["allocation_source"]
