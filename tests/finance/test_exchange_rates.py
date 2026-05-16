from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import CurrencyExchangeRateORM, FinanceBase
from ums_smart_revenue.finance.exchange_rates import (
    CurrencyExchangeRateInput,
    ExchangeRateError,
    ExchangeRateValidationError,
    SqlAlchemyExchangeRateRepository,
    _dialect_insert,
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
    assert second[0].to_api(include_raw_payload=True)["raw_payload"] == {"version": 2}


def test_unsupported_exchange_rate_dialect_raises_runtime_error_type():
    with pytest.raises(ExchangeRateError) as exc_info:
        _dialect_insert("mysql")

    assert type(exc_info.value) is ExchangeRateError
    assert "Unsupported database dialect" in str(exc_info.value)


def test_exchange_rate_repository_rejects_duplicate_rate_keys_in_batch():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        repository = SqlAlchemyExchangeRateRepository(session)

        with pytest.raises(ExchangeRateValidationError, match="duplicate exchange rate"):
            repository.sync_rates(
                rates=[
                    CurrencyExchangeRateInput(
                        rate_date=date(2026, 4, 22),
                        base_currency="EUR",
                        quote_currency="USD",
                        rate=Decimal("1.0845"),
                    ),
                    CurrencyExchangeRateInput(
                        rate_date=date(2026, 4, 22),
                        base_currency="eur",
                        quote_currency="usd",
                        rate=Decimal("1.0850"),
                    ),
                ],
                provider_key="ecb",
                actor_user_id=str(USER_ID),
                source_report_id="ecb-duplicate",
            )


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


def test_exchange_rate_repository_prefers_most_recent_provider_when_latest_date_ties():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        repository = SqlAlchemyExchangeRateRepository(session)
        repository.sync_rates(
            rates=[
                CurrencyExchangeRateInput(
                    rate_date=date(2026, 4, 22),
                    base_currency="EUR",
                    quote_currency="USD",
                    rate=Decimal("1.0800"),
                )
            ],
            provider_key="aa-old-provider",
            actor_user_id=str(USER_ID),
            source_report_id=None,
        )
        repository.sync_rates(
            rates=[
                CurrencyExchangeRateInput(
                    rate_date=date(2026, 4, 22),
                    base_currency="EUR",
                    quote_currency="USD",
                    rate=Decimal("1.0900"),
                )
            ],
            provider_key="zz-new-provider",
            actor_user_id=str(USER_ID),
            source_report_id=None,
        )
        older = session.scalars(
            select(CurrencyExchangeRateORM).where(
                CurrencyExchangeRateORM.provider_key == "aa-old-provider"
            )
        ).one()
        newer = session.scalars(
            select(CurrencyExchangeRateORM).where(
                CurrencyExchangeRateORM.provider_key == "zz-new-provider"
            )
        ).one()
        older.updated_at = datetime(2026, 4, 22, 8, 0, tzinfo=UTC)
        newer.updated_at = datetime(2026, 4, 22, 9, 0, tzinfo=UTC)
        session.flush()
        latest = repository.get_latest_rate(
            base_currency="EUR",
            quote_currency="USD",
            as_of_date=date(2026, 4, 22),
        )

    assert latest is not None
    assert latest.provider_key == "zz-new-provider"
    assert latest.rate == Decimal("1.0900000000")


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


def test_exchange_rate_repository_rejects_rate_with_too_many_digits():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        repository = SqlAlchemyExchangeRateRepository(session)
        with pytest.raises(
            ExchangeRateValidationError,
            match="rate has too many significant digits:",
        ):
            repository.sync_rates(
                rates=[
                    CurrencyExchangeRateInput(
                        rate_date=date(2026, 4, 22),
                        base_currency="EUR",
                        quote_currency="USD",
                        rate=Decimal("123456789012345678901234567890.1234567890"),
                    )
                ],
                provider_key="ecb",
                actor_user_id=str(USER_ID),
                source_report_id=None,
            )


def test_exchange_rate_repository_validates_batch_before_upsert():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        repository = SqlAlchemyExchangeRateRepository(session)
        with pytest.raises(
            ExchangeRateValidationError,
            match="rate has too many significant digits:",
        ):
            repository.sync_rates(
                rates=[
                    CurrencyExchangeRateInput(
                        rate_date=date(2026, 4, 22),
                        base_currency="EUR",
                        quote_currency="USD",
                        rate=Decimal("1.0845"),
                    ),
                    CurrencyExchangeRateInput(
                        rate_date=date(2026, 4, 23),
                        base_currency="EUR",
                        quote_currency="USD",
                        rate=Decimal("123456789012345678901234567890.1234567890"),
                    ),
                ],
                provider_key="ecb",
                actor_user_id=str(USER_ID),
                source_report_id=None,
            )

        assert session.scalars(select(CurrencyExchangeRateORM)).all() == []


def test_exchange_rate_repository_rejects_rate_that_quantizes_to_zero():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    FinanceBase.metadata.create_all(engine)

    with Session(engine) as session:
        repository = SqlAlchemyExchangeRateRepository(session)
        with pytest.raises(
            ExchangeRateValidationError,
            match="rate rounds to zero after quantization:",
        ):
            repository.sync_rates(
                rates=[
                    CurrencyExchangeRateInput(
                        rate_date=date(2026, 4, 22),
                        base_currency="EUR",
                        quote_currency="USD",
                        rate=Decimal("0.00000000001"),
                    )
                ],
                provider_key="ecb",
                actor_user_id=str(USER_ID),
                source_report_id=None,
            )

        assert session.scalars(select(CurrencyExchangeRateORM)).all() == []
