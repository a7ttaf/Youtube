from datetime import date
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import FinanceBase
from ums_smart_revenue.finance.exchange_rates import (
    CurrencyExchangeRateInput,
    ExchangeRateValidationError,
    SqlAlchemyExchangeRateRepository,
)

USER_ID = UUID("00000000-0000-0000-0000-00000000e201")


def test_exchange_rate_repository_upserts_provider_rate():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        repository = SqlAlchemyExchangeRateRepository(session)
        first = repository.sync_rates(
            rates=[
                CurrencyExchangeRateInput(
                    rate_date=date(2026, 4, 22),
                    base_currency="eur",
                    quote_currency="usd",
                    rate=Decimal("1.0845"),
                    raw_payload={"version": 1},
                )
            ],
            provider_key="ecb",
            actor_user_id=str(USER_ID),
            source_report_id="ecb-2026-04-22",
        )
        second = repository.sync_rates(
            rates=[
                CurrencyExchangeRateInput(
                    rate_date=date(2026, 4, 22),
                    base_currency="EUR",
                    quote_currency="USD",
                    rate=Decimal("1.0850"),
                    raw_payload={"version": 2},
                )
            ],
            provider_key="ecb",
            actor_user_id=str(USER_ID),
            source_report_id="ecb-2026-04-22-corrected",
        )
        session.commit()

    assert first[0].rate == Decimal("1.0845000000")
    assert second[0].rate == Decimal("1.0850000000")
    assert second[0].base_currency == "EUR"
    assert second[0].quote_currency == "USD"
    assert second[0].source_report_id == "ecb-2026-04-22-corrected"


def test_exchange_rate_repository_returns_latest_rate_on_or_before_date():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        repository = SqlAlchemyExchangeRateRepository(session)
        repository.sync_rates(
            rates=[
                CurrencyExchangeRateInput(
                    rate_date=date(2026, 4, 20),
                    base_currency="EUR",
                    quote_currency="USD",
                    rate=Decimal("1.0800"),
                ),
                CurrencyExchangeRateInput(
                    rate_date=date(2026, 4, 22),
                    base_currency="EUR",
                    quote_currency="USD",
                    rate=Decimal("1.0845"),
                ),
            ],
            provider_key="ecb",
            actor_user_id=str(USER_ID),
            source_report_id=None,
        )
        latest = repository.get_latest_rate(
            base_currency="EUR",
            quote_currency="USD",
            as_of_date=date(2026, 4, 21),
            provider_key="ecb",
        )

    assert latest is not None
    assert latest.rate_date == date(2026, 4, 20)
    assert latest.rate == Decimal("1.0800000000")


def test_exchange_rate_repository_rejects_invalid_rate():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        repository = SqlAlchemyExchangeRateRepository(session)
        with pytest.raises(ExchangeRateValidationError, match="rate must be > 0"):
            repository.sync_rates(
                rates=[
                    CurrencyExchangeRateInput(
                        rate_date=date(2026, 4, 22),
                        base_currency="EUR",
                        quote_currency="USD",
                        rate=Decimal("0"),
                    )
                ],
                provider_key="ecb",
                actor_user_id=str(USER_ID),
                source_report_id=None,
            )
