from dataclasses import dataclass, field

from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope


@dataclass(frozen=True)
class RoleAssignment:
    role: RoleKey
    scope: AccessScope
    active: bool = True


@dataclass(frozen=True)
class PermissionGrant:
    permission: Permission
    scope: AccessScope
    active: bool = True


@dataclass(frozen=True)
class UserPrincipal:
    user_id: str
    email: str
    role_assignments: list[RoleAssignment] = field(default_factory=list)
    direct_permissions: list[PermissionGrant] = field(default_factory=list)
    is_service_account: bool = False
    disabled: bool = False

