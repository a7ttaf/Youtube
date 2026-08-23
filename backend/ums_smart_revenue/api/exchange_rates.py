import hashlib
import re
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.authz import require_permission
from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.finance.exchange_rates import (
    MAX_EXCHANGE_RATE_BATCH_SIZE,
    CurrencyExchangeRateInput,
    ExchangeRateValidationError,
    SqlAlchemyExchangeRateRepository,
)

router = APIRouter(prefix="/exchange-rates", tags=["exchange-rates"])

CURRENCY_CODE_PATTERN = re.compile(r"^[A-Z]{3}$")


def _normalize_currency_code(value):
    if isinstance(value, str):
        normalized = value.strip().upper()
        if not CURRENCY_CODE_PATTERN.fullmatch(normalized):
            raise ValueError("must be a 3-letter uppercase ISO 4217 code")
        return normalized
    return value


class ExchangeRateRequest(BaseModel):
    rate_date: date
    base_currency: str = Field(min_length=1)
    quote_currency: str = Field(min_length=1)
    rate: Decimal = Field(gt=0)
    raw_payload: dict[str, object] = Field(default_factory=dict)

    @field_validator("base_currency", "quote_currency", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        return _normalize_currency_code(_strip_required_string(value))

    @model_validator(mode="after")
    def check_currency_pair_distinct(self):
        if self.base_currency == self.quote_currency:
            raise ValueError("base_currency and quote_currency must differ")
        return self


class ExchangeRateSyncRequest(BaseModel):
    provider_key: str = Field(min_length=1)
    source_report_id: str | None = None
    reason: str = Field(min_length=1)
    rates: list[ExchangeRateRequest] = Field(
        min_length=1,
        max_length=MAX_EXCHANGE_RATE_BATCH_SIZE,
    )

    @field_validator("provider_key", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        return _strip_required_string(value)

    @field_validator("source_report_id", mode="before")
    @classmethod
    def strip_optional_string(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


def current_exchange_rate_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyExchangeRateRepository:
    return SqlAlchemyExchangeRateRepository(session)


@router.post("/sync")
def sync_exchange_rates(
    payload: ExchangeRateSyncRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyExchangeRateRepository,
        Depends(current_exchange_rate_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    connector_scope = AccessScope.connector(payload.provider_key)
    require_permission(user, Permission.RUN_CONNECTOR_JOBS, connector_scope)
    try:
        entries = repository.sync_rates(
            rates=[
                CurrencyExchangeRateInput(
                    rate_date=rate.rate_date,
                    base_currency=rate.base_currency,
                    quote_currency=rate.quote_currency,
                    rate=rate.rate,
                    raw_payload=rate.raw_payload,
                )
                for rate in payload.rates
            ],
            provider_key=payload.provider_key,
            actor_user_id=user.user_id,
            source_report_id=payload.source_report_id,
        )
    except ExchangeRateValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    if payload.source_report_id:
        batch_entity_id = payload.source_report_id
    else:
        # Stable digest of provider key + sorted (date, base, quote) tuples so
        # different batches from the same provider don't collide just because
        # they share a count, while remaining bounded for audit storage.
        digest = hashlib.sha256()
        digest.update(payload.provider_key.encode())
        for entry in sorted(
            entries,
            key=lambda item: (
                item.rate_date.isoformat(),
                item.base_currency,
                item.quote_currency,
            ),
        ):
            digest.update(
                (
                    f"|{entry.rate_date.isoformat()}|"
                    f"{entry.base_currency}|{entry.quote_currency}|{entry.rate}"
                ).encode()
            )
        batch_entity_id = f"batch:{payload.provider_key}:{digest.hexdigest()[:32]}"
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.EXCHANGE_RATE_SYNCED,
        entity_type="currency_exchange_rate_batch",
        entity_id=batch_entity_id,
        scope=connector_scope,
        reason=payload.reason,
        details={
            "provider_key": payload.provider_key,
            "source_report_id": payload.source_report_id,
            "rate_count": len(entries),
            "currency_pairs": sorted(
                {f"{entry.base_currency}/{entry.quote_currency}" for entry in entries}
            ),
            "rate_dates": sorted({entry.rate_date.isoformat() for entry in entries}),
        },
    )
    return {
        "synced_count": len(entries),
        "items": [entry.to_api() for entry in entries],
        "audit_event": audit_record_to_api(record),
    }


@router.get("/latest")
def get_latest_exchange_rate(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyExchangeRateRepository,
        Depends(current_exchange_rate_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    base_currency: Annotated[str, Query(min_length=1)],
    quote_currency: Annotated[str, Query(min_length=1)],
    as_of_date: date,
    provider_key: str | None = None,
) -> dict[str, object]:
    require_permission(user, Permission.VIEW_REVENUE, AccessScope.global_scope())
    try:
        normalized_base = _normalize_currency_code(base_currency)
        normalized_quote = _normalize_currency_code(quote_currency)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"currency code {exc}",
        ) from exc
    if normalized_base == normalized_quote:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="base_currency and quote_currency must differ",
        )
    try:
        entry = repository.get_latest_rate(
            base_currency=normalized_base,
            quote_currency=normalized_quote,
            as_of_date=as_of_date,
            provider_key=provider_key,
        )
    except ExchangeRateValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Exchange rate not found",
        )
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="currency_exchange_rate",
        entity_id=entry.id,
        scope=AccessScope.global_scope(),
        reason="Read latest currency exchange rate",
        details={
            "base_currency": entry.base_currency,
            "quote_currency": entry.quote_currency,
            "provider_key": entry.provider_key,
            "as_of_date": as_of_date.isoformat(),
        },
    )
    response = entry.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


def _strip_required_string(value):
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped
    return value
