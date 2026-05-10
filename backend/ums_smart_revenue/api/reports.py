from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
from ums_smart_revenue.auth.seed import ROLE_PERMISSIONS
from ums_smart_revenue.reports.raw_files import (
    MAX_RAW_REPORT_FILE_PAGE_SIZE,
    RawReportFileConflictError,
    RawReportFileNotFoundError,
    RawReportFileValidationError,
    SqlAlchemyRawReportFileRepository,
)


router = APIRouter(prefix="/reports", tags=["reports"])


class RawReportFileRegisterRequest(BaseModel):
    source: str = Field(min_length=1)
    report_type: str = Field(min_length=1)
    report_month: str = Field(min_length=1)
    storage_uri: str = Field(min_length=1)
    checksum: str = Field(min_length=1)
    parse_status: str = Field(default="DOWNLOADED", min_length=1)
    reason: str = Field(min_length=1)

    @field_validator(
        "source",
        "report_type",
        "report_month",
        "storage_uri",
        "checksum",
        "parse_status",
        "reason",
        mode="before",
    )
    @classmethod
    def strip_required_strings(cls, value):
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value


def current_raw_report_file_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyRawReportFileRepository:
    return SqlAlchemyRawReportFileRepository(session)


@router.post("/raw-files", status_code=status.HTTP_201_CREATED)
def register_raw_report_file(
    payload: RawReportFileRegisterRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyRawReportFileRepository, Depends(current_raw_report_file_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    connector_scope = AccessScope.connector(payload.source)
    _require_permission(user, Permission.RUN_CONNECTOR_JOBS, connector_scope)
    try:
        raw_file = repository.register_file(
            source=payload.source,
            report_type=payload.report_type,
            report_month=payload.report_month,
            storage_uri=payload.storage_uri,
            checksum=payload.checksum,
            parse_status=payload.parse_status,
            actor_user_id=user.user_id,
        )
    except RawReportFileConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except RawReportFileValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.REPORT_IMPORTED,
        entity_type="raw_report_file",
        entity_id=raw_file.id,
        scope=connector_scope,
        reason=payload.reason,
        details={
            "source": raw_file.source,
            "report_type": raw_file.report_type,
            "report_month": raw_file.report_month,
            "checksum": raw_file.checksum,
            "parse_status": raw_file.parse_status,
        },
    )
    response = raw_file.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.get("/raw-files")
def list_raw_report_files(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyRawReportFileRepository, Depends(current_raw_report_file_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
    source: str | None = None,
    report_type: str | None = None,
    report_month: str | None = None,
    limit: Annotated[int, Query(ge=1, le=MAX_RAW_REPORT_FILE_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    scope = AccessScope.connector(source) if source else AccessScope.global_scope()
    _require_permission(user, Permission.VIEW_RAW_FILES, scope)
    try:
        page = repository.list_files(
            source=source,
            report_type=report_type,
            report_month=report_month,
            limit=limit,
            offset=offset,
        )
    except RawReportFileValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.RAW_FILE_VIEWED,
        entity_type="raw_report_file_page",
        entity_id=source or "all",
        scope=scope,
        details={"returned": len(page.items), "has_more": page.has_more},
    )
    return {
        "items": [item.to_api() for item in page.items],
        "pagination": {
            "limit": page.limit,
            "offset": page.offset,
            "returned": len(page.items),
            "has_more": page.has_more,
        },
    }


@router.get("/raw-files/{raw_file_id}")
def get_raw_report_file(
    raw_file_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyRawReportFileRepository, Depends(current_raw_report_file_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    if not _has_any_permission_scope(user, Permission.VIEW_RAW_FILES):
        _raise_missing_permission(Permission.VIEW_RAW_FILES)

    try:
        raw_file = repository.get_file(raw_file_id)
    except RawReportFileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except RawReportFileValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    scope = AccessScope.connector(raw_file.source)
    _require_permission(user, Permission.VIEW_RAW_FILES, scope)
    record = record_audit_event(
        sink=audit_sink,
        actor=user,
        event_type=AuditEventType.RAW_FILE_VIEWED,
        entity_type="raw_report_file",
        entity_id=raw_file.id,
        scope=scope,
        details={
            "source": raw_file.source,
            "report_type": raw_file.report_type,
            "report_month": raw_file.report_month,
        },
    )
    response = raw_file.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


def _require_permission(user: UserPrincipal, permission: Permission, scope: AccessScope) -> None:
    if not has_permission(user, permission, scope):
        _raise_missing_permission(permission)


def _raise_missing_permission(permission: Permission) -> None:
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Missing permission: {permission.value}",
    )


def _has_any_permission_scope(user: UserPrincipal, permission: Permission) -> bool:
    if user.disabled:
        return False
    for grant in user.direct_permissions:
        if grant.active and grant.permission == permission:
            return True
    for assignment in user.role_assignments:
        if assignment.active and permission in ROLE_PERMISSIONS.get(assignment.role, frozenset()):
            return True
    return False
