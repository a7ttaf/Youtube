# ============================================================================
# Purpose: Verify Smart Alerts API authorization, finance-signal composition,
#   and audit-log disclosure behavior through request-level tests.
# Database/ORM: SQLite test app with FinanceBase, OrgBase, SecurityBase, and
#   seeded AuditLogORM connector/source-row edges.
# Standards: Endpoint tests use real dependency wiring, permission headers, and
#   persisted audit reads instead of bypassing route-level auth/audit behavior.
# Blast Radius: Test coverage only for smart-alert API, finance monitoring, and
#   connector/source-row audit-derived alert disclosure.
# Connections:
#   - File: backend/ums_smart_revenue/api/revenue.py -> smart-alert endpoint.
#   - File: backend/ums_smart_revenue/connectors/runs/audit_alerts.py -> failed
#     connector-run audit read model.
# ============================================================================
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
    """The latest per-month ROWS_SKIPPED edge surfaces the SOURCE_ROWS_SKIPPED alert.

    The alert is gated by VIEW_AUDIT_LOG. The latest skipped edge wins (no
    double-counting across re-runs), so only ``run-skipped-b`` contributes
    even though ``run-skipped-a`` reported a different breakdown. The
    other-month row is filtered by scope and does not contribute.
    """
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
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    # finance_approver has VIEW_AUDIT_LOG (no VIEW_SENSITIVE_AUDIT_PAYLOADS),
    # so the alert surfaces with the count visible and the reason breakdown
    # redacted to an empty dict.
    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_approver", "global"),
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
        "skipped_count": 1,
        "skipped_by_reason": {},
    }
    with Session(engine) as session:
        audit_reads = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == "AUDIT_LOG_VIEWED")
        ).all()
    assert len(audit_reads) == 1
    assert audit_reads[0].entity_type == "audit_log_page"
    assert audit_reads[0].scope_type == "global"
    assert audit_reads[0].details["returned"] == 1
    assert audit_reads[0].details["details_redacted"] is True


def test_month_smart_alerts_clear_stale_skipped_source_after_clean_rerun(tmp_path):
    """A later clean connector-run edge clears older append-only ROWS_SKIPPED history."""
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
                    entity_id="run-skipped",
                    scope_type="finance-month",
                    scope_id="2026-03",
                    reason="connector normalize: source rows skipped during projection",
                    details={
                        "lifecycle": "ROWS_SKIPPED",
                        "skipped_count": 3,
                        "skipped_by_reason": {"unknown_channel": 3},
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
                    entity_id="run-clean",
                    scope_type="finance-month",
                    scope_id="2026-03",
                    reason="connector finished",
                    details={"lifecycle": "FINISHED", "status": "SUCCEEDED"},
                    sensitive=True,
                    created_at=datetime(2026, 4, 2, tzinfo=UTC),
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_approver", "global"),
    )

    assert response.status_code == 200
    codes = [alert["code"] for alert in response.json()["alerts"]]
    assert "SOURCE_ROWS_SKIPPED" not in codes
    with Session(engine) as session:
        audit_reads = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == "AUDIT_LOG_VIEWED")
        ).all()
    assert len(audit_reads) == 1
    assert audit_reads[0].details["returned"] == 0
    assert audit_reads[0].details["details_redacted"] is False


