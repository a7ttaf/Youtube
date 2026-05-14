import re
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import current_db_session, current_principal_from_headers
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.finance.month_close import (
    FinanceMonthCloseEntry,
    FinanceMonthCloseReadinessError,
    SqlAlchemyFinanceMonthCloseRepository,
)
from ums_smart_revenue.finance.month_close_readiness import (
    SqlAlchemyFinanceCloseReadinessService,
)

router = APIRouter(prefix="/finance-close", tags=["finance-close"])
MONTH_PATTERN = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class FinanceCloseReasonRequest(BaseModel):
    reason: str = Field(min_length=1)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        return _strip_required_string(value)


class AllocationRuleRequest(BaseModel):
    allocation_method: str = Field(min_length=1)
    rule_payload: dict[str, object] = Field(default_factory=dict)
    reason: str = Field(min_length=1)

    @field_validator("allocation_method", "reason", mode="before")
    @classmethod
    def strip_required_fields(cls, value):
        return _strip_required_string(value)


def current_finance_month_close_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyFinanceMonthCloseRepository:
    return SqlAlchemyFinanceMonthCloseRepository(session)


def current_finance_close_readiness_service(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyFinanceCloseReadinessService:
    return SqlAlchemyFinanceCloseReadinessService(session)


def _strip_required_string(value):
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped
    return value


@router.get("/{month}")
def get_finance_month_close(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyFinanceMonthCloseRepository, Depends(current_finance_month_close_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    _validate_month(month)
    scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.VIEW_REVENUE, scope)
    close = repository.get(month)
    if close is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Finance month close record not found")
    record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.MONTH_CLOSE_VIEWED,
        entity_type="finance_month_close",
        entity_id=month,
        scope=scope,
        permission_override=Permission.VIEW_REVENUE,
        details={"read_type": "summary", "status": close.status},
    )
    return close.to_api()


@router.get("/{month}/readiness")
def get_finance_close_readiness(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    readiness_service: Annotated[
        SqlAlchemyFinanceCloseReadinessService,
        Depends(current_finance_close_readiness_service),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    _validate_month(month)
    scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.LOCK_FINANCE_MONTH, scope)
    readiness = readiness_service.check_month(month)
    record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.MONTH_CLOSE_VIEWED,
        entity_type="finance_month_close",
        entity_id=month,
        scope=scope,
        permission_override=Permission.LOCK_FINANCE_MONTH,
        details={
            "read_type": "readiness",
            "ready": readiness.ready,
            "blocker_count": len(readiness.blockers),
        },
    )
    return readiness.to_api()


@router.post("/{month}/lock")
def lock_finance_month(
    month: str,
    payload: FinanceCloseReasonRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyFinanceMonthCloseRepository, Depends(current_finance_month_close_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    _validate_month(month)
    scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.LOCK_FINANCE_MONTH, scope)
    _validate_actor_user_id(user.user_id)
    try:
        close = repository.lock_month(month=month, actor_user_id=user.user_id)
    except FinanceMonthCloseReadinessError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.readiness.to_lock_error_detail(),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Finance month cannot be locked from its current state",
        ) from exc
    record = _audit_finance_close(
        audit_sink=audit_sink,
        user=user,
        event_type=AuditEventType.MONTH_LOCKED,
        month=month,
        reason=payload.reason,
        details={"status": close.status},
    )
    return _with_audit_event(close, record)


@router.post("/{month}/unlock")
def unlock_finance_month(
    month: str,
    payload: FinanceCloseReasonRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyFinanceMonthCloseRepository, Depends(current_finance_month_close_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    _validate_month(month)
    scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.UNLOCK_FINANCE_MONTH, scope)
    _validate_actor_user_id(user.user_id)
    try:
        close = repository.unlock_month(month=month, actor_user_id=user.user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Finance month cannot be unlocked from its current state",
        ) from exc
    record = _audit_finance_close(
        audit_sink=audit_sink,
        user=user,
        event_type=AuditEventType.MONTH_UNLOCKED,
        month=month,
        reason=payload.reason,
        details={"status": close.status},
    )
    return _with_audit_event(close, record)


@router.post("/{month}/allocate")
def record_allocation_rule(
    month: str,
    payload: AllocationRuleRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyFinanceMonthCloseRepository, Depends(current_finance_month_close_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    _validate_month(month)
    scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.CHANGE_ALLOCATION_RULE, scope)
    try:
        close = repository.record_allocation_rule(
            month=month,
            allocation_method=payload.allocation_method,
            rule_payload=payload.rule_payload,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Finance month allocation rule cannot be changed from its current state",
        ) from exc
    record = _audit_finance_close(
        audit_sink=audit_sink,
        user=user,
        event_type=AuditEventType.ALLOCATION_RULE_CHANGED,
        month=month,
        reason=payload.reason,
        details={"allocation_method": payload.allocation_method, "rule_payload": payload.rule_payload},
    )
    return _with_audit_event(close, record)


def _require_permission(user: UserPrincipal, permission: Permission, scope: AccessScope) -> None:
    if not has_permission(user, permission, scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
        )


def _validate_month(month: str) -> None:
    if not MONTH_PATTERN.fullmatch(month):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="month must use YYYY-MM with a calendar month from 01 to 12",
        )


def _validate_actor_user_id(user_id: str) -> None:
    try:
        UUID(user_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid request",
        ) from exc


def _audit_finance_close(
    *,
    audit_sink: AuditSink,
    user: UserPrincipal,
    event_type: AuditEventType,
    month: str,
    reason: str,
    details: dict[str, object],
):
    return record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=event_type,
        entity_type="finance_month_close",
        entity_id=month,
        scope=AccessScope.finance_month(month),
        reason=reason,
        details=details,
    )


def _with_audit_event(close: FinanceMonthCloseEntry, record) -> dict[str, object]:
    response = close.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response
