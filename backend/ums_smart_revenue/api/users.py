from typing import Annotated

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
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.auth.user_roles import (
    UserRoleAssignmentConflictError,
    UserRoleAssignmentNotFoundError,
    UserRoleAssignmentValidationError,
    SqlAlchemyUserRoleAssignmentRepository,
)


router = APIRouter(prefix="/users", tags=["users"])

FINANCE_ROLE_KEYS = frozenset({RoleKey.FINANCE_ADMIN, RoleKey.FINANCE_APPROVER, RoleKey.FINANCE_VIEWER})


class UserRoleAssignRequest(BaseModel):
    role_key: str = Field(min_length=1)
    scope_type: str = Field(min_length=1)
    scope_id: str | None = None
    reason: str = Field(min_length=1)

    @field_validator("role_key", "scope_type", "reason", mode="before")
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


class UserRoleRevokeRequest(BaseModel):
    reason: str = Field(min_length=1)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value


def current_user_role_assignment_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyUserRoleAssignmentRepository:
    return SqlAlchemyUserRoleAssignmentRepository(session)


@router.post("/{user_id}/roles", status_code=status.HTTP_201_CREATED)
def assign_user_role(
    user_id: str,
    payload: UserRoleAssignRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyUserRoleAssignmentRepository, Depends(current_user_role_assignment_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    _require_role_assignment_permission(user)
    role = _parse_role_for_policy(payload.role_key)
    _require_role_assignment_policy(user, role)
    try:
        assignment = repository.assign_role(
            user_id=user_id,
            role_key=payload.role_key,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            assigned_by=user.user_id,
            reason=payload.reason,
        )
    except UserRoleAssignmentConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except UserRoleAssignmentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except UserRoleAssignmentValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    record = _audit_role_change(
        audit_sink=audit_sink,
        actor=user,
        assignment=assignment.to_api(),
        reason=payload.reason,
        action="assigned",
    )
    response = assignment.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.post("/{user_id}/roles/{assignment_id}/revoke")
def revoke_user_role(
    user_id: str,
    assignment_id: str,
    payload: UserRoleRevokeRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[SqlAlchemyUserRoleAssignmentRepository, Depends(current_user_role_assignment_repository)],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    _require_role_assignment_permission(user)
    try:
        assignment = repository.revoke_role(
            user_id=user_id,
            assignment_id=assignment_id,
            revoked_by=user.user_id,
            reason=payload.reason,
        )
    except UserRoleAssignmentConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except UserRoleAssignmentNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except UserRoleAssignmentValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc

    _require_role_assignment_policy(user, RoleKey(assignment.role_key))
    record = _audit_role_change(
        audit_sink=audit_sink,
        actor=user,
        assignment=assignment.to_api(),
        reason=payload.reason,
        action="revoked",
    )
    response = assignment.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


def _require_role_assignment_permission(user: UserPrincipal) -> None:
    if not has_permission(user, Permission.ASSIGN_ROLES, AccessScope.global_scope()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.ASSIGN_ROLES.value}",
        )


def _parse_role_for_policy(role_key: str) -> RoleKey:
    try:
        return RoleKey(role_key)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=f"Unknown role_key: {role_key}") from exc


def _require_role_assignment_policy(user: UserPrincipal, role: RoleKey) -> None:
    if role == RoleKey.SUPER_OWNER and not _has_role(user, RoleKey.SUPER_OWNER):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super Owner assignments require Super Owner")
    if role in FINANCE_ROLE_KEYS and not _has_any_role(user, {RoleKey.FINANCE_ADMIN, RoleKey.SUPER_OWNER}):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Finance roles require Finance Admin or Super Owner",
        )


def _has_role(user: UserPrincipal, role: RoleKey) -> bool:
    return any(assignment.active and assignment.role == role for assignment in user.role_assignments)


def _has_any_role(user: UserPrincipal, roles: set[RoleKey]) -> bool:
    return any(assignment.active and assignment.role in roles for assignment in user.role_assignments)


def _audit_role_change(
    *,
    audit_sink: AuditSink,
    actor: UserPrincipal,
    assignment: dict[str, object],
    reason: str,
    action: str,
):
    return record_audit_event(
        sink=audit_sink,
        actor=actor,
        event_type=AuditEventType.USER_ROLE_CHANGED,
        entity_type="user_role_assignment",
        entity_id=str(assignment["id"]),
        scope=AccessScope.global_scope(),
        reason=reason,
        details={
            "action": action,
            "target_user_id": assignment["user_id"],
            "role_key": assignment["role_key"],
            "scope_type": assignment["scope_type"],
            "scope_id": assignment["scope_id"],
            "active": assignment["active"],
        },
    )