def test_month_smart_alerts_surface_failed_connector_run_audit_edges(tmp_path):
    """Latest FINISHED connector/account edges surface failed connector runs."""
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
                    entity_id="run-youtube-old-failed",
                    scope_type="connector",
                    scope_id="youtube-reporting",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "youtube-reporting",
                        "account_id": "content-owner-1",
                        "report_month": "2026-03",
                        "status": "FAILED",
                        "counts": {"reports_failed": 1},
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
                    entity_id="run-youtube-clean",
                    scope_type="connector",
                    scope_id="youtube-reporting",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "youtube-reporting",
                        "account_id": "content-owner-1",
                        "report_month": "2026-03",
                        "status": "SUCCEEDED",
                        "counts": {"reports_succeeded": 1},
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
                    entity_id="run-adsense-partial",
                    scope_type="connector",
                    scope_id="adsense-management",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "adsense-management",
                        "account_id": "pub-1",
                        "report_month": "2026-03",
                        "status": "PARTIAL",
                        "counts": {"reports_failed": 1, "reports_succeeded": 2},
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
                    entity_id="run-other-month-failed",
                    scope_type="connector",
                    scope_id="youtube-reporting",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "youtube-reporting",
                        "account_id": "content-owner-1",
                        "report_month": "2026-02",
                        "status": "FAILED",
                        "counts": {"reports_failed": 1},
                    },
                    sensitive=True,
                    created_at=datetime(2026, 4, 4, tzinfo=UTC),
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_approver", "global"),
    )

    assert response.status_code == 200
    codes = [alert["code"] for alert in response.json()["alerts"]]
    assert codes.index("CONNECTOR_RUNS_FAILED") < codes.index("PAYMENT_NOT_MATCHED")
    failed = next(
        (alert for alert in response.json()["alerts"] if alert["code"] == "CONNECTOR_RUNS_FAILED"),
        None,
    )
    assert failed is not None
    assert failed["severity"] == "HIGH"
    assert failed["source"] == "connector_job_run"
    assert failed["details"] == {
        "failed_run_count": 1,
        "failed_by_status": {"PARTIAL": 1},
    }
    with Session(engine) as session:
        audit_reads = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == "AUDIT_LOG_VIEWED")
        ).all()
    assert len(audit_reads) == 1
    assert audit_reads[0].details["returned"] == 1
    assert audit_reads[0].details["source_rows_skipped_returned"] == 0
    assert audit_reads[0].details["connector_runs_failed_returned"] == 1
    assert audit_reads[0].details["details_redacted"] is False


def test_month_smart_alerts_normalize_connector_aliases_before_latest_edge(
    tmp_path,
):
    """A canonical underscore success clears an older public hyphen-key failure."""
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
                    entity_id="run-hyphen-failed",
                    scope_type="connector",
                    scope_id="youtube-reporting",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "youtube-reporting",
                        "account_id": "content-owner-1",
                        "report_month": "2026-03",
                        "status": "FAILED",
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
                    entity_id="run-underscore-success",
                    scope_type="connector",
                    scope_id="youtube_reporting",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "youtube_reporting",
                        "account_id": "content-owner-1",
                        "report_month": "2026-03",
                        "status": "SUCCEEDED",
                    },
                    sensitive=True,
                    created_at=datetime(2026, 4, 2, tzinfo=UTC),
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_approver", "global"),
    )

    assert response.status_code == 200
    codes = [alert["code"] for alert in response.json()["alerts"]]
    assert "CONNECTOR_RUNS_FAILED" not in codes
    with Session(engine) as session:
        audit_reads = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == "AUDIT_LOG_VIEWED")
        ).all()
    assert len(audit_reads) == 1
    assert audit_reads[0].details["returned"] == 0
    assert audit_reads[0].details["connector_runs_failed_returned"] == 0


def test_month_smart_alerts_normalize_adsense_resource_account_before_latest_edge(
    tmp_path,
):
    """An AdSense raw-id success clears an older resource-name account failure."""
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
                    entity_id="run-adsense-resource-failed",
                    scope_type="connector",
                    scope_id="adsense-management",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "adsense-management",
                        "account_id": "accounts/pub-1",
                        "report_month": "2026-03",
                        "status": "FAILED",
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
                    entity_id="run-adsense-raw-success",
                    scope_type="connector",
                    scope_id="adsense_management",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "adsense_management",
                        "account_id": "pub-1",
                        "report_month": "2026-03",
                        "status": "SUCCEEDED",
                    },
                    sensitive=True,
                    created_at=datetime(2026, 4, 2, tzinfo=UTC),
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_approver", "global"),
    )

    assert response.status_code == 200
    codes = [alert["code"] for alert in response.json()["alerts"]]
    assert "CONNECTOR_RUNS_FAILED" not in codes
    with Session(engine) as session:
        audit_reads = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == "AUDIT_LOG_VIEWED")
        ).all()
    assert len(audit_reads) == 1
    assert audit_reads[0].details["returned"] == 0
    assert audit_reads[0].details["connector_runs_failed_returned"] == 0


