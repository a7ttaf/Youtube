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
    FinanceBase,
    MonthlyChannelRevenueFactORM,
    RevenueManualOverrideORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

SECTOR_ID = UUID("00000000-0000-0000-0000-000000011101")
COMPANY_ID = UUID("00000000-0000-0000-0000-000000011201")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-000000011301")
FINANCE_USER_ID = UUID("00000000-0000-0000-0000-000000011401")
CREATOR_ID = UUID("00000000-0000-0000-0000-000000011402")
APPROVER_ID = UUID("00000000-0000-0000-0000-000000011403")


def auth_headers(
    role: str, user_id: UUID = FINANCE_USER_ID, scope_id: str | None = None
) -> dict[str, str]:
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
    return f"sqlite+pysqlite:///{(tmp_path / 'revenue-explanations.db').as_posix()}"


def seed_database(database_url: str) -> None:
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    ExplanationBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                OrgUnitORM(
                    id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV", active=True
                ),
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
    assert response.json()["metric"] == "adjusted_gross_revenue_usd"
    assert response.json()["value"] == "1125.5"
    assert response.json()["currency"] == "USD"
    assert (
        response.json()["formula"]
        == "baseline_gross_revenue_usd + approved_manual_override_total_usd"
    )
    assert response.json()["confidence"]["label"] == "HIGH"
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
            "message": (
                "1 pending manual override is not included in "
                "adjusted_gross_revenue_usd."
            ),
        }
    ]
    assert response.json()["audit_event"]["event_type"] == "REVENUE_VIEWED"
    assert explanation.value == Decimal("1125.50")
    assert explanation.metric == "adjusted_gross_revenue_usd"
    assert audit_log.event_type == "REVENUE_VIEWED"
    assert audit_log.sensitive is True


def test_assistant_cannot_get_revenue_explanation(tmp_path):
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
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/channels/channel-tv-a/months/2026-03/explain?metric=bogus_metric",
        headers=auth_headers("finance_viewer", scope_id=str(COMPANY_ID)),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Unsupported explanation metric: bogus_metric"


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
            f"/revenue/channels/{channel}/months/{month}/explain"
            "?metric=net_revenue_usd",
        )
    finally:
        client.app.dependency_overrides.pop(current_principal_from_headers, None)


def test_net_revenue_explanation_global_finance_returns_value_provenance_and_plural_audit(
    tmp_path,
):
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
            select(NumberExplanationORM).where(
                NumberExplanationORM.metric == "net_revenue_usd"
            )
        ).all()
    assert len(rows) == 1


def test_net_revenue_explanation_org_scoped_finance_viewer_403_on_net_200_on_gross(
    tmp_path,
):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("finance_viewer", scope_id=str(COMPANY_ID))

    net = client.post(
        "/revenue/channels/channel-tv-a/months/2026-03/explain?metric=net_revenue_usd",
        headers=headers,
    )
    gross = client.post(
        "/revenue/channels/channel-tv-a/months/2026-03/explain"
        "?metric=adjusted_gross_revenue_usd",
        headers=headers,
    )
    assert net.status_code == 403
    assert gross.status_code == 200


def test_net_revenue_explanation_no_facts_channel_returns_422(tmp_path):
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
        "/revenue/channels/channel-tv-a/months/2026-03/explain"
        "?metric=adjusted_gross_revenue_usd",
        headers=auth_headers("finance_viewer", scope_id=str(COMPANY_ID)),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        metrics = sorted(session.scalars(select(NumberExplanationORM.metric)).all())
    assert metrics == ["adjusted_gross_revenue_usd", "net_revenue_usd"]
