import re
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
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
from ums_smart_revenue.connectors.google.adsense_management_client import (
    _validated_account_id,
)
from ums_smart_revenue.connectors.google.errors import (
    MalformedAdsenseAccountIdError,
)
from ums_smart_revenue.finance.adsense_payments import (
    MAX_ADSENSE_PAYMENT_PAGE_SIZE,
    AdSensePaymentInput,
    AdSensePaymentLockedMonthError,
    AdSensePaymentValidationError,
    SqlAlchemyAdSensePaymentRepository,
)
from ums_smart_revenue.finance.payment_status import (
    build_monthly_payment_status_summary,
)

router = APIRouter(prefix="/adsense", tags=["adsense"])

ADSENSE_MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class AdSensePaymentRequest(BaseModel):
    """Request model for one manually supplied AdSense payment row."""

    source_account_id: str = Field(min_length=1)
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
        """Trim required string fields and reject blank values."""
        return _strip_required_string(value)

    # ========================================================================
    # Purpose: Canonicalize the externally supplied AdSense publisher id at the
    #   API boundary so it matches the live pull / Google source-row convention
    #   before it reaches any finance source-of-truth write.
    # Database/ORM: Feeds AdSensePaymentInput.source_account_id -> AdSensePaymentORM.
    # Standards: Typed boundary validation; malformed ids fail closed as a
    #   Pydantic ValueError (FastAPI renders 422) prior to any DB access.
    # Blast Radius: Finance (payment identity), audit (entity id). No Neo4j.
    # Connections:
    #   - File: backend/ums_smart_revenue/connectors/google/adsense_management_client.py
    #       -> shared `_validated_account_id` normalizer (strip `accounts/`,
    #          reject blank/whitespace-padded/reserved-char ids).
    #   - File: backend/ums_smart_revenue/finance/adsense_payments.py
    #       -> AdSensePaymentInput requires a canonical source_account_id.
    # ========================================================================
    @field_validator("source_account_id", mode="before")
    @classmethod
    def canonicalize_source_account_id(cls, value):
        """Canonicalize source account ids with the shared Google validator."""
        if not isinstance(value, str):
            return value
        try:
            # FIX: defer ALL normalization to the shared canonical normalizer and
            # do NOT pre-strip. _validated_account_id strips the trusted
            # `accounts/` prefix and rejects reserved chars, AND fail-closes on a
            # blank or whitespace-padded external id (its `candidate != account_id`
            # guard). A prior value.strip() here defeated that whitespace-padding
            # rejection, silently accepting " accounts/pub-1 " as "pub-1".
            return _validated_account_id(value)
        except MalformedAdsenseAccountIdError as exc:
            raise ValueError(str(exc)) from exc


class AdSensePaymentSyncRequest(BaseModel):
    """Request model for a manual AdSense payment-sync batch."""

    connector_key: str = Field(default="adsense", min_length=1)
    source_report_id: str | None = None
    reason: str = Field(min_length=1)
    payments: list[AdSensePaymentRequest] = Field(min_length=1, max_length=100)

    @field_validator("connector_key", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        """Trim required batch strings and reject blank values."""
        return _strip_required_string(value)

    @field_validator("source_report_id", mode="before")
    @classmethod
    def strip_optional_string(cls, value):
        """Trim optional source report ids and collapse blanks to None."""
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


def current_adsense_payment_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyAdSensePaymentRepository:
    """Build the tenant-aware AdSense payment repository for a request."""
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
    """Sync a manually supplied AdSense payment batch after permission checks."""
    connector_scope = AccessScope.connector(payload.connector_key)
    require_permission(user, Permission.RUN_CONNECTOR_JOBS, connector_scope)
    if payload.connector_key != "adsense":
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="connector_key must be adsense for AdSense payment sync",
        )

    try:
        payments = repository.sync_payments(
            payments=[
                AdSensePaymentInput(
                    source_account_id=payment.source_account_id,
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
    """List AdSense payments for an authorized finance viewer."""
    normalized_month: str | None
    if month is None:
        normalized_month = None
    else:
        normalized_month = month.strip()
        if not normalized_month or not ADSENSE_MONTH_PATTERN.fullmatch(normalized_month):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="month must use YYYY-MM with a calendar month from 01 to 12",
            )
    scope = (
        AccessScope.finance_month(normalized_month)
        if normalized_month is not None
        else AccessScope.global_scope()
    )
    require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, scope)
    month = normalized_month
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


# ============================================================================
# Purpose: Read-only per-month AdSense payment paid/unpaid status breakdown
#   (month rollup + per-account, per-currency; outstanding = PENDING+UNPAID).
# Database/ORM: Reads AdSensePaymentORM via SqlAlchemyAdSensePaymentRepository;
#   no writes.
# Standards: Thin route; boundary month validation -> 422; fail-closed
#   permission check; single reused PAYMENT_VIEWED audit event; no secrets in
#   audit details.
# Blast Radius: Finance read (payment status). No finance mutation, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/payment_status.py -> pure builder.
#   - File: backend/ums_smart_revenue/auth/permissions.py -> VIEW_FINALIZED_PAYMENTS.
# ============================================================================
@router.get("/payments/status")
def get_adsense_payment_status(
    month: Annotated[str, Query(min_length=1)],
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyAdSensePaymentRepository,
        Depends(current_adsense_payment_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Return the paid/unpaid status breakdown for an authorized finance viewer."""
    normalized_month = month.strip()
    if not ADSENSE_MONTH_PATTERN.fullmatch(normalized_month):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="month must use YYYY-MM with a calendar month from 01 to 12",
        )
    scope = AccessScope.finance_month(normalized_month)
    require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, scope)
    try:
        payments = repository.list_month_payments(month=normalized_month)
    except AdSensePaymentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    summary = build_monthly_payment_status_summary(month=normalized_month, payments=payments)
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="adsense_payment_status",
        entity_id=normalized_month,
        scope=scope,
        details={
            "month": normalized_month,
            "total_payment_count": summary.total_payment_count,
            "outstanding_currency_count": len(summary.outstanding_totals),
        },
    )
    result = summary.to_api()
    result["audit_event"] = audit_record_to_api(record)
    return result


def _strip_required_string(value):
    """Trim required strings for Pydantic validators."""
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped
    return value