def test_month_smart_alerts_include_superseded_connector_run_terminal_edge(
    tmp_path,
):
    """Superseded stale RUNNING rows are failed terminal connector-run signals."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    tenant_id = UUID(UMS_TENANT_ID)
    with Session(engine) as session:
        session.add(
            AuditLogORM(
                id=uuid4(),
                tenant_id=tenant_id,
                user_id=USER_ID,
                event_type="CONNECTOR_JOB_RUN",
                entity_type="api_connector",
                entity_id="youtube-reporting:content-owner-1",
                scope_type="connector",
                scope_id="youtube-reporting",
                reason="orphaned RUNNING run superseded by new job",
                details={
                    "action": "run_superseded",
                    "run_id": "run-old",
                    "report_month": "2026-03",
                    "lifecycle": "FINISHED",
                    "status": "FAILED",
                    "error_summary_present": True,
                },
                sensitive=True,
                created_at=datetime(2026, 4, 1, tzinfo=UTC),
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_approver", "global"),
    )

    assert response.status_code == 200
    alerts = [
        alert for alert in response.json()["alerts"] if alert["code"] == "CONNECTOR_RUNS_FAILED"
    ]
    assert len(alerts) == 1
    failed = alerts[0]
    assert failed["severity"] == "HIGH"
    assert failed["source"] == "connector_job_run"
    assert failed["details"] == {
        "failed_run_count": 1,
        "failed_by_status": {"FAILED": 1},
    }
    with Session(engine) as session:
        audit_reads = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == "AUDIT_LOG_VIEWED")
        ).all()
    assert len(audit_reads) == 1
    assert audit_reads[0].details["returned"] == 1
    assert audit_reads[0].details["connector_runs_failed_returned"] == 1


def test_month_smart_alerts_include_projection_failed_connector_run(tmp_path):
    """A projection-failed audit edge is a failed connector-run signal."""
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
                    entity_id="run-clean-before-projection",
                    scope_type="connector",
                    scope_id="youtube_reporting",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "youtube_reporting",
                        "account_id": "content-owner-1",
                        "report_month": "2026-03",
                        "status": "SUCCEEDED",
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
                    entity_id="run-projection-failed",
                    scope_type="connector",
                    scope_id="youtube_reporting",
                    reason="post-run normalize failed; run rewritten to FAILED",
                    details={
                        "lifecycle": "PROJECTION_FAILED",
                        "connector_key": "youtube_reporting",
                        "account_id": "content-owner-1",
                        "report_month": "2026-03",
                        "error_summary_present": True,
                    },
                    sensitive=True,
                    created_at=datetime(2026, 4, 2, tzinfo=UTC),
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_approver", "global"),
    )

    assert response.status_code == 200
    failed = next(
        (alert for alert in response.json()["alerts"] if alert["code"] == "CONNECTOR_RUNS_FAILED"),
        None,
    )
    assert failed is not None
    assert failed["details"]["failed_run_count"] == 1
    assert failed["details"]["failed_by_status"] == {"FAILED": 1}
    with Session(engine) as session:
        audit_reads = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == "AUDIT_LOG_VIEWED")
        ).all()
    assert len(audit_reads) == 1
    assert audit_reads[0].details["returned"] == 1
    assert audit_reads[0].details["connector_runs_failed_returned"] == 1


def test_month_smart_alerts_clear_stale_failed_connector_on_malformed_latest_edge(
    tmp_path,
):
    """A latest malformed terminal edge clears an older failure for the same connector."""
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
                    entity_id="run-old-failed",
                    scope_type="connector",
                    scope_id="youtube-reporting",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "youtube-reporting",
                        "account_id": "content-owner-1",
                        "report_month": "2026-03",
                        "status": "FAILED",
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
                    entity_id="run-latest-malformed",
                    scope_type="connector",
                    scope_id="youtube-reporting",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "youtube-reporting",
                        "account_id": "content-owner-1",
                        "report_month": "2026-03",
                        "status": "UNKNOWN",
                    },
                    sensitive=True,
                    created_at=datetime(2026, 4, 2, tzinfo=UTC),
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_approver", "global"),
    )

    assert response.status_code == 200
    codes = [alert["code"] for alert in response.json()["alerts"]]
    assert "CONNECTOR_RUNS_FAILED" not in codes
    with Session(engine) as session:
        audit_reads = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == "AUDIT_LOG_VIEWED")
        ).all()
    assert len(audit_reads) == 1
    assert audit_reads[0].details["returned"] == 0
    assert audit_reads[0].details["connector_runs_failed_returned"] == 0


def test_month_smart_alerts_clear_failed_connector_when_latest_edges_tie_with_success(
    tmp_path,
):
    """Same-timestamp success/failure ties should not surface an ambiguous failure."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    tenant_id = UUID(UMS_TENANT_ID)
    tied_created_at = datetime(2026, 4, 2, tzinfo=UTC)
    with Session(engine) as session:
        session.add_all(
            [
                AuditLogORM(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    user_id=USER_ID,
                    event_type="CONNECTOR_JOB_RUN",
                    entity_type="connector_run",
                    entity_id="run-tied-failed",
                    scope_type="connector",
                    scope_id="youtube-reporting",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "youtube-reporting",
                        "account_id": "content-owner-1",
                        "report_month": "2026-03",
                        "status": "FAILED",
                    },
                    sensitive=True,
                    created_at=tied_created_at,
                ),
                AuditLogORM(
                    id=uuid4(),
                    tenant_id=tenant_id,
                    user_id=USER_ID,
                    event_type="CONNECTOR_JOB_RUN",
                    entity_type="connector_run",
                    entity_id="run-tied-success",
                    scope_type="connector",
                    scope_id="youtube-reporting",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "youtube-reporting",
                        "account_id": "content-owner-1",
                        "report_month": "2026-03",
                        "status": "SUCCEEDED",
                    },
                    sensitive=True,
                    created_at=tied_created_at,
                ),
            ]
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_approver", "global"),
    )

    assert response.status_code == 200
    codes = [alert["code"] for alert in response.json()["alerts"]]
    assert "CONNECTOR_RUNS_FAILED" not in codes
    with Session(engine) as session:
        audit_reads = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == "AUDIT_LOG_VIEWED")
        ).all()
    assert len(audit_reads) == 1
    assert audit_reads[0].details["returned"] == 0
    assert audit_reads[0].details["connector_runs_failed_returned"] == 0


