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
    AdsenseContentOwnerLinkORM,
    ContentOwnerChannelLinkORM,
    DeductionComponentORM,
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
    RevenueManualOverrideORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
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

SECTOR_ID = UUID("00000000-0000-0000-0000-00000000c101")
COMPANY_ID = UUID("00000000-0000-0000-0000-00000000c201")
COMPANY_2_ID = UUID("00000000-0000-0000-0000-00000000c202")
CHANNEL_A_ROW_ID = UUID("00000000-0000-0000-0000-00000000c301")
CHANNEL_B_ROW_ID = UUID("00000000-0000-0000-0000-00000000c302")
CHANNEL_C_ROW_ID = UUID("00000000-0000-0000-0000-00000000c303")
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


def _company_finance_principal() -> UserPrincipal:
    """Return a principal authorized for company-scoped net-revenue reads."""
    return UserPrincipal(
        user_id=str(USER_ID),
        email="net-revenue@example.com",
        direct_permissions=(
            PermissionGrant(Permission.VIEW_REVENUE, AccessScope.company(str(COMPANY_ID))),
            PermissionGrant(Permission.VIEW_CONFIDENCE, AccessScope.company(str(COMPANY_ID))),
            PermissionGrant(
                Permission.VIEW_FINALIZED_PAYMENTS, AccessScope.finance_month("2026-03")
            ),
        ),
    )


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
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _company_finance_principal
    client = TestClient(app)

    response = client.get(
        f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}",
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
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _company_finance_principal
    client = TestClient(app)
    response = client.get(
        f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}",
    )
    assert response.status_code == 200
    body = response.json()
    channels_by_id = {c["youtube_channel_id"]: c for c in body["channels"]}
    assert "channel-tv-b" in channels_by_id
    channel_b = channels_by_id["channel-tv-b"]
    assert channel_b["status"] == "COMPONENT_DERIVED"
    assert channel_b["net_revenue_usd"] == "180"   # 200 - 20, trimmed
    assert channel_b["deduction_amount_usd"] == "20"
    assert body["total_channel_direct_deduction_amount_usd"] == "20"
    assert body["total_account_allocated_deduction_amount_usd"] == "0"
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

        @staticmethod
        def list_account_components(*, month, adsense_account_id=None):
            """No ACCOUNT-grain rows; the allocation orchestrator needs this method."""
            return []

    repository = RecordingDeductionComponentRepository()
    app = create_app(database_url=database_url)
    app.dependency_overrides[
        revenue_api.current_deduction_component_repository
    ] = lambda: repository
    app.dependency_overrides[current_principal_from_headers] = _company_finance_principal
    client = TestClient(app)

    response = client.get(
        f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}",
    )

    assert response.status_code == 200
    assert set(repository.component_kinds) == {"TAX", "DEDUCTION"}


def test_net_revenue_forbidden_without_finalized_payment_permission(tmp_path):
    """A principal with VIEW_REVENUE + VIEW_CONFIDENCE but NOT VIEW_FINALIZED_PAYMENTS
    is rejected by the new gate (fail-closed).
    """
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
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _company_finance_principal
    client = TestClient(app)
    response = client.get(
        f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}",
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


def _seed_out_of_scope_account_allocation(database_url: str) -> None:
    """Add a second company + channel ("channel-tv-c") that has an ACCOUNT
    deduction mapped via a VERIFIED link. It is outside COMPANY_ID and must not
    appear in a COMPANY_ID-scoped net-revenue response.
    """
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
                    adsense_account_id="pub-9",
                    content_owner_id="owner-9",
                    verification_status="VERIFIED",
                    provenance_kind="OPERATOR_ASSERTED",
                    provenance_payload={},
                    effective_month_start="2026-01",
                ),
                ContentOwnerChannelLinkORM(
                    id=uuid4(),
                    content_owner_id="owner-9",
                    youtube_channel_id="channel-tv-c",
                    provenance_kind="SOURCE_ROW",
                    active=True,
                    effective_month_start="2026-01",
                ),
                DeductionComponentORM(
                    id=uuid4(),
                    month="2026-03",
                    component_kind="DEDUCTION",
                    scope_kind="ACCOUNT",
                    scope_id="pub-9",
                    amount_usd=Decimal("70.00"),
                    amount_native=None,
                    currency_code="USD",
                    source_system="adsense_management",
                    source_table="google_revenue_source_rows",
                    source_id=None,
                    source_key="k-c",
                    source_report_id=None,
                    raw_payload={"k": "v"},
                    component_key="srcrow:adsense_management:k-c",
                ),
            ]
        )
        session.commit()


