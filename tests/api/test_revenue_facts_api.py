from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import (
    FinanceBase,
    FinanceMonthCloseORM,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

SECTOR_ID = UUID("00000000-0000-0000-0000-000000006101")
COMPANY_ID = UUID("00000000-0000-0000-0000-000000006201")
OTHER_COMPANY_ID = UUID("00000000-0000-0000-0000-000000006202")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-000000006301")
OTHER_CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-000000006302")
USER_ID = UUID("00000000-0000-0000-0000-000000006401")


def auth_headers(
    role: str, scope_type: str, scope_id: str | None = None
) -> dict[str, str]:
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "revenue-facts@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed_database(database_url: str, *, locked_month: bool = False) -> None:
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
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
                OrgUnitORM(
                    id=OTHER_COMPANY_ID,
                    parent_id=SECTOR_ID,
                    type="COMPANY",
                    name="News Company",
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
                YouTubeChannelORM(
                    id=OTHER_CHANNEL_ROW_ID,
                    youtube_channel_id="channel-news-a",
                    channel_name="News A",
                    primary_org_unit_id=OTHER_COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                UserORM(
                    id=USER_ID,
                    email="revenue-facts@example.com",
                    display_name="Revenue Facts User",
                ),
            ]
        )
        if locked_month:
            session.add(
                FinanceMonthCloseORM(
                    month="2026-03", status="LOCKED", locked_by=USER_ID
                )
            )
        session.commit()


def test_system_integration_user_imports_monthly_revenue_fact_with_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/facts",
        headers=auth_headers("system_integration_user", "connector", "youtube-cms"),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "source_kind": "YOUTUBE_CMS",
            "connector_key": "youtube-cms",
            "source_report_id": "cms-report-2026-03",
            "gross_revenue_usd": "1234.56",
            "net_revenue_usd": "987.65",
            "views": 250000,
            "watch_time_minutes": "7200.50",
            "confidence_score": "0.9825",
            "reason": "Imported official CMS revenue for March close",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        fact = session.scalars(select(MonthlyChannelRevenueFactORM)).one()
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 201
    assert response.json()["month"] == "2026-03"
    assert response.json()["youtube_channel_id"] == "channel-tv-a"
    assert response.json()["gross_revenue_usd"] == "1234.56"
    assert response.json()["audit_event"]["event_type"] == "REPORT_IMPORTED"
    assert response.json()["audit_event"]["sensitive"] is True
    assert fact.imported_by == USER_ID
    assert fact.gross_revenue_usd == Decimal("1234.56")
    assert audit_log.entity_id == "channel-tv-a:2026-03:YOUTUBE_CMS"
    assert audit_log.sensitive is True


def test_import_rejects_connector_source_kind_mismatch(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/facts",
        headers=auth_headers("system_integration_user", "connector", "youtube-cms"),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "source_kind": "ADSENSE",
            "connector_key": "youtube-cms",
            "gross_revenue_usd": "1234.56",
            "views": 250000,
            "reason": "Attempt import under mismatched source provenance",
        },
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "connector_key youtube-cms cannot import source_kind ADSENSE"
    )


