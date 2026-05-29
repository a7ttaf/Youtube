"""Tests for the AdSensePaymentORM model to ensure correct persistence and integrity."""

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import AdSensePaymentORM, FinanceBase

USER_ID = UUID("00000000-0000-0000-0000-000000009101")


def test_adsense_payment_model_persists_official_payment_metadata():
    """Test that AdSensePaymentORM persists official payment metadata correctly."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            AdSensePaymentORM(
                id=uuid4(),
                month="2026-03",
                payment_name="AdSense payment March 2026",
                payment_date=date(2026, 4, 21),
                payment_amount=Decimal("930.00"),
                payment_currency="USD",
                payment_status="PAID",
                raw_payload={"paymentId": "pay_2026_03"},
                source_report_id="raw-adsense-payment-2026-03",
                source_account_id="pub-1",
                imported_by=USER_ID,
            )
        )
        session.commit()
        payment = session.scalars(select(AdSensePaymentORM)).one()

    assert payment.month == "2026-03"
    assert payment.payment_name == "AdSense payment March 2026"
    assert payment.payment_amount == Decimal("930.00")
    assert payment.payment_currency == "USD"
    assert payment.payment_status == "PAID"
    assert payment.raw_payload == {"paymentId": "pay_2026_03"}
    assert payment.imported_by == USER_ID


def test_adsense_payment_model_rejects_non_alpha_currency_code():
    """Test that AdSensePaymentORM rejects non-alphabetic currency codes with IntegrityError."""
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            AdSensePaymentORM(
                id=uuid4(),
                month="2026-03",
                payment_name="Invalid currency payment",
                payment_date=date(2026, 4, 21),
                payment_amount=Decimal("930.00"),
                payment_currency="1$A",
                payment_status="PAID",
                raw_payload={"paymentId": "pay_2026_03"},
                source_account_id="pub-1",
                imported_by=USER_ID,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