def test_month_smart_alerts_omit_skipped_source_alert_without_audit_permission(
    tmp_path,
):
    """finance_viewer (no VIEW_AUDIT_LOG) must not see the audit-derived alert."""
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
                    entity_id="run-skipped-only",
                    scope_type="finance-month",
                    scope_id="2026-03",
                    reason="connector normalize: source rows skipped during projection",
                    details={
                        "lifecycle": "ROWS_SKIPPED",
                        "skipped_count": 5,
                        "skipped_by_reason": {"unknown_channel": 5},
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
                    entity_id="run-failed-only",
                    scope_type="connector",
                    scope_id="youtube-reporting",
                    reason="connector finished",
                    details={
                        "lifecycle": "FINISHED",
                        "connector_key": "youtube-reporting",
                        "account_id": "content-owner-1",
                        "report_month": "2026-03",
                        "status": "FAILED",
                    },
                    sensitive=True,
                    created_at=datetime(2026, 4, 3, tzinfo=UTC),
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
    assert "SOURCE_ROWS_SKIPPED" not in codes
    assert "CONNECTOR_RUNS_FAILED" not in codes
    with Session(engine) as session:
        audit_reads = session.scalars(
            select(AuditLogORM).where(AuditLogORM.event_type == "AUDIT_LOG_VIEWED")
        ).all()
    assert audit_reads == []


def test_month_smart_alerts_audit_viewer_redacts_reason_breakdown(tmp_path):
    """finance_approver has VIEW_AUDIT_LOG but not VIEW_SENSITIVE_AUDIT_PAYLOADS.

    Per-reason counts are redacted; the total count is still surfaced so
    the dashboard reports the magnitude of the skip without leaking the
    sensitive per-reason breakdown.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    engine = create_engine(database_url)
    tenant_id = UUID(UMS_TENANT_ID)
    with Session(engine) as session:
        session.add(
            AuditLogORM(
                id=uuid4(),
                tenant_id=tenant_id,
                user_id=USER_ID,
                event_type="CONNECTOR_JOB_RUN",
                entity_type="connector_run",
                entity_id="run-skipped-sensitive",
                scope_type="finance-month",
                scope_id="2026-03",
                reason="connector normalize: source rows skipped during projection",
                details={
                    "lifecycle": "ROWS_SKIPPED",
                    "skipped_count": 5,
                    "skipped_by_reason": {"unknown_channel": 5},
                },
                sensitive=True,
                created_at=datetime(2026, 4, 2, tzinfo=UTC),
            )
        )
        session.commit()
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/smart-alerts",
        headers=auth_headers("finance_approver", "global"),
    )

    assert response.status_code == 200
    skipped = next(
        (alert for alert in response.json()["alerts"] if alert["code"] == "SOURCE_ROWS_SKIPPED"),
        None,
    )
    assert skipped is not None
    assert skipped["details"]["skipped_count"] == 5
    assert skipped["details"]["skipped_by_reason"] == {}


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
