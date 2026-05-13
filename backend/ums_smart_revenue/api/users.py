import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, StrictBool, field_validator, model_validator
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
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope
from ums_smart_revenue.auth.user_permissions import (
    SqlAlchemyUserPermissionGrantRepository,
    UserPermissionGrantConflictError,
    UserPermissionGrantNotFoundError,
    UserPermissionGrantValidationError,
)
from ums_smart_revenue.auth.user_roles import (
    SqlAlchemyUserRoleAssignmentRepository,
    UserRoleAssignmentConflictError,
    UserRoleAssignmentNotFoundError,
    UserRoleAssignmentValidationError,
)
from ums_smart_revenue.auth.users import (
    USER_DISPLAY_NAME_MAX_LENGTH,
    USER_EMAIL_MAX_LENGTH,
    USER_LIST_MAX_OFFSET,
    USER_STATUSES,
    SqlAlchemyUserAccountRepository,
    UserAccountConflictError,
    UserAccountNotFoundError,
    UserAccountServiceAccountPolicyError,
    UserAccountStorageError,
    UserAccountValidationError,
)

router = APIRouter(prefix="/users", tags=["users"])
logger = logging.getLogger(__name__)

FINANCE_ROLE_KEYS = frozenset(
    {RoleKey.FINANCE_ADMIN, RoleKey.FINANCE_APPROVER, RoleKey.FINANCE_VIEWER}
)
FINANCE_PERMISSION_KEYS = frozenset(
    {
        Permission.VIEW_REVENUE,
        Permission.VIEW_FINALIZED_PAYMENTS,
        Permission.VIEW_BANK_RECONCILIATION,
        Permission.MANAGE_BANK_RECONCILIATION,
        Permission.CREATE_MANUAL_OVERRIDE,
        Permission.APPROVE_MANUAL_OVERRIDE,
        Permission.LOCK_FINANCE_MONTH,
        Permission.UNLOCK_FINANCE_MONTH,
        Permission.CHANGE_ALLOCATION_RULE,
        Permission.EXPORT_REVENUE_REPORT,
        Permission.VIEW_GRAPH_FINANCE,
    }
)
CONNECTOR_PERMISSION_KEYS = frozenset(
    {
        Permission.RUN_CONNECTOR_JOBS,
        Permission.MANAGE_CONNECTORS,
        Permission.VIEW_RAW_FILES,
    }
)
SUPER_OWNER_ONLY_PERMISSION_KEYS = frozenset(
    {
        Permission.ASSIGN_ROLES,
        Permission.MANAGE_USERS,
        Permission.MANAGE_PLATFORM_SETTINGS,
        Permission.VIEW_SENSITIVE_AUDIT_PAYLOADS,
    }
)
USER_ACTION_REASON_MAX_LENGTH = 500