def test_net_revenue_scoped_excludes_out_of_scope_account_allocation(tmp_path):
    """Regression (PR #59 review): account allocations resolve month-wide, so a
    COMPANY_ID-scoped request must not surface "channel-tv-c" (a different
    company) just because it has an account-level deduction.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_out_of_scope_account_allocation(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _company_finance_principal
    client = TestClient(app)

    response = client.get(
        f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}",
    )

    assert response.status_code == 200
    body = response.json()
    channel_ids = {c["youtube_channel_id"] for c in body["channels"]}
    assert "channel-tv-c" not in channel_ids  # out-of-scope allocation filtered
    assert channel_ids == {"channel-tv-a", "channel-tv-b"}
    assert body["channel_count"] == 2


def _lock_month(database_url: str, month: str) -> None:
    """Mark a finance month LOCKED so readers prefer the committed snapshot.

    A committed snapshot already created an OPEN close row, so update it rather than
    insert a second (which would violate the (tenant_id, month) UNIQUE constraint).
    """
    engine = create_engine(database_url)
    with Session(engine) as session:
        row = session.scalars(
            select(FinanceMonthCloseORM).where(
                FinanceMonthCloseORM.tenant_id == UUID(UMS_TENANT_ID),
                FinanceMonthCloseORM.month == month,
            )
        ).one_or_none()
        if row is None:
            session.add(FinanceMonthCloseORM(
                tenant_id=UUID(UMS_TENANT_ID), month=month, status="LOCKED",
                allocation_rule_payload={},
            ))
        else:
            row.status = "LOCKED"
        session.commit()


def test_net_revenue_open_month_reports_live_provenance(tmp_path):
    """An OPEN month serves live compute and discloses allocation_source=live_compute."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    app = create_app(database_url=database_url)
    app.dependency_overrides[current_principal_from_headers] = _company_finance_principal
    client = TestClient(app)
    resp = client.get(f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}")
    assert resp.status_code == 200
    assert resp.json()["allocation_source"] == "live_compute"
    assert resp.json()["committed_run"] is None


CHANNEL_D_ROW_ID = UUID("00000000-0000-0000-0000-00000000c304")


def _seed_in_scope_account_allocation(database_url: str) -> None:
    """Map an ACCOUNT deduction (pub-7) to a new in-scope, missing-net ADSENSE channel.

    channel-tv-d is in COMPANY_ID with a 2026-03 ADSENSE gross 500 and no source net,
    so the account-allocated DEDUCTION (70.00, net-applicable) derives its net on the
    missing-net path. The Adsense->owner->channel links are VERIFIED and the basis is
    source-aligned (ADSENSE), so the month-wide commit fully allocates pub-7 with zero
    unallocated.
    """
    engine = create_engine(database_url)
    TenantBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            TenantORM(
                id=UUID(UMS_TENANT_ID), slug="ums", display_name="UMS",
                primary_currency="USD", status="ACTIVE",
            ),
            YouTubeChannelORM(
                id=CHANNEL_D_ROW_ID, youtube_channel_id="channel-tv-d",
                channel_name="TV D", primary_org_unit_id=COMPANY_ID,
                cms_status="INSIDE_CMS", revenue_required=True, active=True,
            ),
            MonthlyChannelRevenueFactORM(
                id=uuid4(), month="2026-03", youtube_channel_id="channel-tv-d",
                source_kind="ADSENSE", source_report_id="adsense-report-2026-03",
                gross_revenue_usd=Decimal("500.00"), net_revenue_usd=None,
                views=80000, watch_time_minutes=Decimal("2000.00"),
                confidence_score=Decimal("0.9600"), imported_by=USER_ID,
            ),
            AdsenseContentOwnerLinkORM(
                id=uuid4(), adsense_account_id="pub-7", content_owner_id="owner-7",
                verification_status="VERIFIED", provenance_kind="OPERATOR_ASSERTED",
                provenance_payload={}, effective_month_start="2026-01",
            ),
            ContentOwnerChannelLinkORM(
                id=uuid4(), content_owner_id="owner-7", youtube_channel_id="channel-tv-d",
                provenance_kind="SOURCE_ROW", active=True, effective_month_start="2026-01",
            ),
            DeductionComponentORM(
                id=uuid4(), month="2026-03", component_kind="DEDUCTION",
                scope_kind="ACCOUNT", scope_id="pub-7", amount_usd=Decimal("70.00"),
                amount_native=None, currency_code="USD",
                source_system="adsense_management",
                source_table="google_revenue_source_rows", source_id=None,
                source_key="k-7", source_report_id=None, raw_payload={"k": "v"},
                component_key="srcrow:adsense_management:k-7",
            ),
        ])
        session.commit()


def test_net_revenue_locked_month_serves_committed_snapshot(tmp_path):
    """LOCKED month serves the committed snapshot, not live: the account-allocated
    total reflects the PRE-mutation snapshot value even after the underlying
    DEDUCTION row is mutated to a different amount.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_in_scope_account_allocation(database_url)

    engine = create_engine(database_url)
    with Session(engine) as session:
        committed = SqlAlchemyCommittedAllocationRepository(session)
        committed.commit_allocation(
            month="2026-03", allocation_method="gross_revenue_proportional",
            idempotency_key="k1", request_fingerprint="fp1", reason="close",
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
    resp = client.get(f"/revenue/months/2026-03/net-revenue?scope_type=company&scope_id={COMPANY_ID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["allocation_source"] == "committed_snapshot"
    assert body["committed_run"]["commit_version"] == 1
    # Snapshot froze pub-7's 70.00 allocation; live would now show 999.00.
    assert body["total_account_allocated_deduction_amount_usd"] == "70"
