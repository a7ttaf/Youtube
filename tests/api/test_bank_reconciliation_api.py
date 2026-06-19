"""
Tests for the bank reconciliation API endpoints.

This module contains helper functions for authentication headers, database setup, and
sample payloads, as well as unit tests verifying bank reconciliation recording,
retrieval, permissions, idempotency, and audit logging behaviors.
"""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session

from ums_smart_revenue.app import create_app
from ums_smart_revenue.db.finance_models import (
    AdSensePaymentORM,
    FinanceBase,
    FinanceMonthCloseORM,
)
from ums_smart_revenue.db.security_models import AuditLogORM, SecurityBase, UserORM

USER_ID = UUID("00000000-0000-0000-0000-000000009901")


def auth_headers(
    role: str,
    scope_type: str = "global",
    scope_id: str | None = None,
) -> dict[str, str]:
    """Create authentication headers for a user with specified role and scope."""
    headers = {
        "x-user-id": str(USER_ID),
        "x-user-email": "bank-reconciliation@example.com",
        "x-role": role,
        "x-scope-type": scope_type,
        "x-ums-trusted-gateway-token": "pytest-trusted-gateway-token",
    }
    if scope_id is not None:
        headers["x-scope-id"] = scope_id
    return headers


def build_database_url(tmp_path) -> str:
    """Build and return a new SQLite database URL in the provided temporary path."""
    return f"sqlite+pysqlite:///{(tmp_path / f'{uuid4()}.db').as_posix()}"


def seed_database(database_url: str, *, locked_month: bool = False) -> None:
    """
    Seed the database at the given URL with initial user and payment data.
    Optionally lock a finance month if locked_month is True.
    """
    engine = create_engine(database_url)
    SecurityBase.metadata.create_all(engine)
    FinanceBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                UserORM(
                    id=USER_ID,
                    email="bank-reconciliation@example.com",
                    display_name="Bank Reconciliation User",
                ),
                AdSensePaymentORM(
                    id=uuid4(),
                    month="2026-03",
                    payment_name="AdSense payment March 2026",
                    payment_date=date(2026, 4, 21),
                    payment_amount=Decimal("930.00"),
                    payment_currency="USD",
                    payment_status="PAID",
                    raw_payload={"paymentId": "pay_2026_03"},
                    source_report_id="adsense-payment-2026-03",
                    source_account_id="pub-1",
                    imported_by=USER_ID,
                ),
            ]
        )
        if locked_month:
            session.add(
                FinanceMonthCloseORM(
                    month="2026-03",
                    status="LOCKED",
                    locked_by=USER_ID,
                )
            )
        session.commit()


def bank_payload(amount_usd: str = "928.50") -> dict[str, object]:
    """Return a sample bank reconciliation payload."""
    return {
        "bank_reference": "bank-transfer-2026-04-22",
        "bank_received_date": "2026-04-22",
        "bank_received_amount": amount_usd,
        "bank_received_currency": "USD",
        "bank_received_amount_usd": amount_usd,
        "transfer_fee_usd": "1.50",
        "fx_difference_usd": "0.25",
        "notes": "Bank transfer received",
        "source_report_id": "bank-statement-2026-04",
        "reason": "Record bank receipt for March AdSense payment",
    }


