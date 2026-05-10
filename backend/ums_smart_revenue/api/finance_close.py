from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import current_db_session, current_principal_from_headers
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.finance.month_close import FinanceMonthCloseEntry, SqlAlchemyFinanceMonthCloseRepository


router = APIRouter(prefix="/finance-close", tags=["finance-close"])


class FinanceCloseReasonRequest(BaseModel):
    reason: str = Field(min_length=1)


class AllocationRuleRequest(BaseModel):
    allocation_method: str = Field(min_length=1)
    rule_payload: dict[str, object] = Field(default_factory=dict)
    reason: str = Field(min_length=1)


def current_finance_month_close_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyFinanceMonthCloseRepository:
    return SqlAlchemyFinanceMonthCloseRepository(session)


@router.get("/{month}")
def get_finance_month_close(
    month: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyFinanceMonthCloseRepository, Depends(current_finance_month_close_repository)],
) -> dict[str, object]:
    _require_permission(user, Permission.VIEW_REVENUE, AccessScope.finance_month(month))
    return repository.get_or_create(month).to_api()


@router.post("/{month}/lock")
def lock_finance_month(
    month: str,
    payload: FinanceCloseReasonRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyFinanceMonthCloseRepository, Depends(current_finance_month_close_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.LOCK_FINANCE_MONTH, scope)
    close = repository.lock_month(month=month, actor_user_id=user.user_id)
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
    scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.UNLOCK_FINANCE_MONTH, scope)
    close = repository.unlock_month(month=month, actor_user_id=user.user_id)
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
    scope = AccessScope.finance_month(month)
    _require_permission(user, Permission.CHANGE_ALLOCATION_RULE, scope)
    close = repository.record_allocation_rule(
        month=month,
        allocation_method=payload.allocation_method,
        rule_payload=payload.rule_payload,
    )
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
