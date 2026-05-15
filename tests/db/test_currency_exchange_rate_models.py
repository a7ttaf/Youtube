from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import CurrencyExchangeRateORM, FinanceBase

USER_ID = UUID("00000000-0000-0000-0000-00000000e101")


def test_currency_exchange_rate_model_persists_provider_rate():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            CurrencyExchangeRateORM(
                id=uuid4(),
                rate_date=date(2026, 4, 22),
                base_currency="EUR",
                quote_currency="USD",
                rate=Decimal("1.0845000000"),
                provider_key="ecb",
                source_report_id="ecb-2026-04-22",
                raw_payload={"source": "ecb"},
                imported_by=USER_ID,
            )
        )
        session.commit()
        rate = session.scalars(select(CurrencyExchangeRateORM)).one()

    assert rate.base_currency == "EUR"
    assert rate.quote_currency == "USD"
    assert rate.rate == Decimal("1.0845000000")
    assert rate.provider_key == "ecb"
    assert rate.source_report_id == "ecb-2026-04-22"
    assert rate.raw_payload == {"source": "ecb"}
    assert rate.imported_by == USER_ID


def test_currency_exchange_rate_model_rejects_invalid_currency_pair():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            CurrencyExchangeRateORM(
                id=uuid4(),
                rate_date=date(2026, 4, 22),
                base_currency="USD",
                quote_currency="USD",
                rate=Decimal("1.0000000000"),
                provider_key="ecb",
                raw_payload={},
                imported_by=USER_ID,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
