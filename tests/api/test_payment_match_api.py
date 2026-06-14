"""Test payment match API helpers, auth boundaries, and failure handling."""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.revenue import get_month_payment_match
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.finance_models import (
    AdSensePaymentORM,
    FinanceBase,
    MonthlyChannelRevenueFactORM,
)
from ums_smart_revenue.db.org_models import (
    OrgBase,
    OrgUnitORM,
    YouTubeChannelORM,
)
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.finance.adsense_payments import AdSensePaymentValidationError

SECTOR_ID = UUID("00000000-0000-0000-0000-00000000a101")
COMPANY_ID = UUID("00000000-0000-0000-0000-00000000a201")
CHANNEL_ROW_ID = UUID("00000000-0000-0000-0000-00000000a301")
USER_ID = UUID("00000000-0000-0000-0000-00000000a401")


def auth_headers(
    role: str,
    scope_type: str = "global",
    scope_id: str | None = None,
) -> dict[str, str]:
    """
    Generate authentication headers for testing requests.

    Args:
        role: The role to include in the headers.
        scope_type: The type of access scope (default is "global").
        scope_id: Optional identifier for the access scope.

    Returns:
        A dictionary of HTTP headers with authentication information.
    """
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "payment-match@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    """
    Construct a temporary SQLite database URL using the given path.

    Args:
        tmp_path: A pathlib Path object pointing to a temporary directory.

    Returns:
        A database URL string for an SQLite database file in the tmp_path.
    """
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed_database(database_url: str, *, payment_amount: str = "930.00") -> None:
    """
    Seed the test database with initial organization, security, and finance data.

    This function creates all tables, establishes a session, and inserts
    default entities such as sectors, companies, YouTube channels, users,
    and finance records with a specified payment amount.

    Args:
        database_url: The database URL to connect to.
        payment_amount: The default payment amount to seed (as a string).
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
                    gross_revenue_usd=Decimal("930.00"),
                    net_revenue_usd=None,
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
                    payment_amount=Decimal(payment_amount),
                    payment_currency="USD",
                    payment_status="PAID",
                    raw_payload={"paymentId": "pay_2026_03"},
                    source_report_id="adsense-payment-2026-03",
                    source_account_id="pub-1",
                    imported_by=USER_ID,
                ),
                UserORM(
                    id=USER_ID,
                    email="payment-match@example.com",
                    display_name="Payment Match User",
                ),
            ]
        )
        session.commit()


def test_finance_viewer_reads_payment_match_with_revenue_and_payment_audits(
    tmp_path,
):
    """Test that a finance viewer can read the payment match along with revenue and
    payment audit logs."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/payment-match",
        headers=auth_headers("finance_viewer", "global"),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_logs = session.scalars(select(AuditLogORM).order_by(AuditLogORM.event_type)).all()

    assert response.status_code == 200
    assert response.json()["status"] == "PAYMENT_MATCHED"
    assert response.json()["currency"] == "USD"
    assert response.json()["youtube_revenue_total_usd"] == "930"
    assert response.json()["adsense_paid_amount"] == "930"
    assert response.json()["payment_gap_usd"] == "0"
    assert response.json()["audit_events"][0]["event_type"] == "REVENUE_VIEWED"
    assert response.json()["audit_events"][1]["event_type"] == "PAYMENT_VIEWED"
    audit_logs_by_type = {log.event_type: log for log in audit_logs}
    assert set(audit_logs_by_type) == {
        "REVENUE_VIEWED",
        "PAYMENT_VIEWED",
    }
    assert (
        audit_logs_by_type["PAYMENT_VIEWED"].scope_type,
        audit_logs_by_type["PAYMENT_VIEWED"].scope_id,
    ) == (
        "finance-month",
        "2026-03",
    )
    assert all(log.sensitive is True for log in audit_logs)


def test_payment_match_maps_adsense_payment_validation_to_422():
    """Test that invalid AdSense payment queries map to a 422 HTTPException."""
    user = UserPrincipal(
        user_id=str(USER_ID),
        email="payment-match@example.com",
        role_assignments=(
            RoleAssignment(role=RoleKey.FINANCE_VIEWER, scope=AccessScope.global_scope()),
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        get_month_payment_match(
            month="2026-03",
            user=user,
            revenue_repository=_EmptyRevenueRepository(),
            payment_repository=_FailingPaymentRepository(),
            audit_sink=InMemoryAuditSink(),
            currency="USD",
        )

    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "invalid payment query"


def test_month_payment_match_reports_payment_variance(tmp_path):
    """Test that the payment match reports variance between revenue and payment amounts."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url, payment_amount="900.00")
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/payment-match",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 200
    assert response.json()["status"] == "PAYMENT_VARIANCE"
    assert response.json()["payment_gap_usd"] == "30"
    assert response.json()["issues"][0]["issue_type"] == "PAYMENT_GAP"


def test_month_payment_match_rejects_non_usd_currency_until_exchange_rates_exist(
    tmp_path,
):
    """Test that non-USD currency requests are rejected until exchange-rate support
    is implemented."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/payment-match?currency=EUR",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 422
    assert response.json()["detail"] == (
        "currency must be USD until exchange-rate support is implemented"
    )


def test_assistant_cannot_read_month_payment_match(tmp_path):
    """Test that an assistant role cannot read month payment matches due to missing permissions."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/payment-match",
        headers=auth_headers("assistant_analyst", "global"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


def test_company_scoped_finance_viewer_cannot_read_holding_payment_match(tmp_path):
    """Test that company-scoped finance viewers cannot read holding payment matches
    outside their scope."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/payment-match",
        headers=auth_headers("finance_viewer", "company", str(COMPANY_ID)),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Missing permission: finance.view_revenue"


class _EmptyRevenueRepository:
    """Repository stub that returns no revenue data for testing scenarios."""

    @staticmethod
    def list_month_facts(*, month: str):
        """Return an empty list of monthly facts for testing scenarios."""
        return []


class _FailingPaymentRepository:
    """Payment repository stub that raises validation errors."""

    @staticmethod
    def list_month_payments(*, month: str):
        """Raise an AdSensePaymentValidationError to simulate payment validation failures."""
        raise AdSensePaymentValidationError("invalid payment query")
