import json
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
from ums_smart_revenue.db.explanation_models import (
    ExplanationBase,
    NumberExplanationORM,
)
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

SECTOR_ID = UUID("00000000-0000-0000-0000-000000011101")
COMPANY_ID = UUID("00000000-0000-0000-0000-000000011201")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-000000011301")
FINANCE_USER_ID = UUID("00000000-0000-0000-0000-000000011401")
CREATOR_ID = UUID("00000000-0000-0000-0000-000000011402")
APPROVER_ID = UUID("00000000-0000-0000-0000-000000011403")


def auth_headers(
    role: str, user_id: UUID = FINANCE_USER_ID, scope_id: str | None = None
) -> dict[str, str]:
    """Build trusted-gateway request headers for the given role, user, and optional scope id."""
    headers = {
        "x-user-id": str(user_id),
        "x-user-email": f"{role}@example.com",
        "x-role": role,
        "x-scope-type": "company",
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    """Return a SQLite database URL rooted at the pytest tmp_path directory."""
    return f"sqlite+pysqlite:///{(tmp_path / 'revenue-explanations.db').as_posix()}"


def seed_database(database_url: str) -> None:
    """Create org/security/finance/explanation tables and seed
    channel, users, facts, and overrides.
    """
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    ExplanationBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                OrgUnitORM(id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV", active=True),
                OrgUnitORM(
                    id=COMPANY_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="TV Company",
                    active=True,
                ),
                YouTubeChannelORM(
                    id=CHANNEL_ROW_ID,
                    youtube_channel_id="channel-tv-a",
                    channel_name="TV A",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                UserORM(
                    id=FINANCE_USER_ID,
                    email="finance-viewer@example.com",
                    display_name="Finance Viewer",
                ),
                UserORM(
                    id=CREATOR_ID,
                    email="finance-admin@example.com",
                    display_name="Finance Admin",
                ),
                UserORM(
                    id=APPROVER_ID,
                    email="finance-approver@example.com",
                    display_name="Finance Approver",
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    source_kind="YOUTUBE_CMS",
                    source_report_id="cms-report-2026-03",
                    gross_revenue_usd=Decimal("1000.00"),
                    net_revenue_usd=Decimal("900.00"),
                    views=250000,
                    watch_time_minutes=Decimal("7200.50"),
                    confidence_score=Decimal("0.9800"),
                    imported_by=CREATOR_ID,
                ),
                RevenueManualOverrideORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    adjustment_revenue_usd=Decimal("125.50"),
                    reason="Correct CMS transfer-fee allocation",
                    status="APPROVED",
                    created_by=CREATOR_ID,
                    approved_by=APPROVER_ID,
                    approved_at=datetime.now(UTC),
                    approval_reason="Approved source correction",
                ),
                RevenueManualOverrideORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    adjustment_revenue_usd=Decimal("-50.00"),
                    reason="Pending dispute",
                    status="PENDING",
                    created_by=CREATOR_ID,
                ),
            ]
        )
        session.commit()


