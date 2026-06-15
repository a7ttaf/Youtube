"""Tests for the AdSense payment paid/unpaid status endpoint."""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.api.adsense import get_adsense_payment_status
from ums_smart_revenue.app import create_app
from ums_smart_revenue.auth.audit_service import InMemoryAuditSink
from ums_smart_revenue.auth.models import RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.db.finance_models import AdSensePaymentORM, FinanceBase
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM
from ums_smart_revenue.finance.adsense_payments import AdSensePaymentValidationError

USER_ID = UUID("00000000-0000-0000-0000-00000000b401")
MONTH = "2026-04"


def auth_headers(role, scope_type="global", scope_id=None):
    """Build trusted-gateway auth headers."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "payment-status@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path):
    """Return a unique SQLite URL under pytest's temp path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def _payment(*, name, amount, status, currency, account):
    """Build one AdSensePaymentORM row (tenant_id defaults to the UMS tenant)."""
    return AdSensePaymentORM(
        id=uuid4(),
        month=MONTH,
        payment_name=name,
        payment_date=date(2026, 5, 21),
        payment_amount=Decimal(amount),
        payment_currency=currency,
        payment_status=status,
        raw_payload={"paymentId": name},
        source_report_id="adsense-payment-2026-04",
        source_account_id=account,
        imported_by=USER_ID,
    )


def seed_database(database_url):
    """Seed one trusted user and a multi-status/currency/account payment set."""
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(
            UserORM(
                id=USER_ID,
                email="payment-status@example.com",
                display_name="Status User",
            )
        )
        session.add_all(
            [
                _payment(
                    name="paid-1",
                    amount="8400.00",
                    status="PAID",
                    currency="USD",
                    account="pub-111",
                ),
                _payment(
                    name="pend-usd",
                    amount="1200.00",
                    status="PENDING",
                    currency="USD",
                    account="pub-222",
                ),
                _payment(
                    name="pend-eur",
                    amount="300.00",
                    status="PENDING",
                    currency="EUR",
                    account="pub-222",
                ),
                _payment(
                    name="unp-gbp",
                    amount="500.00",
                    status="UNPAID",
                    currency="GBP",
                    account="pub-222",
                ),
                _payment(
                    name="canc-usd",
                    amount="99.00",
                    status="CANCELLED",
                    currency="USD",
                    account="pub-222",
                ),
            ]
        )
        session.commit()


def test_finance_viewer_reads_payment_status_breakdown_with_audit(tmp_path):
    """Return the status breakdown and record an audit log."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        f"/adsense/payments/status?month={MONTH}",
        headers=auth_headers("finance_viewer", "global"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["month"] == MONTH
    assert body["total_payment_count"] == 5
    assert [b["status"] for b in body["status_totals"]] == [
        "PAID",
        "PENDING",
        "UNPAID",
        "CANCELLED",
    ]
    statuses = {b["status"]: b for b in body["status_totals"]}
    assert statuses["PAID"]["currency_totals"] == [{"currency": "USD", "amount": "8400"}]
    assert statuses["PENDING"]["currency_totals"] == [
        {"currency": "EUR", "amount": "300"},
        {"currency": "USD", "amount": "1200"},
    ]
    assert statuses["CANCELLED"]["currency_totals"] == [{"currency": "USD", "amount": "99"}]
    assert body["outstanding_totals"] == [
        {"currency": "EUR", "amount": "300"},
        {"currency": "GBP", "amount": "500"},
        {"currency": "USD", "amount": "1200"},
    ]
    assert [a["source_account_id"] for a in body["accounts"]] == [
        "pub-111",
        "pub-222",
    ]
    assert body["audit_event"]["event_type"] == "PAYMENT_VIEWED"

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_logs = session.scalars(select(AuditLogORM)).all()
    assert len(audit_logs) == 1
    assert audit_logs[0].event_type == "PAYMENT_VIEWED"
    assert (audit_logs[0].scope_type, audit_logs[0].scope_id) == (
        "finance-month",
        MONTH,
    )
    assert audit_logs[0].sensitive is True


def test_cancelled_amount_present_but_excluded_from_outstanding_via_api(tmp_path):
    """Report cancelled amounts but exclude them from outstanding totals."""
    # API mirror of operator-pinned cases 1 and 2.
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = client.get(
        f"/adsense/payments/status?month={MONTH}",
        headers=auth_headers("finance_viewer", "global"),
    ).json()
    try:
        cancelled = next(b for b in body["status_totals"] if b["status"] == "CANCELLED")
    except StopIteration:
        pytest.fail("missing CANCELLED status bucket")
    assert cancelled["currency_totals"] == [{"currency": "USD", "amount": "99"}]
    try:
        usd_outstanding = next(c for c in body["outstanding_totals"] if c["currency"] == "USD")
    except StopIteration:
        pytest.fail("missing USD outstanding total")
    assert usd_outstanding["amount"] == "1200"  # PENDING only, never the 99 CANCELLED


def test_non_usd_payment_surfaced_in_api_response(tmp_path):
    """Surface non-USD payments in API outstanding totals."""
    # API mirror of operator-pinned case 3.
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    body = client.get(
        f"/adsense/payments/status?month={MONTH}",
        headers=auth_headers("finance_viewer", "global"),
    ).json()
    currencies = {c["currency"] for c in body["outstanding_totals"]}
    assert {"EUR", "GBP"} <= currencies


def test_malformed_month_returns_422(tmp_path):
    """Reject malformed month parameters with HTTP 422."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        "/adsense/payments/status?month=2026-13",
        headers=auth_headers("finance_viewer", "global"),
    )
    assert response.status_code == 422


def test_repository_validation_error_maps_to_422():
    """Map repository validation errors to HTTP 422."""
    user = UserPrincipal(
        user_id=str(USER_ID),
        email="payment-status@example.com",
        role_assignments=(
            RoleAssignment(role=RoleKey.FINANCE_VIEWER, scope=AccessScope.global_scope()),
        ),
    )
    with pytest.raises(HTTPException) as exc_info:
        get_adsense_payment_status(
            month="2026-04",
            user=user,
            repository=_FailingPaymentRepository(),
            audit_sink=InMemoryAuditSink(),
        )
    assert exc_info.value.status_code == 422
    assert exc_info.value.detail == "invalid payment query"


def test_assistant_cannot_read_payment_status(tmp_path):
    """Forbid assistant role reads of payment status."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/adsense/payments/status?month={MONTH}",
        headers=auth_headers("assistant_analyst", "global"),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == ("Missing permission: finance.view_finalized_payments")


def test_finance_month_scoped_viewer_reads_matching_month(tmp_path):
    """Allow finance-month scoped viewers to read their assigned month."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/adsense/payments/status?month={MONTH}",
        headers=auth_headers("finance_viewer", "finance-month", MONTH),
    )
    assert response.status_code == 200


def test_finance_month_scoped_viewer_cannot_read_other_month(tmp_path):
    """Forbid finance-month scoped viewers from reading other months."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    response = client.get(
        f"/adsense/payments/status?month={MONTH}",
        headers=auth_headers("finance_viewer", "finance-month", "2026-03"),
    )
    assert response.status_code == 403
    assert response.json()["detail"] == ("Missing permission: finance.view_finalized_payments")


class _FailingPaymentRepository:
    """Repository stub that raises validation errors."""

    @staticmethod
    def list_month_payments(*, month: str):
        """Raise validation error for failing payment queries."""
        raise AdSensePaymentValidationError("invalid payment query")
