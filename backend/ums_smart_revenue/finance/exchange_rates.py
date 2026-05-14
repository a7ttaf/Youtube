import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ums_smart_revenue.db.finance_models import CurrencyExchangeRateORM

CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")
MAX_EXCHANGE_RATE_BATCH_SIZE = 100


@dataclass(frozen=True)
class CurrencyExchangeRateInput:
    rate_date: date
    base_currency: str
    quote_currency: str
    rate: Decimal
    raw_payload: dict[str, object] | None = None


@dataclass(frozen=True)
class CurrencyExchangeRateEntry:
    id: str
    rate_date: date
    base_currency: str
    quote_currency: str
    rate: Decimal
    provider_key: str
    source_report_id: str | None
    imported_by: str | None
    raw_payload: dict[str, object] | None = None

    @property
    def audit_entity_id(self) -> str:
        return (
            f"{self.rate_date.isoformat()}:"
            f"{self.base_currency}:{self.quote_currency}:{self.provider_key}"
        )

    def to_api(self, *, include_raw_payload: bool = False) -> dict[str, object]:
        return {
            "id": self.id,
            "rate_date": self.rate_date.isoformat(),
            "base_currency": self.base_currency,
            "quote_currency": self.quote_currency,
            "rate": _decimal_to_api(self.rate),
            "provider_key": self.provider_key,
            "source_report_id": self.source_report_id,
            "imported_by": self.imported_by,
            "raw_payload": self.raw_payload if include_raw_payload else None,
        }

class ExchangeRateError(ValueError):
    pass


class ExchangeRateValidationError(ExchangeRateError):
    pass


class SqlAlchemyExchangeRateRepository:
    def __init__(self, session: Session):
        self._session = session

    def sync_rates(
        self,
        *,
        rates: list[CurrencyExchangeRateInput],
        provider_key: str,
        actor_user_id: str,
        source_report_id: str | None,
    ) -> list[CurrencyExchangeRateEntry]:
        if not rates:
            raise ExchangeRateValidationError(
                "rates must contain at least one exchange rate"
            )
        if len(rates) > MAX_EXCHANGE_RATE_BATCH_SIZE:
            raise ExchangeRateValidationError(
                f"rates cannot exceed {MAX_EXCHANGE_RATE_BATCH_SIZE} entries"
            )
        normalized_provider = _normalize_required_string(
            provider_key,
            "provider_key",
        )
        normalized_source_report_id = _normalize_optional_string(source_report_id)
        actor_uuid = _parse_uuid(actor_user_id)
        normalized_rates = [_normalize_rate_input(rate_input) for rate_input in rates]
        seen_rate_keys: set[tuple[date, str, str, str]] = set()
        for normalized in normalized_rates:
            rate_key = (
                normalized.rate_date,
                normalized.base_currency,
                normalized.quote_currency,
                normalized_provider,
            )
            if rate_key in seen_rate_keys:
                raise ExchangeRateValidationError(
                    "duplicate exchange rate in batch for "
                    f"{normalized.rate_date.isoformat()} "
                    f"{normalized.base_currency}/{normalized.quote_currency} "
                    f"from {normalized_provider}"
                )
            seen_rate_keys.add(rate_key)

        prepared_rates = [
            (normalized, _quantize_rate(normalized.rate))
            for normalized in normalized_rates
        ]

        entries: list[CurrencyExchangeRateEntry] = []
        for normalized, quantized_rate in prepared_rates:
            now = datetime.now(UTC)
            insert_statement = _dialect_insert(
                self._session.get_bind().dialect.name
            )(CurrencyExchangeRateORM).values(
                id=uuid4(),
                rate_date=normalized.rate_date,
                base_currency=normalized.base_currency,
                quote_currency=normalized.quote_currency,
                rate=quantized_rate,
                provider_key=normalized_provider,
                source_report_id=normalized_source_report_id,
                raw_payload=normalized.raw_payload or {},
                imported_by=actor_uuid,
                updated_at=now,
            )
            statement = insert_statement.on_conflict_do_update(
                index_elements=[
                    CurrencyExchangeRateORM.rate_date,
                    CurrencyExchangeRateORM.base_currency,
                    CurrencyExchangeRateORM.quote_currency,
                    CurrencyExchangeRateORM.provider_key,
                ],
                set_={
                    "rate": quantized_rate,
                    "source_report_id": normalized_source_report_id,
                    "raw_payload": normalized.raw_payload or {},
                    "imported_by": actor_uuid,
                    "updated_at": now,
                },
            ).returning(CurrencyExchangeRateORM)
            row = self._session.execute(statement).scalar_one()
            entries.append(self._to_entry(row))
        return entries

    def get_latest_rate(
        self,
        *,
        base_currency: str,
        quote_currency: str,
        as_of_date: date,
        provider_key: str | None = None,
    ) -> CurrencyExchangeRateEntry | None:
        normalized_base = _normalize_currency(base_currency, "base_currency")
        normalized_quote = _normalize_currency(quote_currency, "quote_currency")
        if normalized_base == normalized_quote:
            raise ExchangeRateValidationError(
                "base_currency and quote_currency must differ"
            )
        normalized_provider = _normalize_optional_string(provider_key)
        statement = (
            select(CurrencyExchangeRateORM)
            .where(
                CurrencyExchangeRateORM.base_currency == normalized_base,
                CurrencyExchangeRateORM.quote_currency == normalized_quote,
                CurrencyExchangeRateORM.rate_date <= as_of_date,
            )
            .order_by(
                CurrencyExchangeRateORM.rate_date.desc(),
                CurrencyExchangeRateORM.updated_at.desc(),
                CurrencyExchangeRateORM.imported_at.desc(),
                CurrencyExchangeRateORM.provider_key,
            )
            .limit(1)
        )
        if normalized_provider is not None:
            statement = statement.where(
                CurrencyExchangeRateORM.provider_key == normalized_provider
            )
        row = self._session.scalars(statement).one_or_none()
        return self._to_entry(row) if row is not None else None

    @staticmethod
    def _to_entry(row: CurrencyExchangeRateORM) -> CurrencyExchangeRateEntry:
        return CurrencyExchangeRateEntry(
            id=str(row.id),
            rate_date=row.rate_date,
            base_currency=row.base_currency,
            quote_currency=row.quote_currency,
            rate=row.rate,
            provider_key=row.provider_key,
            source_report_id=row.source_report_id,
            imported_by=str(row.imported_by) if row.imported_by else None,
            raw_payload=dict(row.raw_payload or {}),
        )


