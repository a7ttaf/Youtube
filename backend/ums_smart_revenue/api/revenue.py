import re
from datetime import date
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.dependencies import (
    current_db_session,
    current_principal_from_headers,
)
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import (
    AuditRecord,
    AuditSink,
    InMemoryAuditSink,
    record_audit_event,
)
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex, ScopeType
from ums_smart_revenue.auth.seed import ROLE_PERMISSIONS
from ums_smart_revenue.auth.sql_audit_sink import SqlAlchemyAuditSink
from ums_smart_revenue.finance.adsense_payments import (
    AdSensePaymentValidationError,
    SqlAlchemyAdSensePaymentRepository,
)
from ums_smart_revenue.finance.bank_reconciliation import (
    BankReconciliationLockedMonthError,
    BankReconciliationValidationError,
    SqlAlchemyBankReconciliationRepository,
    build_month_bank_reconciliation_summary,
)
from ums_smart_revenue.finance.deduction_ingestion import (
    DeductionComponentValidationError,
    SqlAlchemyDeductionComponentRepository,
)
from ums_smart_revenue.finance.explanations import (
    NumberExplanationValidationError,
    SqlAlchemyNumberExplanationRepository,
    build_channel_month_revenue_explanation,
)
from ums_smart_revenue.finance.manual_overrides import (
    ManualOverrideConflictError,
    ManualOverrideLockedMonthError,
    ManualOverrideNotFoundError,
    ManualOverrideValidationError,
    RevenueManualOverrideEntry,
    SqlAlchemyManualOverrideRepository,
)
from ums_smart_revenue.finance.month_close import SqlAlchemyFinanceMonthCloseRepository
from ums_smart_revenue.finance.net_revenue import (
    NetRevenueValidationError,
    build_month_net_revenue_summary,
    normalize_net_revenue_currency,
)
from ums_smart_revenue.finance.payment_matching import (
    PaymentMatchValidationError,
    build_monthly_payment_match_summary,
    normalize_payment_match_currency,
)
from ums_smart_revenue.finance.recalculation import (
    RevenueRecalculationValidationError,
    build_recalculation_preview,
)
from ums_smart_revenue.finance.reconciliation import (
    build_revenue_reconciliation_issue_queue,
    build_revenue_reconciliation_preview,
)
from ums_smart_revenue.finance.revenue_facts import (
    RevenueFactEntry,
    RevenueFactLockedMonthError,
    RevenueFactNotFoundError,
    RevenueFactSourceKind,
    RevenueFactValidationError,
    SqlAlchemyRevenueFactRepository,
)
from ums_smart_revenue.finance.revenue_summary import build_adjusted_revenue_summary
from ums_smart_revenue.finance.smart_alerts import (
    build_monthly_smart_alert_summary,
)
from ums_smart_revenue.org.access_index import load_org_access_index_from_session

router = APIRouter(prefix="/revenue", tags=["revenue"])
MONTH_VALUE_PATTERN = re.compile(r"^\d{4}-\d{2}$")
_AUDIT_SINK = InMemoryAuditSink()
_REVENUE_SOURCE_KINDS_BY_CONNECTOR_KEY = {
    "youtube-cms": {RevenueFactSourceKind.YOUTUBE_CMS.value},
    "youtube_reporting": {RevenueFactSourceKind.YOUTUBE_CMS.value},
    "youtube-analytics": {RevenueFactSourceKind.YOUTUBE_ANALYTICS.value},
    "youtube_analytics": {RevenueFactSourceKind.YOUTUBE_ANALYTICS.value},
    "adsense": {RevenueFactSourceKind.ADSENSE.value},
    "manual-upload": {RevenueFactSourceKind.MANUAL_UPLOAD.value},
    "manual_upload": {RevenueFactSourceKind.MANUAL_UPLOAD.value},
    "allocation": {RevenueFactSourceKind.ALLOCATION.value},
}


class AuthorizationCheckResponse(BaseModel):
    authorized: bool
    channel_id: str
    permission: str


class RevenueFactImportRequest(BaseModel):
    month: str
    youtube_channel_id: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    connector_key: str = Field(min_length=1)
    source_report_id: str | None = None
    gross_revenue_usd: Decimal = Field(ge=0)
    net_revenue_usd: Decimal | None = Field(default=None, ge=0)
    shorts_revenue_usd: Decimal | None = Field(default=None, ge=0)
    longform_revenue_usd: Decimal | None = Field(default=None, ge=0)
    subscription_revenue_usd: Decimal | None = Field(default=None, ge=0)
    views: int = Field(default=0, ge=0)
    watch_time_minutes: Decimal = Field(default=Decimal("0"), ge=0)
    confidence_score: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    reason: str = Field(min_length=1)

    @field_validator("month", "youtube_channel_id", "source_kind", "connector_key", "reason", mode="before")
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


