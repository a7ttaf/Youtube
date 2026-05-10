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
    role_assignments: tuple[RoleAssignment, ...] = field(default_factory=tuple)
    direct_permissions: tuple[PermissionGrant, ...] = field(default_factory=tuple)
    is_service_account: bool = False
    disabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "role_assignments", tuple(self.role_assignments))
        object.__setattr__(self, "direct_permissions", tuple(self.direct_permissions))

