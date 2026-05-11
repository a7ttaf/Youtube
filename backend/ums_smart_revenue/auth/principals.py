from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ums_smart_revenue.auth.models import PermissionGrant, RoleAssignment, UserPrincipal
from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope, ScopeType
from ums_smart_revenue.db.security_models import (
    AccessScopeORM,
    UserORM,
    UserPermissionGrantORM,
    UserRoleAssignmentORM,
)


class PrincipalLoadError(ValueError):
    pass


class PrincipalNotFoundError(PrincipalLoadError):
    pass


class PrincipalDisabledError(PrincipalLoadError):
    pass


class PrincipalValidationError(PrincipalLoadError):
    pass


class SqlAlchemyPrincipalLoader:
    def __init__(self, session: Session):
        self._session = session

    def load(self, *, user_id: str) -> UserPrincipal:
        parsed_user_id = _parse_uuid(user_id)
        user = self._session.get(UserORM, parsed_user_id)
        if user is None:
            raise PrincipalNotFoundError("User is not registered")
        if user.status == "disabled":
            raise PrincipalDisabledError("User is disabled")

        return UserPrincipal(
            user_id=str(user.id),
            email=user.email,
            role_assignments=self._load_role_assignments(user.id),
            direct_permissions=self._load_permission_grants(user.id),
            is_service_account=user.is_service_account or user.status == "service",
            disabled=False,
        )

    def _load_role_assignments(self, user_id: UUID) -> tuple[RoleAssignment, ...]:
        rows = self._session.execute(
            select(UserRoleAssignmentORM, AccessScopeORM)
            .join(AccessScopeORM, UserRoleAssignmentORM.scope_id == AccessScopeORM.id)
            .where(
                UserRoleAssignmentORM.user_id == user_id,
                UserRoleAssignmentORM.active.is_(True),
            )
            .order_by(UserRoleAssignmentORM.assigned_at, UserRoleAssignmentORM.id)
        ).all()
        assignments: list[RoleAssignment] = []
        for assignment, scope in rows:
            assignments.append(
                RoleAssignment(
                    role=_parse_role(assignment.role_key),
                    scope=_to_scope(scope),
                    active=True,
                )
            )
        return tuple(assignments)

    def _load_permission_grants(self, user_id: UUID) -> tuple[PermissionGrant, ...]:
        rows = self._session.execute(
            select(UserPermissionGrantORM, AccessScopeORM)
            .join(AccessScopeORM, UserPermissionGrantORM.scope_id == AccessScopeORM.id)
            .where(
                UserPermissionGrantORM.user_id == user_id,
                UserPermissionGrantORM.active.is_(True),
            )
            .order_by(UserPermissionGrantORM.granted_at, UserPermissionGrantORM.id)
        ).all()
        grants: list[PermissionGrant] = []
        for grant, scope in rows:
            grants.append(
                PermissionGrant(
                    permission=_parse_permission(grant.permission_key),
                    scope=_to_scope(scope),
                    active=True,
                )
            )
        return tuple(grants)


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise PrincipalValidationError("user_id must be a valid UUID") from exc


def _parse_role(value: str) -> RoleKey:
    try:
        return RoleKey(value)
    except ValueError as exc:
        raise PrincipalValidationError(f"Unknown role assignment in database: {value}") from exc


def _parse_permission(value: str) -> Permission:
    try:
        return Permission(value)
    except ValueError as exc:
        raise PrincipalValidationError(f"Unknown permission grant in database: {value}") from exc


def _to_scope(scope: AccessScopeORM) -> AccessScope:
    try:
        return AccessScope(ScopeType(scope.scope_type), scope.scope_id)
    except ValueError as exc:
        raise PrincipalValidationError(f"Unknown access scope in database: {scope.scope_type}") from exc