def _normalize_rate_input(
    value: CurrencyExchangeRateInput,
) -> CurrencyExchangeRateInput:
    base_currency = _normalize_currency(value.base_currency, "base_currency")
    quote_currency = _normalize_currency(value.quote_currency, "quote_currency")
    if base_currency == quote_currency:
        raise ExchangeRateValidationError(
            "base_currency and quote_currency must differ"
        )
    if not value.rate.is_finite() or value.rate <= 0:
        raise ExchangeRateValidationError("rate must be > 0")
    if value.raw_payload is not None and not isinstance(value.raw_payload, dict):
        raise ExchangeRateValidationError("raw_payload must be an object")
    return CurrencyExchangeRateInput(
        rate_date=value.rate_date,
        base_currency=base_currency,
        quote_currency=quote_currency,
        rate=value.rate,
        raw_payload=dict(value.raw_payload or {}),
    )


def _normalize_currency(value: str, field_name: str) -> str:
    normalized = _normalize_required_string(value, field_name).upper()
    if not CURRENCY_PATTERN.fullmatch(normalized):
        raise ExchangeRateValidationError(
            f"{field_name} must be a three-letter ISO currency code"
        )
    return normalized


def _normalize_required_string(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ExchangeRateValidationError(f"{field_name} must not be blank")
    return normalized


def _normalize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise ExchangeRateValidationError(
            "actor_user_id must be a valid UUID"
        ) from exc


def _quantize_rate(value: Decimal) -> Decimal:
    try:
        quantized = value.quantize(Decimal("0.0000000001"))
    except InvalidOperation as exc:
        raise ExchangeRateValidationError(
            f"rate has too many significant digits: {value}"
        ) from exc
    if quantized <= 0:
        raise ExchangeRateValidationError(
            f"rate rounds to zero after quantization: {value}"
        )
    return quantized


def _decimal_to_api(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return format(normalized, "f")
    return format(normalized, "f").rstrip("0").rstrip(".")


def _dialect_insert(dialect_name: str):
    if dialect_name == "sqlite":
        return sqlite_insert
    if dialect_name == "postgresql":
        return postgresql_insert
    raise ExchangeRateError(
        f"Unsupported database dialect for exchange-rate upsert: {dialect_name}"
    )
