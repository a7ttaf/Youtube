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
    SqlAlchemyAdSensePaymentRepository,
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
from ums_smart_revenue.finance.payment_matching import (
    PaymentMatchValidationError,
    build_monthly_payment_match_summary,
    normalize_payment_match_currency,
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
from ums_smart_revenue.org.access_index import load_org_access_index_from_session

router = APIRouter(prefix="/revenue", tags=["revenue"])
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


def current_number_explanation_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyNumberExplanationRepository:
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
    revenue_channel_ids = _authorized_channel_ids_for_permission(user, Permission.VIEW_REVENUE, org_index)
    confidence_channel_ids = _authorized_channel_ids_for_permission(user, Permission.VIEW_CONFIDENCE, org_index)
    if revenue_channel_ids == set():
        _raise_missing_permission(Permission.VIEW_REVENUE)
    if confidence_channel_ids == set():
        _raise_missing_permission(Permission.VIEW_CONFIDENCE)

    visible_channel_ids = _intersect_channel_sets(revenue_channel_ids, confidence_channel_ids)
    if visible_channel_ids == set():
        _raise_missing_permission(Permission.VIEW_REVENUE)

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
    scope = AccessScope.global_scope()
    _require_permission(user, Permission.VIEW_REVENUE, scope)
    _require_permission(user, Permission.VIEW_FINALIZED_PAYMENTS, scope)
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
    except (PaymentMatchValidationError, RevenueFactValidationError) as exc:
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
        scope=scope,
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
        scope=scope,
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
