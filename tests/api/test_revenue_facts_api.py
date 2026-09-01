from decimal import Decimal
from uuid import UUID, uuid4

import pytest
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


def auth_headers(role: str, scope_type: str, scope_id: str | None = None) -> dict[str, str]:
    """Build trust-gateway auth headers for the given role, scope type, and optional scope id."""
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
    """Return the SQLite URL for an isolated per-test revenue facts database under tmp_path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed_database(database_url: str, *, locked_month: bool = False) -> None:
    """Create schema tables and seed org units, channels, and the test user for revenue tests."""
    engine = create_engine(database_url)
    OrgBase.metadata.create_all(engine)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
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
            session.add(FinanceMonthCloseORM(month="2026-03", status="LOCKED", locked_by=USER_ID))
        session.commit()


def test_system_integration_user_imports_monthly_revenue_fact_with_audit(tmp_path):
    """A connector-scoped system user imports a fact and a sensitive REPORT_IMPORTED audit row."""
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
            "shorts_revenue_usd": "234.56",
            "longform_revenue_usd": "900.00",
            "subscription_revenue_usd": "50.00",
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
    assert response.json()["shorts_revenue_usd"] == "234.56"
    assert response.json()["longform_revenue_usd"] == "900"
    assert response.json()["subscription_revenue_usd"] == "50"
    assert response.json()["audit_event"]["event_type"] == "REPORT_IMPORTED"
    assert response.json()["audit_event"]["sensitive"] is True
    assert fact.imported_by == USER_ID
    assert fact.gross_revenue_usd == Decimal("1234.56")
    assert fact.shorts_revenue_usd == Decimal("234.56")
    assert fact.longform_revenue_usd == Decimal("900.00")
    assert fact.subscription_revenue_usd == Decimal("50.00")
    assert audit_log.entity_id == "channel-tv-a:2026-03:YOUTUBE_CMS"
    assert audit_log.details["shorts_revenue_usd"] == "234.56"
    assert audit_log.sensitive is True


@pytest.mark.parametrize("manual_connector_key", ["manual-upload", "manual_upload"])
def test_beta_operator_imports_only_manual_revenue_without_connector_power(
    tmp_path,
    manual_connector_key,
):
    """The beta workflow writes MANUAL_UPLOAD facts under its narrow audit permission."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/facts",
        headers=auth_headers("beta_operator", "global"),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "source_kind": "MANUAL_UPLOAD",
            "connector_key": manual_connector_key,
            "source_report_id": "operator-upload-2026-03",
            "gross_revenue_usd": "1234.56",
            "views": 250000,
            "confidence_score": "0.95",
            "reason": "Google-free beta revenue upload",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        fact = session.scalars(select(MonthlyChannelRevenueFactORM)).one()
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 201, response.text
    assert fact.source_kind == "MANUAL_UPLOAD"
    assert audit_log.details["permission"] == "finance.import_manual_revenue"
    assert audit_log.details["connector_key"] == manual_connector_key
    assert audit_log.scope_type == "connector"
    assert audit_log.scope_id == manual_connector_key


@pytest.mark.parametrize("manual_connector_key", ["manual-upload", "manual_upload"])
def test_beta_operator_manual_alias_with_non_manual_source_fails_closed(
    tmp_path,
    manual_connector_key,
):
    """Both aliases require MANUAL_UPLOAD; neither grants generic connector power."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/facts",
        headers=auth_headers("beta_operator", "global"),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "source_kind": "YOUTUBE_CMS",
            "connector_key": manual_connector_key,
            "gross_revenue_usd": "1234.56",
            "reason": "Attempt non-manual import through manual alias",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        assert session.scalars(select(MonthlyChannelRevenueFactORM)).all() == []
        assert session.scalars(select(AuditLogORM)).all() == []

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: connectors.run_jobs"


def test_beta_operator_cannot_import_connector_sourced_revenue(tmp_path):
    """The narrow grant is not an alternate connector-ingestion boundary."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/facts",
        headers=auth_headers("beta_operator", "global"),
        json={
            "month": "2026-03",
            "youtube_channel_id": "channel-tv-a",
            "source_kind": "YOUTUBE_CMS",
            "connector_key": "youtube-cms",
            "gross_revenue_usd": "1234.56",
            "reason": "Attempt connector-backed import",
        },
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        assert session.scalars(select(MonthlyChannelRevenueFactORM)).all() == []
        assert session.scalars(select(AuditLogORM)).all() == []

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: connectors.run_jobs"


def test_import_rejects_connector_source_kind_mismatch(tmp_path):
    """Import is rejected with 422 when the source kind does not match the connector key."""
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
        response.json()["detail"] == "connector_key youtube-cms cannot import source_kind ADSENSE"
    )


