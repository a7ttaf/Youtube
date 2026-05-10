from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import current_db_session, current_principal_from_headers
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_log import (
    MAX_AUDIT_LOG_PAGE_SIZE,
    AuditLogValidationError,
    SqlAlchemyAuditLogRepository,
)
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope


router = APIRouter(prefix="/audit", tags=["audit"])


def current_audit_log_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyAuditLogRepository:
    return SqlAlchemyAuditLogRepository(session)


@router.get("/events")
def list_audit_events(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyAuditLogRepository, Depends(current_audit_log_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    event_type: str | None = None,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_AUDIT_LOG_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    audit_scope = AccessScope.global_scope()
    _require_permission(user, Permission.VIEW_AUDIT_LOG, audit_scope)
    include_sensitive_details = has_permission(user, Permission.VIEW_SENSITIVE_AUDIT_PAYLOADS, audit_scope)
    try:
        page = repository.list_events(
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            exclude_event_type=AuditEventType.AUDIT_LOG_VIEWED.value,
            limit=limit,
            offset=offset,
        )
    except AuditLogValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.AUDIT_LOG_VIEWED,
        entity_type="audit_log_page",
        entity_id=event_type or "all",
        scope=audit_scope,
        details={
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "returned": len(page.items),
            "details_redacted": not include_sensitive_details,
        },
    )
    return {
        "items": [item.to_api(include_sensitive_details=include_sensitive_details) for item in page.items],
        "pagination": {
            "limit": page.limit,
            "offset": page.offset,
            "returned": len(page.items),
            "has_more": page.has_more,
        },
        "audit_event": audit_record_to_api(record),
    }


def _require_permission(user: UserPrincipal, permission: Permission, scope: AccessScope) -> None:
    if not has_permission(user, permission, scope):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {permission.value}",
        )