class ManualOverrideCreateRequest(BaseModel):
    month: str
    youtube_channel_id: str = Field(min_length=1)
    adjustment_revenue_usd: Decimal
    reason: str = Field(min_length=1)

    @field_validator("month", "youtube_channel_id", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        return _strip_required_string(value)


class ManualOverrideApprovalRequest(BaseModel):
    reason: str = Field(min_length=1)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        return _strip_required_string(value)


class BankReconciliationRecordRequest(BaseModel):
    bank_reference: str = Field(min_length=1)
    bank_received_date: date
    bank_received_amount: Decimal = Field(ge=0)
    bank_received_currency: str = Field(min_length=1)
    bank_received_amount_usd: Decimal = Field(ge=0)
    transfer_fee_usd: Decimal = Field(default=Decimal("0"), ge=0)
    fx_difference_usd: Decimal = Decimal("0")
    notes: str | None = None
    source_report_id: str | None = None
    reason: str = Field(min_length=1)

    @field_validator("bank_reference", "bank_received_currency", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        return _strip_required_string(value)

    @field_validator("notes", "source_report_id", mode="before")
    @classmethod
    def strip_optional_strings(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class RevenueRecalculationRequest(BaseModel):
    month: str
    allocation_method: str = Field(min_length=1)
    scope_type: str = Field(default="global", min_length=1)
    scope_id: str | None = None
    currency: str = Field(default="USD", min_length=1)
    dry_run: bool = True
    reason: str = Field(min_length=1)

    @field_validator(
        "month",
        "allocation_method",
        "scope_type",
        "currency",
        "reason",
        mode="before",
    )
    @classmethod
    def strip_required_strings(cls, value):
        return _strip_required_string(value)

    @field_validator("scope_id", mode="before")
    @classmethod
    def strip_optional_string(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


def current_org_access_index(
    session: Annotated[Session, Depends(current_db_session)],
) -> OrgAccessIndex:
    return load_org_access_index_from_session(session)


def current_revenue_fact_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyRevenueFactRepository:
    return SqlAlchemyRevenueFactRepository(session)


def current_adsense_payment_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyAdSensePaymentRepository:
    return SqlAlchemyAdSensePaymentRepository(session)


def current_manual_override_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyManualOverrideRepository:
    return SqlAlchemyManualOverrideRepository(session)


def current_bank_reconciliation_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyBankReconciliationRepository:
    return SqlAlchemyBankReconciliationRepository(session)


def current_finance_month_close_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyFinanceMonthCloseRepository:
    return SqlAlchemyFinanceMonthCloseRepository(session)


def current_deduction_component_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyDeductionComponentRepository:
    """Build the tenant-aware deduction-component repository for a request."""
    return SqlAlchemyDeductionComponentRepository(session)


def current_number_explanation_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyNumberExplanationRepository:
    """Build the tenant-aware number-explanation repository for a request."""
    return SqlAlchemyNumberExplanationRepository(session)


def current_revenue_audit_sink() -> InMemoryAuditSink:
    return _AUDIT_SINK


def sql_revenue_audit_sink_from_session(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyAuditSink:
    return SqlAlchemyAuditSink(session)


@router.get("/channels/{channel_id}/authorization-check", response_model=AuthorizationCheckResponse)
def check_channel_revenue_authorization(
    channel_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
) -> AuthorizationCheckResponse:
    target_scope = AccessScope.channel(channel_id)
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    return AuthorizationCheckResponse(
        authorized=True,
        channel_id=channel_id,
        permission=Permission.VIEW_REVENUE.value,
    )


@router.post("/recalculate")
def request_revenue_recalculation(
    payload: RevenueRecalculationRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    override_repository: Annotated[
        SqlAlchemyManualOverrideRepository,
        Depends(current_manual_override_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    target_scope, channel_ids = _revenue_read_scope_to_channel_ids(
        scope_type=payload.scope_type,
        scope_id=payload.scope_id,
        org_index=org_index,
    )
    month_scope = AccessScope.finance_month(payload.month)
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    _require_permission(user, Permission.CHANGE_ALLOCATION_RULE, month_scope)
    if not payload.dry_run:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="committed recalculation writes are not implemented; use dry_run=true",
        )
    try:
        facts = revenue_repository.list_month_facts(
            month=payload.month,
            youtube_channel_ids=channel_ids,
        )
        overrides = override_repository.list_month_overrides(
            month=payload.month,
            youtube_channel_ids=channel_ids,
        )
        preview = build_recalculation_preview(
            month=payload.month,
            allocation_method=payload.allocation_method,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            currency=payload.currency,
            dry_run=payload.dry_run,
            facts=facts,
            manual_overrides=overrides,
        )
    except (
        ManualOverrideValidationError,
        RevenueFactValidationError,
        RevenueRecalculationValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    summary = preview.source_summary.to_api()
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.RECALCULATION_REQUESTED,
        entity_type="revenue_recalculation_preview",
        entity_id=f"{preview.month}:{preview.allocation_method}",
        scope=month_scope,
        reason=payload.reason,
        details={
            "status": preview.status,
            "allocation_method": preview.allocation_method,
            "scope_type": preview.scope_type,
            "scope_id": preview.scope_id,
            "revenue_fact_count": summary["revenue_fact_count"],
            "source_channel_count": summary["source_channel_count"],
            "write_status": preview.write_status,
        },
    )
    response = preview.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.post("/facts", status_code=status.HTTP_201_CREATED)
def import_revenue_fact(
    payload: RevenueFactImportRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyRevenueFactRepository, Depends(current_revenue_fact_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    connector_scope = AccessScope.connector(payload.connector_key)
    _require_permission(user, Permission.RUN_CONNECTOR_JOBS, connector_scope)
    try:
        source_kind = _validate_connector_source_kind(payload.connector_key, payload.source_kind)
        fact = repository.record_fact(
            month=payload.month,
            youtube_channel_id=payload.youtube_channel_id,
            source_kind=source_kind,
            source_report_id=payload.source_report_id,
            gross_revenue_usd=payload.gross_revenue_usd,
            net_revenue_usd=payload.net_revenue_usd,
            shorts_revenue_usd=payload.shorts_revenue_usd,
            longform_revenue_usd=payload.longform_revenue_usd,
            subscription_revenue_usd=payload.subscription_revenue_usd,
            views=payload.views,
            watch_time_minutes=payload.watch_time_minutes,
            confidence_score=payload.confidence_score,
            actor_user_id=user.user_id,
        )
    except RevenueFactLockedMonthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RevenueFactValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REPORT_IMPORTED,
        entity_type="monthly_channel_revenue_fact",
        entity_id=fact.audit_entity_id,
        scope=connector_scope,
        reason=payload.reason,
        details={
            "connector_key": payload.connector_key,
            "source_report_id": payload.source_report_id,
            "gross_revenue_usd": fact.to_api()["gross_revenue_usd"],
            "shorts_revenue_usd": fact.to_api()["shorts_revenue_usd"],
            "longform_revenue_usd": fact.to_api()["longform_revenue_usd"],
            "subscription_revenue_usd": fact.to_api()[
                "subscription_revenue_usd"
            ],
        },
    )
    return _with_audit_event(fact, record)


@router.get("/channels/{channel_id}/months/{month}/facts")
def list_channel_month_revenue_facts(
    channel_id: str,
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    repository: Annotated[SqlAlchemyRevenueFactRepository, Depends(current_revenue_fact_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    target_scope = AccessScope.channel(channel_id)
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    try:
        facts = repository.list_channel_month_facts(month=month, youtube_channel_id=channel_id)
    except RevenueFactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RevenueFactValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_channel_revenue_fact",
        entity_id=f"{channel_id}:{month}",
        scope=target_scope,
        details={"fact_count": len(facts)},
    )
    return {
        "month": month,
        "youtube_channel_id": channel_id,
        "facts": [fact.to_api() for fact in facts],
        "audit_event": audit_record_to_api(record),
    }


@router.get("/channels/{channel_id}/months/{month}/reconciliation-preview")
def get_channel_month_reconciliation_preview(
    channel_id: str,
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    repository: Annotated[SqlAlchemyRevenueFactRepository, Depends(current_revenue_fact_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    target_scope = AccessScope.channel(channel_id)
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    _require_permission(user, Permission.VIEW_CONFIDENCE, target_scope, org_index)
    try:
        facts = repository.list_channel_month_facts(month=month, youtube_channel_id=channel_id)
    except RevenueFactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RevenueFactValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    preview = build_revenue_reconciliation_preview(facts, month=month, youtube_channel_id=channel_id)
    record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="revenue_reconciliation_preview",
        entity_id=f"{channel_id}:{month}",
        scope=target_scope,
        details={
            "status": preview.status,
            "issue_count": len(preview.issues),
            "compared_source_count": preview.compared_source_count,
        },
    )
    return preview.to_api()


@router.get("/months/{month}/reconciliation-issues")
def list_month_reconciliation_issues(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    repository: Annotated[SqlAlchemyRevenueFactRepository, Depends(current_revenue_fact_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    limit: int = Query(default=100, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    # Reject only when the caller has no relevant grant at all; a caller whose
    # scoped grant currently maps to zero channels (e.g. sector/company with
    # no active mapping) should see an empty queue, not 403.
    if user.disabled or not _granted_scopes_for_permission(user, Permission.VIEW_REVENUE):
        _raise_missing_permission(Permission.VIEW_REVENUE)
    if not _granted_scopes_for_permission(user, Permission.VIEW_CONFIDENCE):
        _raise_missing_permission(Permission.VIEW_CONFIDENCE)

    revenue_channel_ids = _authorized_channel_ids_for_permission(user, Permission.VIEW_REVENUE, org_index)
    confidence_channel_ids = _authorized_channel_ids_for_permission(user, Permission.VIEW_CONFIDENCE, org_index)
    visible_channel_ids = _intersect_channel_sets(revenue_channel_ids, confidence_channel_ids)
    if visible_channel_ids is not None and not visible_channel_ids:
        record_audit_event(
            sink=audit_sink,
            actor=user,
            event_type=AuditEventType.REVENUE_VIEWED,
            entity_type="revenue_reconciliation_issue_queue",
            entity_id=month,
            scope=AccessScope.finance_month(month),
            details={
                "issue_count": 0,
                "page_channel_count": 0,
                "page_fact_count": 0,
                "has_more": False,
                "scoped_channel_count": 0,
            },
        )
        empty_queue = build_revenue_reconciliation_issue_queue([], month=month)
        response = empty_queue.to_api()
        response["pagination"] = {
            "limit": limit,
            "offset": offset,
            "next_offset": None,
            "has_more": False,
        }
        return response

    try:
        page_size = limit + 1
        page_channel_ids = repository.list_month_channel_ids(
            month=month,
            youtube_channel_ids=visible_channel_ids,
            limit=page_size,
            offset=offset,
        )
        channel_ids_for_page = page_channel_ids[:limit]
        facts = repository.list_month_facts(
            month=month,
            youtube_channel_ids=set(channel_ids_for_page),
        )
    except RevenueFactValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    has_more = len(page_channel_ids) > limit
    queue = build_revenue_reconciliation_issue_queue(facts, month=month)
    record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="revenue_reconciliation_issue_queue",
        entity_id=month,
        scope=AccessScope.finance_month(month),
        details={
            "issue_count": len(queue.items),
            "page_channel_count": len(channel_ids_for_page),
            "page_fact_count": len(facts),
            "has_more": has_more,
            "scoped_channel_count": len(visible_channel_ids) if visible_channel_ids is not None else None,
        },
    )
    response = queue.to_api()
    response["pagination"] = {
        "limit": limit,
        "offset": offset,
        "next_offset": offset + limit if has_more else None,
        "has_more": has_more,
    }
    return response


@router.get("/months/{month}/payment-match")
def get_month_payment_match(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    payment_repository: Annotated[
        SqlAlchemyAdSensePaymentRepository,
        Depends(current_adsense_payment_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    currency: Annotated[str, Query(min_length=1)] = "USD",
) -> dict[str, object]:
    revenue_scope = AccessScope.global_scope()
    payment_scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_REVENUE, revenue_scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, payment_scope)
    try:
        normalized_currency = normalize_payment_match_currency(currency)
        facts = revenue_repository.list_month_facts(month=month)
        payments = payment_repository.list_month_payments(month=month)
        summary = build_monthly_payment_match_summary(
            month=month,
            facts=facts,
            payments=payments,
            currency=normalized_currency,
        )
    except (
        AdSensePaymentValidationError,
        PaymentMatchValidationError,
        RevenueFactValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    summary_api = summary.to_api()
    revenue_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_payment_match",
        entity_id=month,
        scope=revenue_scope,
        details={
            "status": summary.status,
            "youtube_revenue_total_usd": summary_api["youtube_revenue_total_usd"],
        },
    )
    payment_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="monthly_payment_match",
        entity_id=month,
        scope=payment_scope,
        details={
            "status": summary.status,
            "adsense_paid_amount": summary_api["adsense_paid_amount"],
            "paid_payment_count": summary.paid_payment_count,
        },
    )
    summary_api["audit_events"] = [
        audit_record_to_api(revenue_record),
        audit_record_to_api(payment_record),
    ]
    return summary_api


@router.get("/months/{month}/smart-alerts")
def get_month_smart_alerts(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    payment_repository: Annotated[
        SqlAlchemyAdSensePaymentRepository,
        Depends(current_adsense_payment_repository),
    ],
    bank_repository: Annotated[
        SqlAlchemyBankReconciliationRepository,
        Depends(current_bank_reconciliation_repository),
    ],
    override_repository: Annotated[
        SqlAlchemyManualOverrideRepository,
        Depends(current_manual_override_repository),
    ],
    close_repository: Annotated[
        SqlAlchemyFinanceMonthCloseRepository,
        Depends(current_finance_month_close_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    global_scope = AccessScope.global_scope()
    month_scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_REVENUE, global_scope)
    _require_permission(user, Permission.VIEW_CONFIDENCE, global_scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, month_scope)
    _require_permission(user, Permission.VIEW_BANK_RECONCILIATION, month_scope)
    try:
        facts = revenue_repository.list_month_facts(month=month)
        previous_facts = revenue_repository.list_month_facts(
            month=_previous_month(month)
        )
        payments = payment_repository.list_month_payments(month=month)
        bank_entries = bank_repository.list_month_entries(month=month)
        manual_overrides = override_repository.list_month_overrides(month=month)
        close = close_repository.get(month)
        payment_match = build_monthly_payment_match_summary(
            month=month,
            facts=facts,
            payments=payments,
        )
        bank_reconciliation = build_month_bank_reconciliation_summary(
            month=month,
            payments=payments,
            bank_entries=bank_entries,
        )
    except (
        AdSensePaymentValidationError,
        BankReconciliationValidationError,
        ManualOverrideValidationError,
        PaymentMatchValidationError,
        RevenueFactValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    summary = build_monthly_smart_alert_summary(
        month=month,
        payment_match=payment_match,
        bank_reconciliation=bank_reconciliation,
        close_status=close.status if close else "OPEN",
        manual_overrides=manual_overrides,
        current_revenue_facts=facts,
        previous_revenue_facts=previous_facts,
    )
    summary_api = summary.to_api()
    audit_details = {
        "status": summary.status,
        "alert_count": len(summary.alerts),
        "highest_severity": summary.highest_severity,
    }
    revenue_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_smart_alerts",
        entity_id=month,
        scope=global_scope,
        details=audit_details,
    )
    payment_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="monthly_smart_alerts",
        entity_id=month,
        scope=month_scope,
        details=audit_details,
    )
    bank_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.BANK_RECONCILIATION_VIEWED,
        entity_type="monthly_smart_alerts",
        entity_id=month,
        scope=month_scope,
        details=audit_details,
    )
    summary_api["audit_events"] = [
        audit_record_to_api(revenue_record),
        audit_record_to_api(payment_record),
        audit_record_to_api(bank_record),
    ]
    return summary_api


# ============================================================================
# Purpose: Read-only per-month deduction-evidence view, grouped by scope
#   (CHANNEL/ACCOUNT/PAYMENT). Surfaces the typed components PR-A ingested; never
#   writes, never triggers ingestion, never returns raw_payload.
# Database/ORM: Reads deduction_components via SqlAlchemyDeductionComponentRepository.
# Standards: smart-alerts four-permission auth; sensitive-view audit (revenue +
#   payment + bank); month validation -> 422; offset/limit pagination.
# Blast Radius: Finance read (deduction evidence). No finance mutation, no Neo4j.
# Connections:
#   - File: backend/ums_smart_revenue/finance/deduction_ingestion.py -> repo.
#   - File: backend/ums_smart_revenue/finance/deduction_components.py -> to_api().
# ============================================================================
@router.get("/months/{month}/deduction-components")
def get_month_deduction_components(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyDeductionComponentRepository,
        Depends(current_deduction_component_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    component_kind: str | None = None,
    scope_kind: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    """Return one month's deduction evidence grouped by scope for finance review."""
    global_scope = AccessScope.global_scope()
    month_scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_REVENUE, global_scope)
    _require_permission(user, Permission.VIEW_CONFIDENCE, global_scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, month_scope)
    _require_permission(user, Permission.VIEW_BANK_RECONCILIATION, month_scope)
    try:
        page = repository.list_month_components_page(
            month=month,
            component_kind=component_kind,
            scope_kind=scope_kind,
            limit=limit,
            offset=offset,
        )
    except DeductionComponentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    grouped: dict[str, list[dict[str, object]]] = {}
    for component in page.components:
        grouped.setdefault(component.scope_kind, []).append(component.to_api())
    scopes = [
        {"scope_kind": kind, "components": grouped[kind]}
        for kind in sorted(grouped)
    ]

    audit_details = {
        "month": month,
        "total_count": page.total_count,
        "returned_count": len(page.components),
    }
    revenue_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_deduction_components",
        entity_id=month,
        scope=global_scope,
        details=audit_details,
    )
    payment_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="monthly_deduction_components",
        entity_id=month,
        scope=month_scope,
        details=audit_details,
    )
    bank_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.BANK_RECONCILIATION_VIEWED,
        entity_type="monthly_deduction_components",
        entity_id=month,
        scope=month_scope,
        details=audit_details,
    )
    has_more = offset + len(page.components) < page.total_count
    return {
        "month": month,
        "total_count": page.total_count,
        "returned_count": len(page.components),
        "scopes": scopes,
        "pagination": {
            "limit": limit,
            "offset": offset,
            "next_offset": (offset + limit) if has_more else None,
            "has_more": has_more,
        },
        "audit_events": [
            audit_record_to_api(revenue_record),
            audit_record_to_api(payment_record),
            audit_record_to_api(bank_record),
        ],
    }


@router.get("/months/{month}/net-revenue")
def get_month_net_revenue(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    revenue_repository: Annotated[
        SqlAlchemyRevenueFactRepository,
        Depends(current_revenue_fact_repository),
    ],
    override_repository: Annotated[
        SqlAlchemyManualOverrideRepository,
        Depends(current_manual_override_repository),
    ],
    deduction_component_repository: Annotated[
        SqlAlchemyDeductionComponentRepository,
        Depends(current_deduction_component_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    scope_type: Annotated[str, Query(min_length=1)] = "global",
    scope_id: str | None = None,
    currency: Annotated[str, Query(min_length=1)] = "USD",
) -> dict[str, object]:
    """Return the scoped monthly net-revenue summary for an authorized finance viewer."""
    target_scope, channel_ids = _revenue_read_scope_to_channel_ids(
        scope_type=scope_type,
        scope_id=scope_id,
        org_index=org_index,
    )
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    _require_permission(user, Permission.VIEW_CONFIDENCE, target_scope, org_index)
    try:
        normalized_currency = normalize_net_revenue_currency(currency)
        facts = revenue_repository.list_month_facts(
            month=month,
            youtube_channel_ids=channel_ids,
        )
        overrides = override_repository.list_month_overrides(
            month=month,
            youtube_channel_ids=channel_ids,
        )
        deduction_components = deduction_component_repository.list_month_components(
            month=month,
        )
        summary = build_month_net_revenue_summary(
            month=month,
            facts=facts,
            manual_overrides=overrides,
            deduction_components=deduction_components,
        )
    except (
        DeductionComponentValidationError,
        ManualOverrideValidationError,
        NetRevenueValidationError,
        RevenueFactValidationError,
    ) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    summary_api = summary.to_api()
    summary_api["currency"] = normalized_currency
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="monthly_net_revenue_summary",
        entity_id=f"{month}:{scope_type}:{scope_id or 'global'}",
        scope=target_scope,
        details={
            "status": summary.status,
            "channel_count": summary.channel_count,
            "calculated_channel_count": summary.calculated_channel_count,
            "missing_net_source_count": summary.missing_net_source_count,
        },
    )
    summary_api["audit_event"] = audit_record_to_api(record)
    return summary_api


@router.post(
    "/months/{month}/bank-reconciliation",
    status_code=status.HTTP_201_CREATED,
)
def record_month_bank_reconciliation(
    month: str,
    payload: BankReconciliationRecordRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyBankReconciliationRepository,
        Depends(current_bank_reconciliation_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.MANAGE_BANK_RECONCILIATION, scope)
    try:
        entry = repository.record_entry(
            month=month,
            bank_reference=payload.bank_reference,
            bank_received_date=payload.bank_received_date,
            bank_received_amount=payload.bank_received_amount,
            bank_received_currency=payload.bank_received_currency,
            bank_received_amount_usd=payload.bank_received_amount_usd,
            transfer_fee_usd=payload.transfer_fee_usd,
            fx_difference_usd=payload.fx_difference_usd,
            notes=payload.notes,
            source_report_id=payload.source_report_id,
            actor_user_id=user.user_id,
        )
    except BankReconciliationLockedMonthError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
    except BankReconciliationValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.BANK_RECONCILIATION_RECORDED,
        entity_type="bank_reconciliation_entry",
        entity_id=entry.id,
        scope=AccessScope.finance_month(month),
        reason=payload.reason,
        details={
            "bank_reference": entry.bank_reference,
            "bank_received_amount_usd": entry.to_api()[
                "bank_received_amount_usd"
            ],
        },
    )
    response = entry.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.get("/months/{month}/bank-reconciliation")
def get_month_bank_reconciliation(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    payment_repository: Annotated[
        SqlAlchemyAdSensePaymentRepository,
        Depends(current_adsense_payment_repository),
    ],
    bank_repository: Annotated[
        SqlAlchemyBankReconciliationRepository,
        Depends(current_bank_reconciliation_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_BANK_RECONCILIATION, scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, scope)
    try:
        payments = payment_repository.list_month_payments(month=month)
        entries = bank_repository.list_month_entries(month=month)
    except (AdSensePaymentValidationError, BankReconciliationValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    summary = build_month_bank_reconciliation_summary(
        month=month,
        payments=payments,
        bank_entries=entries,
    )
    summary_api = summary.to_api()
    bank_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.BANK_RECONCILIATION_VIEWED,
        entity_type="month_bank_reconciliation",
        entity_id=month,
        scope=AccessScope.finance_month(month),
        details={
            "status": summary.status,
            "entry_count": summary.entry_count,
            "bank_received_amount_usd": summary_api[
                "bank_received_amount_usd"
            ],
        },
    )
    payment_record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.PAYMENT_VIEWED,
        entity_type="month_bank_reconciliation",
        entity_id=month,
        scope=scope,
        details={
            "status": summary.status,
            "paid_payment_count": summary.paid_payment_count,
            "adsense_paid_amount_usd": summary_api["adsense_paid_amount_usd"],
        },
    )
    summary_api["audit_events"] = [
        audit_record_to_api(bank_record),
        audit_record_to_api(payment_record),
    ]
    return summary_api


@router.post("/channels/{channel_id}/months/{month}/explain")
def explain_channel_month_revenue_metric(
    channel_id: str,
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    revenue_repository: Annotated[SqlAlchemyRevenueFactRepository, Depends(current_revenue_fact_repository)],
    override_repository: Annotated[SqlAlchemyManualOverrideRepository, Depends(current_manual_override_repository)],
    explanation_repository: Annotated[
        SqlAlchemyNumberExplanationRepository,
        Depends(current_number_explanation_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
    metric: str = "adjusted_gross_revenue_usd",
) -> dict[str, object]:
    target_scope = AccessScope.channel(channel_id)
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    _require_permission(user, Permission.VIEW_CONFIDENCE, target_scope, org_index)
    try:
        facts = revenue_repository.list_channel_month_facts(month=month, youtube_channel_id=channel_id)
        overrides = override_repository.list_channel_month_overrides(month=month, youtube_channel_id=channel_id)
        explanation = build_channel_month_revenue_explanation(
            facts=facts,
            manual_overrides=overrides,
            month=month,
            youtube_channel_id=channel_id,
            metric=metric,
        )
        explanation_repository.record_explanation(explanation)
    except RevenueFactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (ManualOverrideValidationError, NumberExplanationValidationError, RevenueFactValidationError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="number_explanation",
        entity_id=f"{channel_id}:{month}:{metric}",
        scope=target_scope,
        details={"metric": metric, "warning_count": len(explanation.warnings)},
    )
    response = explanation.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.post("/manual-overrides", status_code=status.HTTP_201_CREATED)
def create_manual_override(
    payload: ManualOverrideCreateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    repository: Annotated[SqlAlchemyManualOverrideRepository, Depends(current_manual_override_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    target_scope = AccessScope.channel(payload.youtube_channel_id)
    _require_permission(user, Permission.CREATE_MANUAL_OVERRIDE, target_scope, org_index)
    try:
        override = repository.create_override(
            month=payload.month,
            youtube_channel_id=payload.youtube_channel_id,
            adjustment_revenue_usd=payload.adjustment_revenue_usd,
            reason=payload.reason,
            actor_user_id=user.user_id,
        )
    except ManualOverrideLockedMonthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ManualOverrideValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.MANUAL_OVERRIDE_CREATED,
        entity_type="revenue_manual_override",
        entity_id=override.id,
        scope=target_scope,
        reason=payload.reason,
        details={
            "month": override.month,
            "youtube_channel_id": override.youtube_channel_id,
            "adjustment_revenue_usd": override.to_api()["adjustment_revenue_usd"],
        },
    )
    return _manual_override_with_audit_event(override, record)


@router.post("/manual-overrides/{manual_override_id}/approve")
def approve_manual_override(
    manual_override_id: str,
    payload: ManualOverrideApprovalRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    repository: Annotated[SqlAlchemyManualOverrideRepository, Depends(current_manual_override_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    if _authorized_channel_ids_for_permission(user, Permission.APPROVE_MANUAL_OVERRIDE, org_index) == set():
        _raise_missing_permission(Permission.APPROVE_MANUAL_OVERRIDE)

    try:
        target_channel_id = repository.get_override_channel_id(manual_override_id)
    except ManualOverrideValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    if target_channel_id is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Manual override not found")

    target_scope = AccessScope.channel(target_channel_id)
    if not has_permission(user, Permission.APPROVE_MANUAL_OVERRIDE, target_scope, org_index):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Manual override not found")
    try:
        override = repository.approve_override(
            override_id=manual_override_id,
            actor_user_id=user.user_id,
            reason=payload.reason,
        )
    except ManualOverrideNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ManualOverrideConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ManualOverrideLockedMonthError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ManualOverrideValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.MANUAL_OVERRIDE_APPROVED,
        entity_type="revenue_manual_override",
        entity_id=override.id,
        scope=target_scope,
        reason=payload.reason,
        details={
            "month": override.month,
            "youtube_channel_id": override.youtube_channel_id,
            "adjustment_revenue_usd": override.to_api()["adjustment_revenue_usd"],
        },
    )
    return _manual_override_with_audit_event(override, record)


@router.get("/channels/{channel_id}/months/{month}/summary")
def get_channel_month_revenue_summary(
    channel_id: str,
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    revenue_repository: Annotated[SqlAlchemyRevenueFactRepository, Depends(current_revenue_fact_repository)],
    override_repository: Annotated[SqlAlchemyManualOverrideRepository, Depends(current_manual_override_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_revenue_audit_sink)],
) -> dict[str, object]:
    target_scope = AccessScope.channel(channel_id)
    _require_permission(user, Permission.VIEW_REVENUE, target_scope, org_index)
    try:
        facts = revenue_repository.list_channel_month_facts(month=month, youtube_channel_id=channel_id)
        overrides = override_repository.list_channel_month_overrides(month=month, youtube_channel_id=channel_id)
    except RevenueFactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except (ManualOverrideValidationError, RevenueFactValidationError) as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    summary = build_adjusted_revenue_summary(
        facts=facts,
        manual_overrides=overrides,
        month=month,
        youtube_channel_id=channel_id,
    )
    record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REVENUE_VIEWED,
        entity_type="adjusted_revenue_summary",
        entity_id=f"{channel_id}:{month}",
        scope=target_scope,
        details={
            "status": summary.status,
            "approved_manual_override_count": summary.approved_manual_override_count,
            "pending_manual_override_count": summary.pending_manual_override_count,
        },
    )
    return summary.to_api()


def _require_permission(
    user: UserPrincipal,
    permission: Permission,
    scope: AccessScope,
    org_index: OrgAccessIndex | None = None,
) -> None:
    if not has_permission(user, permission, scope, org_index):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
        )


def _raise_missing_permission(permission: Permission) -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Missing permission: {permission.value}",
    )


def _authorized_channel_ids_for_permission(
    user: UserPrincipal,
    permission: Permission,
    org_index: OrgAccessIndex,
) -> set[str] | None:
    if user.disabled:
        return set()

    channel_ids: set[str] = set()
    for scope in _granted_scopes_for_permission(user, permission):
        if scope.type == ScopeType.GLOBAL:
            return None
        if scope.type == ScopeType.CHANNEL and scope.id is not None:
            channel_ids.add(scope.id)
        elif scope.type == ScopeType.COMPANY and scope.id is not None:
            channel_ids.update(
                channel_id for channel_id, company_id in org_index.channel_company.items() if company_id == scope.id
            )
        elif scope.type == ScopeType.SECTOR and scope.id is not None:
            channel_ids.update(
                channel_id for channel_id, sector_id in org_index.channel_sector.items() if sector_id == scope.id
            )
    return channel_ids


def _granted_scopes_for_permission(user: UserPrincipal, permission: Permission) -> tuple[AccessScope, ...]:
    scopes: list[AccessScope] = []
    for grant in user.direct_permissions:
        if grant.active and grant.permission == permission:
            scopes.append(grant.scope)
    for assignment in user.role_assignments:
        if assignment.active and permission in ROLE_PERMISSIONS.get(assignment.role, frozenset()):
            scopes.append(assignment.scope)
    return tuple(scopes)


def _intersect_channel_sets(left: set[str] | None, right: set[str] | None) -> set[str] | None:
    if left is None:
        return right
    if right is None:
        return left
    return left & right


def _previous_month(month: str) -> str:
    if not MONTH_VALUE_PATTERN.fullmatch(month):
        raise RevenueFactValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )
    try:
        year_value, month_value = month.split("-", maxsplit=1)
        year = int(year_value)
        month_number = int(month_value)
    except ValueError as exc:
        raise RevenueFactValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        ) from exc
    if year == 0 or month_number < 1 or month_number > 12:
        raise RevenueFactValidationError(
            "month must use YYYY-MM with a calendar month from 01 to 12"
        )
    if month_number == 1:
        if year == 1:
            raise RevenueFactValidationError(
                "month must use YYYY-MM with a calendar month from 01 to 12"
            )
        return f"{year - 1:04d}-12"
    return f"{year:04d}-{month_number - 1:02d}"


def _revenue_read_scope_to_channel_ids(
    *,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
) -> tuple[AccessScope, set[str] | None]:
    normalized_scope_type = scope_type.strip()
    normalized_scope_id = (
        scope_id.strip() if isinstance(scope_id, str) else scope_id
    )
    if normalized_scope_type == "global":
        if normalized_scope_id:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="scope_id must be omitted for global revenue reads",
            )
        return AccessScope.global_scope(), None
    if not normalized_scope_id:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(
                "scope_id is required for revenue scope_type: "
                f"{normalized_scope_type}"
            ),
        )
    if normalized_scope_type == "sector":
        return (
            AccessScope.sector(normalized_scope_id),
            {
                channel_id
                for channel_id, sector_id in org_index.channel_sector.items()
                if sector_id == normalized_scope_id
            },
        )
    if normalized_scope_type == "company":
        return (
            AccessScope.company(normalized_scope_id),
            {
                channel_id
                for channel_id, company_id in org_index.channel_company.items()
                if company_id == normalized_scope_id
            },
        )
    if normalized_scope_type == "channel":
        return AccessScope.channel(normalized_scope_id), {normalized_scope_id}
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=f"Unknown revenue scope_type: {scope_type}",
    )


def audit_record_to_api(record: AuditRecord) -> dict[str, object]:
    return {
        "event_type": record.event_type,
        "entity_type": record.entity_type,
        "entity_id": record.entity_id,
        "scope_type": record.scope_type,
        "scope_id": record.scope_id,
        "reason": record.reason,
        "sensitive": record.sensitive,
    }


def _with_audit_event(fact: RevenueFactEntry, record: AuditRecord) -> dict[str, object]:
    response = fact.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


def _manual_override_with_audit_event(
    override: RevenueManualOverrideEntry,
    record: AuditRecord,
) -> dict[str, object]:
    response = override.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


def _strip_required_string(value):
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped
    return value


def _validate_connector_source_kind(connector_key: str, source_kind: str) -> str:
    try:
        normalized_source_kind = RevenueFactSourceKind(source_kind).value
    except ValueError as exc:
        raise RevenueFactValidationError(f"Unknown revenue fact source_kind: {source_kind}") from exc

    allowed_source_kinds = _REVENUE_SOURCE_KINDS_BY_CONNECTOR_KEY.get(connector_key)
    if allowed_source_kinds is None:
        raise RevenueFactValidationError(f"Unknown revenue fact connector_key: {connector_key}")
    if normalized_source_kind not in allowed_source_kinds:
        raise RevenueFactValidationError(
            f"connector_key {connector_key} cannot import source_kind {normalized_source_kind}"
        )
    return normalized_source_kind