def test_finance_admin_records_bank_reconciliation_with_audit(tmp_path):
    """
    Test that a finance admin can record a bank reconciliation entry
    and that it is audited correctly.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/months/2026-03/bank-reconciliation",
        headers=auth_headers("finance_admin", "global"),
        json=bank_payload(),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        bank_row = (
            session.execute(text("SELECT * FROM bank_reconciliation_entries")).mappings().one()
        )
        audit_log = session.scalars(select(AuditLogORM)).one()

    assert response.status_code == 201
    assert response.json()["bank_reference"] == "bank-transfer-2026-04-22"
    assert response.json()["bank_received_amount_usd"] == "928.5"
    assert response.json()["audit_event"]["event_type"] == "BANK_RECONCILIATION_RECORDED"
    assert bank_row["bank_received_amount_usd"] == Decimal("928.500000")
    assert audit_log.event_type == "BANK_RECONCILIATION_RECORDED"
    assert audit_log.reason == "Record bank receipt for March AdSense payment"
    assert audit_log.sensitive is True


def test_bank_reconciliation_record_is_idempotent_for_same_month_reference(tmp_path):
    """
    Test that submitting bank reconciliation for the same month
    and reference is idempotent and updates correctly.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    first = client.post(
        "/revenue/months/2026-03/bank-reconciliation",
        headers=auth_headers("finance_admin", "global"),
        json=bank_payload("928.50"),
    )
    second = client.post(
        "/revenue/months/2026-03/bank-reconciliation",
        headers=auth_headers("finance_admin", "global"),
        json=bank_payload("930.00"),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        bank_count = session.execute(
            text("SELECT COUNT(*) FROM bank_reconciliation_entries")
        ).scalar_one()
        received_amount = session.execute(
            text("SELECT bank_received_amount_usd FROM bank_reconciliation_entries")
        ).scalar_one()

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["bank_received_amount_usd"] == "930"
    assert bank_count == 1
    assert received_amount == Decimal("930.000000")


def test_finance_viewer_reads_bank_reconciliation_summary_with_audit(tmp_path):
    """
    Test that a finance viewer can read the bank reconciliation
    summary and see the related audit events.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))
    create_response = client.post(
        "/revenue/months/2026-03/bank-reconciliation",
        headers=auth_headers("finance_admin", "global"),
        json=bank_payload(),
    )

    response = client.get(
        "/revenue/months/2026-03/bank-reconciliation",
        headers=auth_headers("finance_viewer", "global"),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        audit_logs = session.scalars(
            select(AuditLogORM).order_by(
                AuditLogORM.created_at,
                AuditLogORM.event_type,
                AuditLogORM.id,
            )
        ).all()

    assert create_response.status_code == 201
    assert response.status_code == 200
    assert response.json()["status"] == "BANK_VARIANCE"
    assert response.json()["adsense_paid_amount_usd"] == "930"
    assert response.json()["bank_received_amount_usd"] == "928.5"
    assert response.json()["bank_gap_usd"] == "1.5"
    money_provenance = response.json()["money_provenance"]
    assert money_provenance["adsense_paid_amount_usd"] == {
        "source": "adsense_payments",
        "formula": (
            "sum(payment_amount) for records in the month where "
            "payment_currency = USD and payment_status = PAID"
        ),
        "confidence": "SOURCE_BACKED",
        "export_value": "930",
    }
    assert money_provenance["bank_gap_usd"] == {
        "source": "adsense_payments_and_bank_reconciliation_entries",
        "formula": (
            "adsense_paid_amount_usd - bank_received_amount_usd when both "
            "paid AdSense and bank receipt sources exist"
        ),
        "confidence": "VARIANCE",
        "export_value": "1.5",
    }
    assert response.json()["entries"][0]["bank_reference"] == ("bank-transfer-2026-04-22")
    assert response.json()["audit_events"][0]["event_type"] == ("BANK_RECONCILIATION_VIEWED")
    assert response.json()["audit_events"][1]["event_type"] == "PAYMENT_VIEWED"
    assert [log.event_type for log in audit_logs] == [
        "BANK_RECONCILIATION_RECORDED",
        "BANK_RECONCILIATION_VIEWED",
        "PAYMENT_VIEWED",
    ]
    assert all(log.sensitive is True for log in audit_logs)


def test_finance_month_scoped_admin_records_matching_month(tmp_path):
    """
    Test that a finance-month scoped admin can record bank
    reconciliation for the matching month only.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/months/2026-03/bank-reconciliation",
        headers=auth_headers("finance_admin", "finance-month", "2026-03"),
        json=bank_payload(),
    )

    assert response.status_code == 201
    assert response.json()["month"] == "2026-03"


def test_finance_month_scoped_viewer_cannot_read_another_month(tmp_path):
    """
    Test that a finance-month scoped viewer cannot read bank reconciliation for a different month.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-04/bank-reconciliation",
        headers=auth_headers("finance_viewer", "finance-month", "2026-03"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == ("Missing permission: finance.view_bank_reconciliation")


def test_assistant_cannot_read_bank_reconciliation(tmp_path):
    """
    Test that an assistant analyst cannot read bank reconciliation
    summary due to missing permission.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.get(
        "/revenue/months/2026-03/bank-reconciliation",
        headers=auth_headers("assistant_analyst", "global"),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == ("Missing permission: finance.view_bank_reconciliation")


def test_finance_viewer_cannot_record_bank_reconciliation(tmp_path):
    """
    Test that a finance viewer cannot record bank reconciliation due to insufficient permissions.
    """
    database_url = build_database_url(tmp_path)
    seed_database(database_url)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/months/2026-03/bank-reconciliation",
        headers=auth_headers("finance_viewer", "global"),
        json=bank_payload(),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == ("Missing permission: finance.manage_bank_reconciliation")


def test_locked_finance_month_rejects_bank_reconciliation_writes(tmp_path):
    """Reject bank reconciliation writes to locked finance months."""
    database_url = build_database_url(tmp_path)
    seed_database(database_url, locked_month=True)
    client = TestClient(create_app(database_url=database_url))

    response = client.post(
        "/revenue/months/2026-03/bank-reconciliation",
        headers=auth_headers("finance_admin", "global"),
        json=bank_payload(),
    )

    engine = create_engine(database_url)
    with Session(engine) as session:
        bank_count = session.execute(
            text("SELECT COUNT(*) FROM bank_reconciliation_entries")
        ).scalar_one()

    assert response.status_code == 409
    assert response.json()["detail"] == ("Finance month is locked for bank reconciliation")
    assert bank_count == 0