def test_finance_viewer_reads_channel_month_facts_with_revenue_audit(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            MonthlyChannelRevenueFactORM(
                id=uuid4(),
                month="2026-03",
                youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS",
                source_report_id="cms-report-2026-03",
                gross_revenue_usd=Decimal("1234.56"),
                net_revenue_usd=Decimal("987.65"),
                views=250000,
                watch_time_minutes=Decimal("7200.50"),
                confidence_score=Decimal("0.9825"),
                imported_by=USER_ID,
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/channels/channel-tv-a/months/2026-03/facts",
        headers=auth_headers("finance_viewer", "company", str(COMPANY_ID)),
    )

    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert response.json()["month"] == "2026-03"
    assert response.json()["youtube_channel_id"] == "channel-tv-a"
    assert response.json()["facts"][0]["net_revenue_usd"] == "987.65"
    assert response.json()["audit_event"]["event_type"] == "REVENUE_VIEWED"
    assert audit_log.event_type == "REVENUE_VIEWED"
    assert audit_log.sensitive is True


def test_company_manager_cannot_read_revenue_facts(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/channels/channel-tv-a/months/2026-03/facts",
        headers=auth_headers("company_manager", "company", str(COMPANY_ID)),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_finance_viewer_reads_reconciliation_preview(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all(
            [
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
                    imported_by=USER_ID,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    source_kind="ADSENSE",
                    source_report_id="adsense-report-2026-03",
                    gross_revenue_usd=Decimal("930.00"),
                    net_revenue_usd=Decimal("880.00"),
                    views=0,
                    watch_time_minutes=Decimal("0"),
                    confidence_score=Decimal("0.9000"),
                    imported_by=USER_ID,
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/channels/channel-tv-a/months/2026-03/reconciliation-preview",
        headers=auth_headers("finance_viewer", "company", str(COMPANY_ID)),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "VARIANCE_DETECTED"
    assert response.json()["gross_revenue_variance_usd"] == "70"
    assert response.json()["issues"][0]["issue_type"] == "GROSS_REVENUE_VARIANCE"


def test_company_manager_cannot_read_reconciliation_preview(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/channels/channel-tv-a/months/2026-03/reconciliation-preview",
        headers=auth_headers("company_manager", "company", str(COMPANY_ID)),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_finance_viewer_reads_month_reconciliation_issue_queue_for_allowed_company(
    tmp_path,
):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all(
            [
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    source_kind="YOUTUBE_CMS",
                    gross_revenue_usd=Decimal("1000.00"),
                    views=250000,
                    watch_time_minutes=Decimal("7200.50"),
                    confidence_score=Decimal("0.9800"),
                    imported_by=USER_ID,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    source_kind="ADSENSE",
                    gross_revenue_usd=Decimal("930.00"),
                    views=0,
                    watch_time_minutes=Decimal("0"),
                    confidence_score=Decimal("0.9000"),
                    imported_by=USER_ID,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-news-a",
                    source_kind="YOUTUBE_CMS",
                    gross_revenue_usd=Decimal("2000.00"),
                    views=300000,
                    watch_time_minutes=Decimal("8200.50"),
                    confidence_score=Decimal("0.9800"),
                    imported_by=USER_ID,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-news-a",
                    source_kind="ADSENSE",
                    gross_revenue_usd=Decimal("1500.00"),
                    views=0,
                    watch_time_minutes=Decimal("0"),
                    confidence_score=Decimal("0.9000"),
                    imported_by=USER_ID,
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/reconciliation-issues",
        headers=auth_headers("finance_viewer", "company", str(COMPANY_ID)),
    )

    with Session(engine) as session:
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 200
    assert response.json()["month"] == "2026-03"
    assert response.json()["issue_count"] == 1
    assert [item["youtube_channel_id"] for item in response.json()["items"]] == [
        "channel-tv-a"
    ]
    assert response.json()["pagination"] == {
        "limit": 100,
        "offset": 0,
        "next_offset": None,
        "has_more": False,
    }
    assert audit_log.entity_type == "revenue_reconciliation_issue_queue"
    assert audit_log.sensitive is True


def test_finance_viewer_pages_month_reconciliation_issue_queue_by_channel(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all(
            [
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    source_kind="YOUTUBE_CMS",
                    gross_revenue_usd=Decimal("1000.00"),
                    views=250000,
                    watch_time_minutes=Decimal("7200.50"),
                    confidence_score=Decimal("0.9800"),
                    imported_by=USER_ID,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-tv-a",
                    source_kind="ADSENSE",
                    gross_revenue_usd=Decimal("930.00"),
                    views=0,
                    watch_time_minutes=Decimal("0"),
                    confidence_score=Decimal("0.9000"),
                    imported_by=USER_ID,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-news-a",
                    source_kind="YOUTUBE_CMS",
                    gross_revenue_usd=Decimal("2000.00"),
                    views=300000,
                    watch_time_minutes=Decimal("8200.50"),
                    confidence_score=Decimal("0.9800"),
                    imported_by=USER_ID,
                ),
                MonthlyChannelRevenueFactORM(
                    id=uuid4(),
                    month="2026-03",
                    youtube_channel_id="channel-news-a",
                    source_kind="ADSENSE",
                    gross_revenue_usd=Decimal("1500.00"),
                    views=0,
                    watch_time_minutes=Decimal("0"),
                    confidence_score=Decimal("0.9000"),
                    imported_by=USER_ID,
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    first_page = client.get(
        "/revenue/months/2026-03/reconciliation-issues?limit=1&offset=0",
        headers=auth_headers("finance_viewer", "sector", str(SECTOR_ID)),
    )
    second_page = client.get(
        "/revenue/months/2026-03/reconciliation-issues?limit=1&offset=1",
        headers=auth_headers("finance_viewer", "sector", str(SECTOR_ID)),
    )

    assert first_page.status_code == 200
    assert [item["youtube_channel_id"] for item in first_page.json()["items"]] == [
        "channel-news-a"
    ]
    assert first_page.json()["pagination"] == {
        "limit": 1,
        "offset": 0,
        "next_offset": 1,
        "has_more": True,
    }
    assert second_page.status_code == 200
    assert [item["youtube_channel_id"] for item in second_page.json()["items"]] == [
        "channel-tv-a"
    ]
    assert second_page.json()["pagination"] == {
        "limit": 1,
        "offset": 1,
        "next_offset": None,
        "has_more": False,
    }


def test_company_manager_cannot_read_month_reconciliation_issue_queue(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/reconciliation-issues",
        headers=auth_headers("company_manager", "company", str(COMPANY_ID)),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_import_rejects_locked_finance_month(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url, locked_month=True)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/facts",
        headers=auth_headers("system_integration_user", "connector", "youtube-cms"),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "source_kind": "YOUTUBE_CMS",
            "connector_key": "youtube-cms",
            "gross_revenue_usd": "1234.56",
            "views": 250000,
            "reason": "Attempt import after month lock",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        facts = session.scalars(select(MonthlyChannelRevenueFactORM)).all()

    assert response.status_code == 409
    assert (
        response.json()["detail"] == "Finance month is locked for revenue fact imports"
    )
    assert facts == []


def test_import_rejects_missing_channel(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/facts",
        headers=auth_headers("system_integration_user", "connector", "youtube-cms"),
        json={
            "month": "2026-03",
            "youtube_channel_id": "missing-channel",
            "source_kind": "YOUTUBE_CMS",
            "connector_key": "youtube-cms",
            "gross_revenue_usd": "1234.56",
            "views": 250000,
            "reason": "Attempt import for missing channel",
        },
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "youtube_channel_id must reference an active channel"
    )


def test_import_rejects_invalid_source_kind(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/facts",
        headers=auth_headers("system_integration_user", "connector", "youtube-cms"),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "source_kind": "ESTIMATED_FAKE_SOURCE",
            "connector_key": "youtube-cms",
            "gross_revenue_usd": "1234.56",
            "views": 250000,
            "reason": "Attempt import with invalid source kind",
        },
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"]
        == "Unknown revenue fact source_kind: ESTIMATED_FAKE_SOURCE"
    )


def test_import_rejects_malformed_actor_id(tmp_path):
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("system_integration_user", "connector", "youtube-cms")
    headers["x-user-id"] = "not-a-uuid"

    response = client.post(
        "/revenue/facts",
        headers=headers,
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "source_kind": "YOUTUBE_CMS",
            "connector_key": "youtube-cms",
            "gross_revenue_usd": "1234.56",
            "views": 250000,
            "reason": "Attempt import with malformed actor id",
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "actor_user_id must be a valid UUID"