class UserRoleAssignRequest(BaseModel):
    """Request body for assigning a role to a user within an access scope."""

    role_key: str = Field(min_length=1)
    scope_type: str = Field(min_length=1)
    scope_id: str | None = None
    reason: str = Field(min_length=1, max_length=USER_ACTION_REASON_MAX_LENGTH)

    @field_validator("role_key", "scope_type", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        """Trim required role-assignment strings and reject blank values."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value

    @field_validator("scope_id", mode="before")
    @classmethod
    def strip_optional_string(cls, value):
        """Normalize blank optional scope ids to None."""
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class UserAccountCreateRequest(BaseModel):
    """Request body for creating human or service user accounts."""

    email: str = Field(min_length=1, max_length=USER_EMAIL_MAX_LENGTH)
    display_name: str = Field(min_length=1, max_length=USER_DISPLAY_NAME_MAX_LENGTH)
    is_service_account: StrictBool = Field(default=False)
    reason: str = Field(min_length=1, max_length=USER_ACTION_REASON_MAX_LENGTH)

    @field_validator("email", "display_name", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        """Trim required account-create strings and reject blank values."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value


class UserAccountUpdateRequest(BaseModel):
    """Request body for partial account metadata or lifecycle updates."""

    email: str | None = Field(default=None, max_length=USER_EMAIL_MAX_LENGTH)
    display_name: str | None = Field(
        default=None, max_length=USER_DISPLAY_NAME_MAX_LENGTH
    )
    status: str | None = None
    reason: str = Field(min_length=1, max_length=USER_ACTION_REASON_MAX_LENGTH)

    @field_validator("email", "display_name", "status", mode="before")
    @classmethod
    def strip_optional_strings(cls, value):
        """Trim optional update strings and treat blanks as absent fields."""
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        """Trim the required audit reason and reject blank values."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value

    @field_validator("status")
    @classmethod
    def normalize_status(cls, value):
        """Normalize lifecycle status values before repository validation."""
        if value is None:
            return None
        normalized = value.lower()
        if normalized not in USER_STATUSES:
            allowed = ", ".join(sorted(USER_STATUSES))
            raise ValueError(f"Unknown status: {normalized}; allowed: {allowed}")
        return normalized

    @model_validator(mode="after")
    def require_account_change(self):
        """Reject requests that would only write an audit reason."""
        if self.email is None and self.display_name is None and self.status is None:
            raise ValueError("at least one account field must be provided")
        return self


class UserRoleRevokeRequest(BaseModel):
    """Request body for revoking an active user role assignment."""

    reason: str = Field(min_length=1, max_length=USER_ACTION_REASON_MAX_LENGTH)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        """Trim the required revoke reason and reject blank values."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value


class UserPermissionGrantRequest(BaseModel):
    """Request body for granting a scoped direct permission to a user."""

    permission_key: str = Field(min_length=1)
    scope_type: str = Field(min_length=1)
    scope_id: str | None = None
    reason: str = Field(min_length=1, max_length=USER_ACTION_REASON_MAX_LENGTH)

    @field_validator("permission_key", "scope_type", "reason", mode="before")
    @classmethod
    def strip_required_strings(cls, value):
        """Trim required permission-grant strings and reject blank values."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value

    @field_validator("scope_id", mode="before")
    @classmethod
    def strip_optional_string(cls, value):
        """Normalize blank optional scope ids to None."""
        if value is None:
            return None
        if isinstance(value, str):
            stripped = value.strip()
            return stripped or None
        return value


class UserPermissionRevokeRequest(BaseModel):
    """Request body for revoking an active direct permission grant."""

    reason: str = Field(min_length=1, max_length=USER_ACTION_REASON_MAX_LENGTH)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_reason(cls, value):
        """Trim the required revoke reason and reject blank values."""
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                raise ValueError("must not be blank")
            return stripped
        return value


def current_user_role_assignment_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyUserRoleAssignmentRepository:
    """FastAPI dependency: provide a scoped role assignment repository."""
    return SqlAlchemyUserRoleAssignmentRepository(session)


def current_user_account_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyUserAccountRepository:
    """FastAPI dependency: provide a user account repository."""
    return SqlAlchemyUserAccountRepository(session)


def current_user_permission_grant_repository(
    session: Annotated[Session, Depends(current_db_session)],
) -> SqlAlchemyUserPermissionGrantRepository:
    """FastAPI dependency: provide a scoped permission grant repository."""
    return SqlAlchemyUserPermissionGrantRepository(session)


@router.get("")
def list_user_accounts(
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyUserAccountRepository, Depends(current_user_account_repository)
    ],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=USER_LIST_MAX_OFFSET)] = 0,
    cursor_email: Annotated[str | None, Query()] = None,
    cursor_id: Annotated[str | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> dict[str, object]:
    """List user accounts for access administration; requires MANAGE_USERS."""
    _require_user_management_permission(user)
    try:
        items, has_more, next_cursor = repository.list_users(
            limit=limit,
            offset=offset,
            status=status_filter,
            cursor_email=cursor_email,
            cursor_id=cursor_id,
        )
    except UserAccountValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except UserAccountStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    return {
        "items": [item.to_api() for item in items],
        "pagination": {
            "limit": limit,
            "offset": offset,
            "returned": len(items),
            "has_more": has_more,
            "next_cursor": next_cursor,
        },
    }


@router.get("/{user_id}/access")
def get_user_access_profile(
    user_id: str,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyUserAccountRepository, Depends(current_user_account_repository)
    ],
) -> dict[str, object]:
    """Return a user's active roles and direct grants; requires MANAGE_USERS."""
    _require_user_management_permission(user)
    try:
        return repository.get_access_profile(user_id=user_id).to_api()
    except UserAccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except UserAccountValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except UserAccountStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc


@router.post("", status_code=status.HTTP_201_CREATED)
def create_user_account(
    payload: UserAccountCreateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyUserAccountRepository, Depends(current_user_account_repository)
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Create a human or service user account; requires MANAGE_USERS."""
    _require_user_management_permission(user)
    if payload.is_service_account:
        _require_service_account_management_policy(user)
    try:
        account = repository.create_user(
            email=payload.email,
            display_name=payload.display_name,
            is_service_account=payload.is_service_account,
        )
    except UserAccountConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except UserAccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except UserAccountValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except UserAccountStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    record = _audit_user_account_change(
        audit_sink=audit_sink,
        actor=user,
        account=account.to_api(),
        reason=payload.reason,
        action="created",
    )
    response = account.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.patch("/{user_id}")
def update_user_account(
    user_id: str,
    payload: UserAccountUpdateRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyUserAccountRepository, Depends(current_user_account_repository)
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Update user account metadata or status; requires MANAGE_USERS."""
    _require_user_management_permission(user)
    try:
        existing = repository.get_user(user_id=user_id)
    except UserAccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except UserAccountValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except UserAccountStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    service_account_updates_allowed = _has_role(user, RoleKey.SUPER_OWNER)
    if (existing.is_service_account or payload.status == "service") and (
        not service_account_updates_allowed
    ):
        _require_service_account_management_policy(user)

    try:
        account = repository.update_user(
            user_id=user_id,
            email=payload.email,
            display_name=payload.display_name,
            status=payload.status,
            service_account_updates_allowed=service_account_updates_allowed,
        )
    except UserAccountConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except UserAccountNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except UserAccountValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except UserAccountServiceAccountPolicyError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
        ) from exc
    except UserAccountStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc

    record = _audit_user_account_change(
        audit_sink=audit_sink,
        actor=user,
        account=account.to_api(),
        reason=payload.reason,
        action="updated",
    )
    response = account.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.post("/{user_id}/roles", status_code=status.HTTP_201_CREATED)
def assign_user_role(
    user_id: str,
    payload: UserRoleAssignRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyUserRoleAssignmentRepository,
        Depends(current_user_role_assignment_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Assign a role to a user within a scope.

    Requires ASSIGN_ROLES and family-specific authority.
    """
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
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except UserRoleAssignmentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except UserRoleAssignmentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

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
    repository: Annotated[
        SqlAlchemyUserRoleAssignmentRepository,
        Depends(current_user_role_assignment_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Revoke an active role assignment.

    Re-checks family policy against the existing role before revoking.
    """
    _require_role_assignment_permission(user)
    try:
        existing = repository.get_assignment(
            user_id=user_id, assignment_id=assignment_id
        )
    except UserRoleAssignmentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except UserRoleAssignmentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    _require_role_assignment_policy(user, RoleKey(existing.role_key))

    try:
        assignment = repository.revoke_role(
            user_id=user_id,
            assignment_id=assignment_id,
            revoked_by=user.user_id,
            reason=payload.reason,
        )
    except UserRoleAssignmentConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except UserRoleAssignmentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except UserRoleAssignmentValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
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


@router.post("/{user_id}/permissions", status_code=status.HTTP_201_CREATED)
def grant_user_permission(
    user_id: str,
    payload: UserPermissionGrantRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyUserPermissionGrantRepository,
        Depends(current_user_permission_grant_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Grant a direct permission to a user; enforces family-specific authority rules."""
    _require_role_assignment_permission(user)
    permission = _parse_permission_for_policy(payload.permission_key)
    _require_permission_grant_policy(user, permission)
    try:
        grant = repository.grant_permission(
            user_id=user_id,
            permission_key=payload.permission_key,
            scope_type=payload.scope_type,
            scope_id=payload.scope_id,
            granted_by=user.user_id,
            reason=payload.reason,
        )
    except UserPermissionGrantConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except UserPermissionGrantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except UserPermissionGrantValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    record = _audit_permission_change(
        audit_sink=audit_sink,
        actor=user,
        grant=grant.to_api(),
        reason=payload.reason,
        action="granted",
    )
    response = grant.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


@router.post("/{user_id}/permissions/{grant_id}/revoke")
def revoke_user_permission(
    user_id: str,
    grant_id: str,
    payload: UserPermissionRevokeRequest,
    user: Annotated[UserPrincipal, Depends(current_principal_from_headers)],
    repository: Annotated[
        SqlAlchemyUserPermissionGrantRepository,
        Depends(current_user_permission_grant_repository),
    ],
    audit_sink: Annotated[AuditSink, Depends(current_audit_sink)],
) -> dict[str, object]:
    """Revoke an active permission grant.

    Re-checks family policy against the stored permission.
    """
    _require_role_assignment_permission(user)
    try:
        existing = repository.get_grant(user_id=user_id, grant_id=grant_id)
    except UserPermissionGrantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except UserPermissionGrantValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    permission = _parse_permission_for_policy(existing.permission_key)
    _require_permission_grant_policy(user, permission)

    try:
        grant = repository.revoke_permission(
            user_id=user_id,
            grant_id=grant_id,
            revoked_by=user.user_id,
            reason=payload.reason,
        )
    except UserPermissionGrantConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    except UserPermissionGrantNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    except UserPermissionGrantValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    record = _audit_permission_change(
        audit_sink=audit_sink,
        actor=user,
        grant=grant.to_api(),
        reason=payload.reason,
        action="revoked",
    )
    response = grant.to_api()
    response["audit_event"] = audit_record_to_api(record)
    return response


def _require_role_assignment_permission(user: UserPrincipal) -> None:
    """Raise 403 if the caller lacks ASSIGN_ROLES on the global scope."""
    if not has_permission(user, Permission.ASSIGN_ROLES, AccessScope.global_scope()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.ASSIGN_ROLES.value}",
        )


def _require_user_management_permission(user: UserPrincipal) -> None:
    """Raise 403 if the caller lacks MANAGE_USERS on the global scope."""
    if not has_permission(user, Permission.MANAGE_USERS, AccessScope.global_scope()):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing permission: {Permission.MANAGE_USERS.value}",
        )


def _require_service_account_management_policy(user: UserPrincipal) -> None:
    """Restrict service account lifecycle changes to Super Owner."""
    if not _has_role(user, RoleKey.SUPER_OWNER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Service account management requires Super Owner",
        )


def _parse_role_for_policy(role_key: str) -> RoleKey:
    """Parse role_key to RoleKey; raise 422 for unknown values."""
    try:
        return RoleKey(role_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown role_key: {role_key}",
        ) from exc


def _parse_permission_for_policy(permission_key: str) -> Permission:
    """Parse permission_key to Permission; raise 422 for unknown values."""
    try:
        return Permission(permission_key)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Unknown permission_key: {permission_key}",
        ) from exc


def _require_role_assignment_policy(user: UserPrincipal, role: RoleKey) -> None:
    """Enforce role-family-specific assignment authority."""
    if role == RoleKey.SUPER_OWNER and not _has_role(user, RoleKey.SUPER_OWNER):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super Owner assignments require Super Owner",
        )
    if role in FINANCE_ROLE_KEYS and not _has_any_role(
        user, {RoleKey.FINANCE_ADMIN, RoleKey.SUPER_OWNER}
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Finance roles require Finance Admin or Super Owner",
        )


def _require_permission_grant_policy(
    user: UserPrincipal, permission: Permission
) -> None:
    """Enforce permission-family-specific grant authority."""
    if permission in SUPER_OWNER_ONLY_PERMISSION_KEYS and not _has_role(
        user, RoleKey.SUPER_OWNER
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrative permissions require Super Owner",
        )
    if permission in FINANCE_PERMISSION_KEYS and not _has_any_role(
        user, {RoleKey.FINANCE_ADMIN, RoleKey.SUPER_OWNER}
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Finance permissions require Finance Admin or Super Owner",
        )
    if permission in CONNECTOR_PERMISSION_KEYS and not _has_any_role(
        user, {RoleKey.CONNECTOR_ADMIN, RoleKey.SUPER_OWNER}
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Connector permissions require Connector Admin or Super Owner",
        )


def _has_role(user: UserPrincipal, role: RoleKey) -> bool:
    """Return whether the caller has an active assignment for one role."""
    return any(
        assignment.active and assignment.role == role
        for assignment in user.role_assignments
    )


def _has_any_role(user: UserPrincipal, roles: set[RoleKey]) -> bool:
    """Return whether the caller has any active assignment in a role set."""
    return any(
        assignment.active and assignment.role in roles
        for assignment in user.role_assignments
    )


def _audit_role_change(
    *,
    audit_sink: AuditSink,
    actor: UserPrincipal,
    assignment: dict[str, object],
    reason: str,
    action: str,
):
    """Record a fail-closed audit event for role assignment changes."""
    return _record_user_audit_event(
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


def _audit_user_account_change(
    *,
    audit_sink: AuditSink,
    actor: UserPrincipal,
    account: dict[str, object],
    reason: str,
    action: str,
):
    """Record a fail-closed audit event for account lifecycle changes."""
    return _record_user_audit_event(
        sink=audit_sink,
        actor=actor,
        event_type=AuditEventType.USER_ACCOUNT_CHANGED,
        entity_type="user",
        entity_id=str(account["id"]),
        scope=AccessScope.global_scope(),
        reason=reason,
        details={
            "action": action,
            "target_user_id": account["id"],
            "email": account["email"],
            "status": account["status"],
            "is_service_account": account["is_service_account"],
        },
    )


def _audit_permission_change(
    *,
    audit_sink: AuditSink,
    actor: UserPrincipal,
    grant: dict[str, object],
    reason: str,
    action: str,
):
    """Record a fail-closed audit event for direct permission changes."""
    return _record_user_audit_event(
        sink=audit_sink,
        actor=actor,
        event_type=AuditEventType.USER_PERMISSION_CHANGED,
        entity_type="user_permission_grant",
        entity_id=str(grant["id"]),
        scope=AccessScope.global_scope(),
        reason=reason,
        details={
            "action": action,
            "target_user_id": grant["user_id"],
            "permission_key": grant["permission_key"],
            "scope_type": grant["scope_type"],
            "scope_id": grant["scope_id"],
            "active": grant["active"],
        },
    )


def _record_user_audit_event(**kwargs):
    """Persist a user-management audit event or return a stable API error."""
    try:
        return record_audit_event(**kwargs)
    except ValueError as exc:
        _rollback_audit_session(kwargs.get("sink"))
        logger.warning("User-management audit event rejected: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        _rollback_audit_session(kwargs.get("sink"))
        logger.exception("User-management audit event recording failed")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Audit logging unavailable",
        ) from exc


def _rollback_audit_session(sink: object) -> None:
    """Rollback SQL-backed audit sinks so handled API errors do not commit writes."""
    rollback = getattr(sink, "rollback", None)
    if callable(rollback):
        rollback()
