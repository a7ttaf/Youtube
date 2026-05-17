"""Immutable request-principal models used by authorization policy code."""

from dataclasses import dataclass, field

from ums_smart_revenue.auth.permissions import Permission
from ums_smart_revenue.auth.roles import RoleKey
from ums_smart_revenue.auth.scopes import AccessScope


@dataclass(frozen=True)
class RoleAssignment:
    """Role membership granted to a user over an access scope."""

    role: RoleKey
    scope: AccessScope
    active: bool = True


@dataclass(frozen=True)
class PermissionGrant:
    """Direct permission grant assigned to a user over an access scope."""

    permission: Permission
    scope: AccessScope
    active: bool = True


@dataclass(frozen=True)
class UserPrincipal:
    """Immutable tenant-user identity and authorization material."""

    user_id: str
    email: str
    role_assignments: tuple[RoleAssignment, ...] = field(default_factory=tuple)
    direct_permissions: tuple[PermissionGrant, ...] = field(default_factory=tuple)
    is_service_account: bool = False
    disabled: bool = False
    # Tenant the user belongs to. None during the pre-S2.4 window where some
    # routes still run without tenant resolution; will become required when
    # the resolver middleware is installed by create_app() and `users.tenant_id`
    # is populated. Stored as the string form of the tenant UUID, matching
    # the existing `user_id: str` convention.
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        """Freeze iterable authorization collections as tuples."""
        object.__setattr__(self, "role_assignments", tuple(self.role_assignments))
        object.__setattr__(self, "direct_permissions", tuple(self.direct_permissions))