def test_finance_viewer_gets_adjusted_revenue_explanation_with_audit_and_snapshot(
    tmp_path,
):
    """Assert a finance_viewer gets a 200 adjusted_gross explanation
    with a pending-override warning, snapshot, log.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/channels/channel-tv-a/months/2026-03/explain?metric=adjusted_gross_revenue_usd",
        headers=auth_headers("finance_viewer", scope_id=str(COMPANY_ID)),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        explanation = session.scalars(select(NumberExplanationORM)).one()
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "month",
        "entity_type",
        "entity_id",
        "metric",
        "value",
        "currency",
        "formula",
        "confidence",
        "components",
        "warnings",
        "audit_event",
    }
    assert body["metric"] == "adjusted_gross_revenue_usd"
    assert response.json()["value"] == "1125.5"
    assert response.json()["currency"] == "USD"
    assert (
        response.json()["formula"]
        == "baseline_gross_revenue_usd + approved_manual_override_total_usd"
    )
    # The seeded month carries a PENDING override, so the response warns; a
    # warned explanation can never carry the HIGH badge (see _confidence).
    assert response.json()["confidence"] == {"label": "MEDIUM", "score": "0.9"}
    assert response.json()["components"] == [
        {
            "key": "baseline_gross_revenue_usd",
            "label": "Baseline gross revenue",
            "value": "1000",
            "source_kind": "YOUTUBE_CMS",
            "source_report_id": "cms-report-2026-03",
        },
        {
            "key": "approved_manual_override_total_usd",
            "label": "Approved manual overrides",
            "value": "125.5",
            "count": 1,
        },
    ]
    assert response.json()["warnings"] == [
        {
            "code": "PENDING_MANUAL_OVERRIDES",
            "message": ("1 pending manual override is not included in adjusted_gross_revenue_usd."),
        }
    ]
    assert response.json()["audit_event"]["event_type"] == "REVENUE_VIEWED"
    assert explanation.value == Decimal("1125.50")
    assert explanation.metric == "adjusted_gross_revenue_usd"
    assert json.loads(explanation.confidence) == {"label": "MEDIUM", "score": "0.9"}
    assert audit_log.event_type == "REVENUE_VIEWED"
    assert audit_log.sensitive is True


def test_assistant_cannot_get_revenue_explanation(tmp_path):
    """Assert an assistant_analyst is denied the explain endpoint
    with 403 and a missing finance.view_revenue detail.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/channels/channel-tv-a/months/2026-03/explain?metric=adjusted_gross_revenue_usd",
        headers=auth_headers("assistant_analyst", scope_id=str(COMPANY_ID)),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_revenue_explanation_rejects_unsupported_metric(tmp_path):
    """Assert an unsupported metric query returns 422
    with an unsupported-explanation-metric detail.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/channels/channel-tv-a/months/2026-03/explain?metric=bogus_metric",
        headers=auth_headers("finance_viewer", scope_id=str(COMPANY_ID)),
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail.startswith("Unsupported explanation metric: bogus_metric")
    assert "Supported:" in detail


def _finance_month_net_principal() -> UserPrincipal:
    """Return a principal authorized to explain net revenue for any channel-month.

    VIEW_REVENUE + VIEW_CONFIDENCE are granted at GLOBAL scope so the principal
    authorizes any channel target (including a channel with no facts, letting the
    no-facts test reach 422 rather than a 403), plus VIEW_FINALIZED_PAYMENTS at the
    finance_month scope the net-metric gate checks. Mirrors the direct-grant
    principal pattern in test_net_revenue_api.py.
    """
    return UserPrincipal(
        user_id=str(FINANCE_USER_ID),
        email="net-finance@example.com",
        direct_permissions=(
            PermissionGrant(Permission.VIEW_REVENUE, AccessScope.global_scope()),
            PermissionGrant(Permission.VIEW_CONFIDENCE, AccessScope.global_scope()),
            PermissionGrant(
                Permission.VIEW_FINALIZED_PAYMENTS,
                AccessScope.finance_month("2026-03"),
            ),
        ),
    )


def _post_net_explain(client, *, channel, month, principal):
    """POST the net-metric explain endpoint with an injected principal.

    Installs the principal via the FastAPI dependency override (mirroring
    test_net_revenue_api.py), POSTs ?metric=net_revenue_usd, and clears the
    override afterwards so other tests on the same app are unaffected.
    """
    client.app.dependency_overrides[current_principal_from_headers] = lambda: principal
    try:
        return client.post(
            f"/revenue/channels/{channel}/months/{month}/explain?metric=net_revenue_usd",
        )
    finally:
        client.app.dependency_overrides.pop(current_principal_from_headers, None)


def test_net_revenue_explanation_global_finance_returns_value_provenance_and_plural_audit(
    tmp_path,
):
    """Assert a global finance principal gets a 200 net_revenue_usd
    explanation with plural audits and one snapshot.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = _post_net_explain(
        client,
        channel="channel-tv-a",
        month="2026-03",
        principal=_finance_month_net_principal(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["metric"] == "net_revenue_usd"
    assert isinstance(body["value"], str)
    assert "audit_event" not in body
    assert [e["event_type"] for e in body["audit_events"]] == [
        "REVENUE_VIEWED",
        "PAYMENT_VIEWED",
    ]
    engine = create_engine(database_url)
    with Session(engine) as session:
        rows = session.scalars(
            select(NumberExplanationORM).where(NumberExplanationORM.metric == "net_revenue_usd")
        ).all()
    assert len(rows) == 1


def test_net_revenue_explanation_org_scoped_finance_viewer_403_on_net_200_on_gross(
    tmp_path,
):
    """Assert an org-scoped finance_viewer is denied net_revenue_usd
    (403) but allowed adjusted_gross_revenue (200).
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("finance_viewer", scope_id=str(COMPANY_ID))

    net = client.post(
        "/revenue/channels/channel-tv-a/months/2026-03/explain?metric=net_revenue_usd",
        headers=headers,
    )
    gross = client.post(
        "/revenue/channels/channel-tv-a/months/2026-03/explain?metric=adjusted_gross_revenue_usd",
        headers=headers,
    )
    assert net.status_code == 403
    assert gross.status_code == 200


def test_net_revenue_explanation_no_facts_channel_returns_422(tmp_path):
    """Assert net_revenue_usd for a fact-less channel returns 422
    and persists no explanation snapshot.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    # Seed an active channel with NO revenue facts for the gated month so the
    # request reaches the net builder (channel exists -> not a 404) and hits the
    # indeterminate-net (E_MISSING) path, which the service rejects with 422.
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            YouTubeChannelORM(
                id=UUID("00000000-0000-0000-0000-000000011302"),
                youtube_channel_id="channel-no-facts",
                channel_name="No Facts",
                primary_org_unit_id=COMPANY_ID,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                active=True,
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = _post_net_explain(
        client,
        channel="channel-no-facts",
        month="2026-03",
        principal=_finance_month_net_principal(),
    )

    assert response.status_code == 422
    engine = create_engine(database_url)
    with Session(engine) as session:
        rows = session.scalars(select(NumberExplanationORM)).all()
    assert rows == []


def test_net_revenue_explanation_idempotent_upsert_coexists_with_gross(tmp_path):
    """Assert repeated net explains upsert to one row that coexists
    with the adjusted_gross_revenue_usd snapshot.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    _post_net_explain(
        client,
        channel="channel-tv-a",
        month="2026-03",
        principal=_finance_month_net_principal(),
    )
    _post_net_explain(
        client,
        channel="channel-tv-a",
        month="2026-03",
        principal=_finance_month_net_principal(),
    )
    client.post(
        "/revenue/channels/channel-tv-a/months/2026-03/explain?metric=adjusted_gross_revenue_usd",
        headers=auth_headers("finance_viewer", scope_id=str(COMPANY_ID)),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        metrics = sorted(session.scalars(select(NumberExplanationORM.metric)).all())
    assert metrics == ["adjusted_gross_revenue_usd", "net_revenue_usd"]


CHANNEL_ALLOC_ROW_ID = UUID("00000000-0000-0000-0000-000000011303")


def _seed_account_allocated_channel(database_url: str) -> None:
    """Seed a missing-net ADSENSE channel mapped to one ACCOUNT deduction (pub-9).

    channel-alloc is in-scope with a 2026-03 ADSENSE gross 500 and no source net,
    so the account-allocated DEDUCTION (70.00, net-applicable) derives its net on the
    COMPONENT_DERIVED path -> the explanation builds the account_allocated_deduction_usd
    component. The Adsense->owner->channel links are VERIFIED with a source-aligned
    (ADSENSE) basis, so a month-wide commit fully allocates pub-9.
    """
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
                    id=CHANNEL_ALLOC_ROW_ID,
                    youtube_channel_id="channel-alloc",
                    channel_name="Alloc",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-alloc",
                    source_kind="ADSENSE",
                    source_report_id="adsense-report-2026-03",
                    gross_revenue_usd=Decimal("500.00"),
                    net_revenue_usd=None,
                    views=80000,
                    watch_time_minutes=Decimal("2000.00"),
                    confidence_score=Decimal("0.9600"),
                    imported_by=CREATOR_ID,
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
                    youtube_channel_id="channel-alloc",
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
                    source_key="k-9",
                    source_report_id=None,
                    raw_payload={"k": "v"},
                    component_key="srcrow:adsense_management:k-9",
                ),
            ]
        )
        session.commit()


def _commit_account_snapshot(database_url: str, month: str) -> None:
    """Commit one account-allocation snapshot for the month (creates an OPEN close row)."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        SqlAlchemyCommittedAllocationRepository(session).commit_allocation(
            month=month,
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


def _lock_month(database_url: str, month: str) -> None:
    """Mark a finance month LOCKED so readers prefer the committed snapshot.

    A committed snapshot already creates an OPEN close row, so update it in place
    rather than insert a second (which would violate the (tenant_id, month) UNIQUE).
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


def _persisted_account_component(database_url: str, channel: str, month: str) -> dict:
    """Read back the persisted net explanation's account_allocated_deduction_usd component."""
    engine = create_engine(database_url)
    with Session(engine) as session:
        row = session.scalars(
            select(NumberExplanationORM).where(
                NumberExplanationORM.metric == "net_revenue_usd",
                NumberExplanationORM.entity_id == channel,
                NumberExplanationORM.month == month,
            )
        ).one()
        components = list(row.components)
    try:
        return next(
            component
            for component in components
            if component["key"] == "account_allocated_deduction_usd"
        )
    except StopIteration as exc:
        raise ValueError("account_allocated_deduction_usd component not found") from exc


def test_net_explanation_locked_month_records_committed_snapshot_provenance(tmp_path):
    """A LOCKED month's persisted account component carries committed_snapshot provenance."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_account_allocated_channel(database_url)
    _commit_account_snapshot(database_url, "2026-03")
    _lock_month(database_url, "2026-03")
    client = TestClient(create_app(database_url=database_url))

    response = _post_net_explain(
        client,
        channel="channel-alloc",
        month="2026-03",
        principal=_finance_month_net_principal(),
    )

    assert response.status_code == 200
    account_component = _persisted_account_component(database_url, "channel-alloc", "2026-03")
    assert account_component["allocation_source"] == "committed_snapshot"
    assert account_component["committed_run"] is not None
    assert account_component["committed_run"]["commit_version"] == 1


def test_net_explanation_open_month_records_live_compute_provenance(tmp_path):
    """An OPEN month's persisted account component carries live_compute provenance."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    _seed_account_allocated_channel(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = _post_net_explain(
        client,
        channel="channel-alloc",
        month="2026-03",
        principal=_finance_month_net_principal(),
    )

    assert response.status_code == 200
    account_component = _persisted_account_component(database_url, "channel-alloc", "2026-03")
    assert account_component["allocation_source"] == "live_compute"
    assert account_component["committed_run"] is None
