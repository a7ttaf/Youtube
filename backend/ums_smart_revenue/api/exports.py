from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ums_smart_revenue.api.channels import audit_record_to_api, current_audit_sink
from ums_smart_revenue.api.dependencies import current_db_session, current_principal_from_headers
from ums_smart_revenue.api.revenue import current_org_access_index
from ums_smart_revenue.auth.audit import AuditEventType
from ums_smart_revenue.auth.audit_service import AuditSink, record_audit_event
from ums_smart_revenue.auth.models import UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.policy import has_permission
from ums_smart_revenue.auth.scopes import AccessScope, OrgAccessIndex
from ums_smart_revenue.auth.seed import ROLE_PERMISSIONS
from ums_smart_revenue.org.channel_groups import ChannelGroupRegistryStore
from ums_smart_revenue.api.groups import current_group_registry
from ums_smart_revenue.reports.exports import (
    MAX_EXPORT_JOB_PAGE_SIZE,
    ExportJobNotFoundError,
    ExportJobValidationError,
    SqlAlchemyExportJobRepository,
    is_finance_export_type,
)


router = APIRouter(prefix="/exports", tags=["exports"])


class ExportRequest(BaseModel):
    export_type: str = Field(min_length=1)
    scope_type: str = Field(min_length=1)
    scope_id: str | None = None
    month: str = Field(min_length=1)
    currency: str = Field(default="USD", min_length=1)
    include_confidence_notes: bool = True
    include_manual_override_notes: bool = True
    reason: str = Field(min_length=1)

    @field_validator("export_type", "scope_type", "month", "currency", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value

    @field_validator("scope_id", mode="before")
    @classmethod
    def strip_optional_string(cls, value):
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


def current_export_job_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyExportJobRepository:
    return SqlAlchemyExportJobRepository(session)


@router.post("", status_code=status.HTTP_202_ACCEPTED)
def request_export(
    payload: ExportRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[ChannelGroupRegistryStore, Depends(current_group_registry)],
    repository: Annotated[SqlAlchemyExportJobRepository, Depends(current_export_job_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    try:
        _require_export_permissions(
            user=user,
            export_type=payload.export_type,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            org_index=org_index,
            group_registry=group_registry,
        )
        export_job = repository.request_export(
            export_type=payload.export_type,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            month=payload.month,
            currency=payload.currency,
            actor_user_id=user.user_id,
            include_confidence_notes=payload.include_confidence_notes,
            include_manual_override_notes=payload.include_manual_override_notes,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc).strip("'")) from exc
    except ExportJobValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.EXPORT_CREATED,
        entity_type="export_job",
        entity_id=export_job.id,
        scope=AccessScope.export(export_job.id),
        reason=payload.reason,
        details={
            "export_type": export_job.export_type,
            "scope_type": export_job.scope_type,
            "scope_id": export_job.scope_id,
            "month": export_job.month,
            "currency": export_job.currency,
            "month_lock_status": export_job.month_lock_status,
        },
    )
    response = export_job.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.get("")
def list_exports(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyExportJobRepository, Depends(current_export_job_repository)],
    limit: Annotated[int, Query(ge=1, le=MAX_EXPORT_JOB_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    try:
        page = repository.list_jobs(requested_by=user.user_id, limit=limit, offset=offset)
    except ExportJobValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return {
        "items": [item.to_api() for item in page.items],
        "pagination": {
            "limit": page.limit,
            "offset": page.offset,
            "returned": len(page.items),
            "has_more": page.has_more,
        },
    }


@router.get("/{export_id}")
def get_export(
    export_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    org_index: Annotated[OrgAccessIndex, Depends(current_org_access_index)],
    group_registry: Annotated[ChannelGroupRegistryStore, Depends(current_group_registry)],
    repository: Annotated[SqlAlchemyExportJobRepository, Depends(current_export_job_repository)],
) -> dict[str, object]:
    if not _has_any_export_permission(user):
        _raise_missing_permission(Permission.EXPORT_ANALYTICS_REPORT)
    try:
        export_job = repository.get_job(export_id)
    except ExportJobNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ExportJobValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    if export_job.requested_by != user.user_id:
        _require_export_permissions(
            user=user,
            export_type=export_job.export_type,
            scope_type=export_job.scope_type,
            scope_id=export_job.scope_id,
            org_index=org_index,
            group_registry=group_registry,
        )
    return export_job.to_api()


def _require_export_permissions(
    *,
    user: UserPrincipal,
    export_type: str,
    scope_type: str,
    scope_id: str | None,
    org_index: OrgAccessIndex,
    group_registry: ChannelGroupRegistryStore,
) -> None:
    finance_export = is_finance_export_type(export_type)
    export_permission = Permission.EXPORT_REVENUE_REPORT if finance_export else Permission.EXPORT_ANALYTICS_REPORT
    view_permission = Permission.VIEW_REVENUE if finance_export else Permission.VIEW_ANALYTICS

    if scope_type == "group":
        if not scope_id:
            raise ExportJobValidationError("scope_id is required for export scope_type: group")
        group = group_registry.get_group(scope_id)
        if group is None:
            raise KeyError(f"Group not found: {scope_id}")
        if not group.channel_ids:
            raise ExportJobValidationError("group exports require at least one channel")
        for channel_id in group.channel_ids:
            channel_scope = AccessScope.channel(channel_id)
            _require_permission(user, export_permission, channel_scope, org_index)
            _require_permission(user, view_permission, channel_scope, org_index)
        return

    target_scope = _access_scope_from_export_scope(scope_type, scope_id)
    _require_permission(user, export_permission, target_scope, org_index)
    _require_permission(user, view_permission, target_scope, org_index)


def _access_scope_from_export_scope(scope_type: str, scope_id: str | None) -> AccessScope:
    if scope_type == "global":
        if scope_id is not None:
            raise ExportJobValidationError("scope_id must be omitted for global exports")
        return AccessScope.global_scope()
    if not scope_id:
        raise ExportJobValidationError(f"scope_id is required for export scope_type: {scope_type}")
    if scope_type == "sector":
        return AccessScope.sector(scope_id)
    if scope_type == "company":
        return AccessScope.company(scope_id)
    if scope_type == "channel":
        return AccessScope.channel(scope_id)
    raise ExportJobValidationError(f"Unknown export scope_type: {scope_type}")


def _require_permission(
    user: UserPrincipal,
    permission: Permission,
    scope: AccessScope,
    org_index: OrgAccessIndex,
) -> None:
    if not has_permission(user, permission, scope, org_index):
        _raise_missing_permission(permission)


def _raise_missing_permission(permission: Permission) -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Missing permission: {permission.value}",
    )


def _has_any_export_permission(user: UserPrincipal) -> bool:
    if user.disabled:
        return False
    export_permissions = {Permission.EXPORT_ANALYTICS_REPORT, Permission.EXPORT_REVENUE_REPORT}
    for grant in user.direct_permissions:
        if grant.active and grant.permission in export_permissions:
            return True
    for assignment in user.role_assignments:
        if assignment.active and ROLE_PERMISSIONS.get(assignment.role, frozenset()) & export_permissions:
            return True
    return False
