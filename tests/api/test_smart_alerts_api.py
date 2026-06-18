"""Tests for the Smart Alerts API authorization and database behavior."""

from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.revenue import _previous_month
from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import (
    AdSensePaymentORM,
    FinanceBase,
    MonthlyChannelRevenueFactORM,
    RevenueManualOverrideORM,
)
from ums_smart_revenue.db.org_models import OrgBase, OrgUnitORM, YouTubeChannelORM
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.finance.revenue_facts import RevenueFactValidationError
from ums_smart_revenue.tenancy.constants import UMS_TENANT_ID

SECTOR_ID = UUID("00000000-0000-0000-0000-00000000b101")
COMPANY_ID = UUID("00000000-0000-0000-0000-00000000b201")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-00000000b301")
USER_ID = UUID("00000000-0000-0000-0000-00000000b401")
APPROVER_ID = UUID("00000000-0000-0000-0000-00000000b402")


def auth_headers(
    role: str,
    scope_type: str = "global",
    scope_id: str | None = None,
) -> dict[str, str]:
    """
    Generate authentication headers for test requests.

    Args:
        role: The user role to include in the headers.
        scope_type: The scope type for the headers (default is 'global').
        scope_id: Optional scope identifier to include.

    Returns:
        A dictionary of HTTP headers containing user identity,
        role, scope, and a trusted gateway token.
    """
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "smart-alerts@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    """
    Construct a temporary SQLite database URL for testing.

    Args:
        tmp_path: A pathlib Path object pointing to a temporary directory.

    Returns:
        A database URL string for a new SQLite database file.
    """
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed_database(database_url: str) -> None:
    """
    Initialize and seed the database with organization, security, and finance models.

    Args:
        database_url: The database connection URL where tables will be created and seeded.
    """
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
                    id=CHANNEL_ROW_ID,
                    youtube_channel_id="channel-tv-a",
                    channel_name="TV A",
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
                    net_revenue_usd=Decimal("900.00"),
                    views=250000,
                    watch_time_minutes=Decimal("7200.50"),
                    confidence_score=Decimal("0.9825"),
                    imported_by=USER_ID,
                ),
                AdSensePaymentORM(
                    id=uuid4(),
                    month="2026-03",
                    payment_name="AdSense payment March 2026",
                    payment_date=date(2026, 4, 21),
                    payment_amount=Decimal("900.00"),
                    payment_currency="USD",
                    payment_status="PAID",
                    raw_payload={"paymentId": "pay_2026_03"},
                    source_report_id="adsense-payment-2026-03",
                    source_account_id="pub-1",
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
                    email="smart-alerts@example.com",
                    display_name="Smart Alerts User",
                ),
            ]
        )
        session.commit()


def test_finance_viewer_reads_month_smart_alerts_with_sensitive_audits(tmp_path):
    """Test that finance viewers can read month smart alerts and see sensitive audit events."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_viewer", "global"),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_logs = session.scalars(select(AuditLogORM).order_by(AuditLogORM.event_type)).all()

    assert response.status_code == 200
    assert response.json()["status"] == "ATTENTION_REQUIRED"
    assert [alert["code"] for alert in response.json()["alerts"]] == [
        "PAYMENT_NOT_MATCHED",
        "BANK_AMOUNT_MISSING",
        "UNEXPLAINED_GAP_HIGH",
        "MONTH_NOT_LOCKED",
        "MANUAL_OVERRIDE_USED",
    ]
    assert [event["event_type"] for event in response.json()["audit_events"]] == [
        "REVENUE_VIEWED",
        "PAYMENT_VIEWED",
        "BANK_RECONCILIATION_VIEWED",
    ]
    assert [log.event_type for log in audit_logs] == [
        "BANK_RECONCILIATION_VIEWED",
        "PAYMENT_VIEWED",
        "REVENUE_VIEWED",
    ]
    assert all(log.sensitive is True for log in audit_logs)


