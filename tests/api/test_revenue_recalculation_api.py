from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import (
    FinanceBase,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

SECTOR_ID = UUID("00000000-0000-0000-0000-00000000d101")
COMPANY_ID = UUID("00000000-0000-0000-0000-00000000d201")
OTHER_COMPANY_ID = UUID("00000000-0000-0000-0000-00000000d202")
CHANNEL_A_ROW_ID = UUID("00000000-0000-0000-0000-00000000d301")
CHANNEL_B_ROW_ID = UUID("00000000-0000-0000-0000-00000000d302")
OTHER_CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-00000000d303")
USER_ID = UUID("00000000-0000-0000-0000-00000000d401")


def auth_headers(
    role: str,
    scope_type: str = "global",
    scope_id: str | None = None,
) -> dict[str, str]:
    """Return request headers for the given role and optional scope."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "recalculation@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    """Return a unique SQLite URL under pytest's tmp_path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed_database(database_url: str) -> None:
    """Seed org/security/finance rows for recalculation tests."""
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
                OrgUnitORM(
                    id=OTHER_COMPANY_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="News Company",
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
                YouTubeChannelORM(
                    id=OTHER_CHANNEL_ROW_ID,
                    youtube_channel_id="channel-news-a",
                    channel_name="News A",
                    primary_org_unit_id=OTHER_COMPANY_ID,
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
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-news-a",
                    source_kind="YOUTUBE_CMS",
                    source_report_id="cms-report-2026-03",
                    gross_revenue_usd=Decimal("900.00"),
                    net_revenue_usd=Decimal("770.00"),
                    views=150000,
                    watch_time_minutes=Decimal("4100.00"),
                    confidence_score=Decimal("0.9700"),
                    imported_by=USER_ID,
                ),
                UserORM(
                    id=USER_ID,
                    email="recalculation@example.com",
                    display_name="Recalculation User",
                ),
            ]
        )
        session.commit()


def recalculation_payload(**overrides) -> dict[str, object]:
    """Return a default recalculation request payload with optional overrides."""
    payload: dict[str, object] = {
        "month": "2026-03",
        "scope_type": "company",
        "scope_id": str(COMPANY_ID),
        "allocation_method": "gross_revenue_proportional",
        "currency": "USD",
        "dry_run": True,
        "reason": "Preview March transfer-fee allocation before close",
    }
    payload.update(overrides)
    return payload


def test_finance_admin_requests_recalculation_preview_with_audit(tmp_path):
    """finance_admin dry-run returns 200 + READY_FOR_REVIEW and writes one audit row."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/recalculate",
        headers=auth_headers("finance_admin", "global"),
        json=recalculation_payload(),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    body = response.json()
    assert body["month"] == "2026-03"
    assert body["allocation_method"] == "gross_revenue_proportional"
    assert body["scope"] == {"scope_type": "company", "scope_id": str(COMPANY_ID)}
    assert body["currency"] == "USD"
    assert body["dry_run"] is True
    assert body["write_status"] == "NO_WRITES_PERFORMED"
    assert body["status"] == "READY_FOR_REVIEW"
    assert body["source_summary"] == {
        "revenue_fact_count": 2,
        "source_channel_count": 2,
        "manual_override_count": 0,
        "pending_manual_override_count": 0,
        "net_revenue_source_count": 1,
        "missing_net_revenue_source_count": 1,
    }
    assert body["blocking_issues"] == []
    assert body["audit_event"]["event_type"] == "RECALCULATION_REQUESTED"
    assert body["audit_event"]["sensitive"] is True
    assert audit_log.event_type == "RECALCULATION_REQUESTED"
    assert audit_log.reason == "Preview March transfer-fee allocation before close"
    assert audit_log.scope_type == "finance-month"
    assert audit_log.scope_id == "2026-03"
    assert audit_log.sensitive is True


def test_finance_viewer_cannot_request_recalculation(tmp_path):
    """finance_viewer is rejected with 403."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/recalculate",
        headers=auth_headers("finance_viewer", "global"),
        json=recalculation_payload(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Missing permission: finance.change_allocation_rule"
    )


def test_recalculation_rejects_committed_writes_until_engine_is_enabled(tmp_path):
    """dry_run=False raises 422 before any audit write."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/recalculate",
        headers=auth_headers("finance_admin", "global"),
        json=recalculation_payload(dry_run=False),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_count = session.execute(
            text("SELECT COUNT(*) FROM audit_logs")
        ).scalar_one()

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "committed recalculation writes are not implemented; use dry_run=true"
    )
    assert audit_count == 0


def test_recalculation_rejects_unknown_allocation_method(tmp_path):
    """An unknown allocation method is rejected with 422."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/recalculate",
        headers=auth_headers("finance_admin", "global"),
        json=recalculation_payload(allocation_method="magic_estimate"),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "Unknown allocation_method: magic_estimate. "
        "Allowed methods: company_level, gross_revenue_proportional, manual, "
        "no_allocation, post_tax_revenue_proportional"
    )


def _seed_two_source_kinds(database_url: str) -> None:
    """One channel with YOUTUBE_CMS net=880 and ADSENSE net=None (grain isolation)."""
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            OrgUnitORM(id=SECTOR_ID, parent_id=None, type="SECTOR", name="TV", active=True),
            OrgUnitORM(id=COMPANY_ID, parent_id=SECTOR_ID, type="COMPANY",
                       name="TV Company", active=True),
            YouTubeChannelORM(
                id=CHANNEL_A_ROW_ID, youtube_channel_id="channel-tv-a", channel_name="TV A",
                primary_org_unit_id=COMPANY_ID, cms_status="INSIDE_CMS",
                revenue_required=True, active=True,
            ),
            MonthlyChannelRevenueFactORM(
                id=uuid4(), month="2026-03", youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS", source_report_id="cms-1",
                gross_revenue_usd=Decimal("1000.00"), net_revenue_usd=Decimal("880.00"),
                views=1, watch_time_minutes=Decimal("1"),
                confidence_score=Decimal("0.95"), imported_by=USER_ID,
            ),
            MonthlyChannelRevenueFactORM(
                id=uuid4(), month="2026-03", youtube_channel_id="channel-tv-a",
                source_kind="ADSENSE", source_report_id="ad-1",
                gross_revenue_usd=Decimal("500.00"), net_revenue_usd=None,
                views=1, watch_time_minutes=Decimal("1"),
                confidence_score=Decimal("0.95"), imported_by=USER_ID,
            ),
            UserORM(id=USER_ID, email="recalculation@example.com",
                    display_name="Recalculation User"),
        ])
        session.commit()


def test_post_tax_dry_run_blocks_on_missing_source_kind_net(tmp_path):
    """A channel with net for YOUTUBE_CMS but null net for ADSENSE blocks post_tax
    under the (channel, source_kind) grain (channel grain would have passed).
    """
    database_url = build_database_url(tmp_path)
    _seed_two_source_kinds(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.post(
        "/revenue/recalculate",
        headers=auth_headers("finance_admin", "global"),
        json=recalculation_payload(
            scope_type="global", scope_id=None,
            allocation_method="post_tax_revenue_proportional",
        ),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "BLOCKED"
    assert any(
        i["issue_type"] == "NET_REVENUE_SOURCE_MISSING"
        for i in body["blocking_issues"]
    )
    assert body["source_summary"]["net_revenue_source_count"] == 1
    assert body["source_summary"]["missing_net_revenue_source_count"] == 1