def test_finance_viewer_reads_channel_month_facts_with_revenue_audit(tmp_path):
    """Finance viewers read channel-month facts and trigger a sensitive REVENUE_VIEWED audit."""
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
    assert response.json()["facts"][0]["shorts_revenue_usd"] is None
    assert response.json()["audit_event"]["event_type"] == "REVENUE_VIEWED"
    assert audit_log.event_type == "REVENUE_VIEWED"
    assert audit_log.sensitive is True


def test_import_rejects_revenue_breakdown_above_gross(tmp_path):
    """Import fails with 422 and persists nothing when the breakdown exceeds gross revenue."""
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
            "gross_revenue_usd": "100.00",
            "shorts_revenue_usd": "80.00",
            "longform_revenue_usd": "40.00",
            "reason": "Reject inconsistent official revenue breakdown",
        },
    )

    assert response.status_code == 422
    assert (
        response.json()["detail"] == "revenue format breakdown total must be <= gross_revenue_usd"
    )
    engine = create_engine(database_url)
    with Session(engine) as session:
        facts = session.scalars(
            select(MonthlyChannelRevenueFactORM).where(
                MonthlyChannelRevenueFactORM.month == "2026-03",
                MonthlyChannelRevenueFactORM.youtube_channel_id == "channel-tv-a",
                MonthlyChannelRevenueFactORM.source_kind == "YOUTUBE_CMS",
            )
        ).all()
    assert facts == []


def test_company_manager_cannot_read_revenue_facts(tmp_path):
    """A company manager is denied revenue facts with 403 for missing finance.view_revenue."""
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
    """A finance viewer's preview flags VARIANCE_DETECTED with a GROSS_REVENUE_VARIANCE issue."""
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
    """A company manager cannot read the reconciliation preview without finance.view_revenue."""
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
    """The monthly queue returns only the viewer's company issues and audits the sensitive read."""
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
    assert [item["youtube_channel_id"] for item in response.json()["items"]] == ["channel-tv-a"]
    assert response.json()["pagination"] == {
        "limit": 100,
        "offset": 0,
        "next_offset": None,
        "has_more": False,
    }
    assert audit_log.entity_type == "revenue_reconciliation_issue_queue"
    assert audit_log.sensitive is True


def test_finance_viewer_pages_month_reconciliation_issue_queue_by_channel(tmp_path):
    """The issue queue pages by channel with next_offset and has_more across sector scope."""
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
    assert [item["youtube_channel_id"] for item in first_page.json()["items"]] == ["channel-news-a"]
    assert first_page.json()["pagination"] == {
        "limit": 1,
        "offset": 0,
        "next_offset": 1,
        "has_more": True,
    }
    assert second_page.status_code == 200
    assert [item["youtube_channel_id"] for item in second_page.json()["items"]] == ["channel-tv-a"]
    assert second_page.json()["pagination"] == {
        "limit": 1,
        "offset": 1,
        "next_offset": None,
        "has_more": False,
    }


def test_company_manager_cannot_read_month_reconciliation_issue_queue(tmp_path):
    """A company manager is denied the monthly issue queue without finance.view_revenue."""
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
    """Import into a locked finance month fails with 409 and persists no fact."""
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
    assert response.json()["detail"] == "Finance month is locked for revenue fact imports"
    assert facts == []


def test_import_rejects_missing_channel(tmp_path):
    """Import for an unknown channel is rejected because it must reference an active channel."""
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
    assert response.json()["detail"] == "youtube_channel_id must reference an active channel"


def test_import_rejects_invalid_source_kind(tmp_path):
    """Import is rejected with 422 when the source_kind is not a known revenue source."""
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
    assert response.json()["detail"] == "Unknown revenue fact source_kind: ESTIMATED_FAKE_SOURCE"


def test_import_accepts_gateway_subject_actor_id(tmp_path):
    """A non-UUID gateway subject maps to a deterministic uuid5 and is persisted as imported_by."""
    # Header-auth deployments can deliver non-UUID gateway subjects via
    # x-user-id (e.g. service-account slugs). The revenue-fact repository
    # used to reject these with 422; per the shared actor_identity_uuid
    # helper the subject is now mapped to a deterministic uuid5 and the
    # write succeeds with that value persisted to imported_by.
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    headers = auth_headers("system_integration_user", "connector", "youtube-cms")
    headers["x-user-id"] = "gateway-service-account"

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
            "reason": "Accept gateway non-UUID actor for revenue facts",
        },
    )

    assert response.status_code == 201
    from ums_smart_revenue.auth.actor_identity import actor_identity_uuid

    expected_actor_uuid = actor_identity_uuid("gateway-service-account")
    engine = create_engine(database_url)
    with Session(engine) as session:
        fact = session.scalars(
            select(MonthlyChannelRevenueFactORM).where(
                MonthlyChannelRevenueFactORM.month == "2026-03",
                MonthlyChannelRevenueFactORM.youtube_channel_id == "channel-tv-a",
                MonthlyChannelRevenueFactORM.source_kind == "YOUTUBE_CMS",
            )
        ).one()
    assert fact.imported_by == expected_actor_uuid