def test_month_smart_alerts_reject_non_padded_month(tmp_path):
    """Test that smart alerts endpoint rejects months not zero-padded in the URL path."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-3/smart-alerts",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "month must use YYYY-MM with a calendar month from 01 to 12"


def test_previous_month_rejects_non_padded_month():
    """Test that the helper for computing the previous month rejects non-padded month strings."""
    with pytest.raises(
        RevenueFactValidationError,
        match="month must use YYYY-MM with a calendar month from 01 to 12",
    ):
        _previous_month("2026-3")


def test_month_smart_alerts_include_month_over_month_revenue_anomaly(tmp_path):
    """Test that month smart alerts include anomalies based on month-over-month revenue changes."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        current_fact = session.scalars(
            select(MonthlyChannelRevenueFactORM).where(
                MonthlyChannelRevenueFactORM.month == "2026-03"
            )
        ).one()
        current_fact.gross_revenue_usd = Decimal("900.00")
        session.add(
            MonthlyChannelRevenueFactORM(
                id=uuid4(),
                month="2026-02",
                youtube_channel_id="channel-tv-a",
                source_kind="YOUTUBE_CMS",
                source_report_id="cms-report-2026-02",
                gross_revenue_usd=Decimal("2000.00"),
                net_revenue_usd=Decimal("1800.00"),
                views=300000,
                watch_time_minutes=Decimal("9200.50"),
                confidence_score=Decimal("0.9825"),
                imported_by=USER_ID,
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 200
    anomaly = next(
        (alert for alert in response.json()["alerts"] if alert["code"] == "REVENUE_TREND_ANOMALY"),
        None,
    )
    assert anomaly is not None
    assert anomaly["details"]["channels"] == [
        {
            "youtube_channel_id": "channel-tv-a",
            "current_gross_revenue_usd": "900",
            "previous_gross_revenue_usd": "2000",
            "change_percent": "-55",
        }
    ]


def test_month_smart_alerts_flag_channel_missing_revenue_facts(tmp_path):
    """A 2nd active revenue-required channel with no fact surfaces the coverage alert."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add(
            YouTubeChannelORM(
                id=UUID("00000000-0000-0000-0000-00000000b302"),
                youtube_channel_id="channel-tv-b",
                channel_name="TV B",
                primary_org_unit_id=COMPANY_ID,
                cms_status="INSIDE_CMS",
                revenue_required=True,
                active=True,
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 200
    coverage = next(
        (
            alert
            for alert in response.json()["alerts"]
            if alert["code"] == "CHANNELS_MISSING_REVENUE_FACTS"
        ),
        None,
    )
    assert coverage is not None
    assert coverage["severity"] == "HIGH"
    assert coverage["confidence"] == "E_MISSING"
    assert coverage["details"] == {
        "channel_count": 1,
        "sample_channel_ids": ["channel-tv-b"],
    }
    # The pre-existing audit shape stays stable (no new audit events).
    assert [event["event_type"] for event in response.json()["audit_events"]] == [
        "REVENUE_VIEWED",
        "PAYMENT_VIEWED",
        "BANK_RECONCILIATION_VIEWED",
    ]


def test_month_smart_alerts_surface_skipped_source_row_audit_edges(tmp_path):
    """ROWS_SKIPPED connector audit edges become a finance dashboard smart alert."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    tenant_id = UUID(UMS_TENANT_ID)
    with Session(engine) as session:
        session.add_all(
            [
                AuditLogORM(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    user_id=USER_ID,
                    event_type="CONNECTOR_JOB_RUN",
                    entity_type="connector_run",
                    entity_id="run-skipped-a",
                    scope_type="finance-month",
                    scope_id="2026-03",
                    reason="connector normalize: source rows skipped during projection",
                    details={
                        "lifecycle": "ROWS_SKIPPED",
                        "skipped_count": 2,
                        "skipped_by_reason": {"unknown_channel": 2},
                    },
                    sensitive=True,
                    created_at=datetime(2026, 4, 1, tzinfo=UTC),
                ),
                AuditLogORM(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    user_id=USER_ID,
                    event_type="CONNECTOR_JOB_RUN",
                    entity_type="connector_run",
                    entity_id="run-skipped-b",
                    scope_type="finance-month",
                    scope_id="2026-03",
                    reason="connector normalize: source rows skipped during projection",
                    details={
                        "lifecycle": "ROWS_SKIPPED",
                        "skipped_count": 1,
                        "skipped_by_reason": {"missing_channel_id": 1},
                    },
                    sensitive=True,
                    created_at=datetime(2026, 4, 2, tzinfo=UTC),
                ),
                AuditLogORM(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    user_id=USER_ID,
                    event_type="CONNECTOR_JOB_RUN",
                    entity_type="connector_run",
                    entity_id="run-skipped-other-month",
                    scope_type="finance-month",
                    scope_id="2026-02",
                    reason="connector normalize: source rows skipped during projection",
                    details={
                        "lifecycle": "ROWS_SKIPPED",
                        "skipped_count": 9,
                        "skipped_by_reason": {"stale_month": 9},
                    },
                    sensitive=True,
                    created_at=datetime(2026, 4, 3, tzinfo=UTC),
                ),
                AuditLogORM(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    user_id=USER_ID,
                    event_type="CONNECTOR_JOB_RUN",
                    entity_type="connector_run",
                    entity_id="run-finished",
                    scope_type="finance-month",
                    scope_id="2026-03",
                    reason="connector finished",
                    details={"lifecycle": "FINISHED", "status": "SUCCEEDED"},
                    sensitive=True,
                    created_at=datetime(2026, 4, 4, tzinfo=UTC),
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 200
    codes = [alert["code"] for alert in response.json()["alerts"]]
    assert codes.index("SOURCE_ROWS_SKIPPED") < codes.index("PAYMENT_NOT_MATCHED")
    skipped = next(
        (alert for alert in response.json()["alerts"] if alert["code"] == "SOURCE_ROWS_SKIPPED"),
        None,
    )
    assert skipped is not None
    assert skipped["severity"] == "HIGH"
    assert skipped["source"] == "connector_job_run"
    assert skipped["details"] == {
        "skipped_count": 3,
        "skipped_by_reason": {
            "missing_channel_id": 1,
            "unknown_channel": 2,
        },
    }


def test_coverage_alert_excludes_inactive_and_non_required_channels(tmp_path):
    """Only active AND revenue_required factless channels surface in the alert."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    with Session(engine) as session:
        session.add_all(
            [
                # Flagged: active + revenue_required + no fact.
                YouTubeChannelORM(
                    id=UUID("00000000-0000-0000-0000-00000000b302"),
                    youtube_channel_id="channel-tv-b",
                    channel_name="TV B",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=True,
                ),
                # Not flagged: archived (active=False), factless.
                YouTubeChannelORM(
                    id=UUID("00000000-0000-0000-0000-00000000b303"),
                    youtube_channel_id="channel-tv-archived",
                    channel_name="TV Archived",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=True,
                    active=False,
                ),
                # Not flagged: revenue not required, factless.
                YouTubeChannelORM(
                    id=UUID("00000000-0000-0000-0000-00000000b304"),
                    youtube_channel_id="channel-tv-optional",
                    channel_name="TV Optional",
                    primary_org_unit_id=COMPANY_ID,
                    cms_status="INSIDE_CMS",
                    revenue_required=False,
                    active=True,
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 200
    coverage = next(
        (
            alert
            for alert in response.json()["alerts"]
            if alert["code"] == "CHANNELS_MISSING_REVENUE_FACTS"
        ),
        None,
    )
    assert coverage is not None
    # Only the active, revenue-required, factless channel is flagged.
    assert coverage["details"]["channel_count"] == 1
    assert coverage["details"]["sample_channel_ids"] == ["channel-tv-b"]
    assert "channel-tv-archived" not in coverage["details"]["sample_channel_ids"]
    assert "channel-tv-optional" not in coverage["details"]["sample_channel_ids"]


def test_assistant_cannot_read_month_smart_alerts(tmp_path):
    """
    Test that an assistant_analyst user without finance.view_revenue permission
    receives a 403 Forbidden response when accessing monthly smart alerts.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("assistant_analyst", "global"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"
