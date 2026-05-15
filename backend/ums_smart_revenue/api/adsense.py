from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.finance.adsense_payments import (
    MAX_ADSENSE_PAYMENT_PAGE_SIZE,
    AdSensePaymentInput,
    AdSensePaymentLockedMonthError,
    AdSensePaymentValidationError,
    SqlAlchemyAdSensePaymentRepository,
)

router = APIRouter(prefix="/adsense", tags=["adsense"])


class AdSensePaymentRequest(BaseModel):
    month: str
    payment_name: str = Field(min_length=1)
    payment_date: date
    payment_amount: Decimal = Field(ge=0)
    payment_currency: str = Field(min_length=1)
    payment_status: str = Field(default="PAID", min_length=1)
    raw_payload: dict[str, object] = Field(default_factory=dict)

    @field_validator(
        "month",
        "payment_name",
        "payment_currency",
        "payment_status",
        mode="before",
    )
    @classmethod
    def strip_required_strings(cls, value):
        return _strip_required_string(value)


class AdSensePaymentSyncRequest(BaseModel):
    connector_key: str = Field(default="adsense", min_length=1)
    source_report_id: str | None = None
    reason: str = Field(min_length=1)
    payments: list[AdSensePaymentRequest] = Field(min_length=1, max_length=100)

    @field_validator("connector_key", "reason", mode="before")
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


def current_adsense_payment_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyAdSensePaymentRepository:
    return SqlAlchemyAdSensePaymentRepository(session)


@router.post("/sync-payments")
def sync_adsense_payments(
    payload: AdSensePaymentSyncRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyAdSensePaymentRepository,
        Depends(current_adsense_payment_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    connector_scope = AccessScope.connector(payload.connector_key)
    _require_permission(user, Permission.RUN_CONNECTOR_JOBS, connector_scope)
    if payload.connector_key != "adsense":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="connector_key must be adsense for AdSense payment sync",
        )

    try:
        payments = repository.sync_payments(
            payments=[
                AdSensePaymentInput(
                    month=payment.month,
                    payment_name=payment.payment_name,
                    payment_date=payment.payment_date,
                    payment_amount=payment.payment_amount,
                    payment_currency=payment.payment_currency,
                    payment_status=payment.payment_status,
                    raw_payload=payment.raw_payload,
                )
                for payment in payload.payments
            ],
            actor_user_id=user.user_id,
            source_report_id=payload.source_report_id,
        )
    except AdSensePaymentLockedMonthError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except AdSensePaymentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.ADSENSE_PAYMENT_SYNCED,
        entity_type="adsense_payment_batch",
        entity_id=payload.source_report_id
        or ",".join(payment.audit_entity_id for payment in payments),
        scope=connector_scope,
        reason=payload.reason,
        details={
            "connector_key": payload.connector_key,
            "source_report_id": payload.source_report_id,
            "payment_count": len(payments),
            "months": sorted({payment.month for payment in payments}),
        },
    )
    return {
        "synced_count": len(payments),
        "items": [payment.to_api() for payment in payments],
        "audit_event": audit_record_to_api(record),
    }


@router.get("/payments")
def list_adsense_payments(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyAdSensePaymentRepository,
        Depends(current_adsense_payment_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    month: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_ADSENSE_PAYMENT_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    scope = AccessScope.finance_month(month) if month is not None else AccessScope.global_scope()
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, scope)
    try:
        page = repository.list_payments(month=month, limit=limit, offset=offset)
    except AdSensePaymentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="adsense_payment_page",
        entity_id=month or "all",
        scope=scope,
        details={
            "month": month,
            "returned": len(page.items),
            "has_more": page.has_more,
        },
    )
    return {
        "items": [payment.to_api() for payment in page.items],
        "pagination": {
            "limit": page.limit,
            "offset": page.offset,
            "returned": len(page.items),
            "has_more": page.has_more,
        },
        "audit_event": audit_record_to_api(record),
    }


def _require_permission(
    user: UserPrincipal,
    permission: Permission,
    scope: AccessScope,
) -> None:
    if not has_permission(user, permission, scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
        )


def _strip_required_string(value):
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped
    return value
